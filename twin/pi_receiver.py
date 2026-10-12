"""
Laptop-side receiver for the Raspberry Pi telemetry sync.

The Pi measures and POSTs raw JSON (twin/pi_protocol.py) to this process; this
process validates it, orders and de-duplicates it by (session_id, seq), and does
the Ditto PATCH. The Pi never talks to Ditto and never holds a Ditto credential.

    set PI_SHARED_TOKEN (same value on the Pi), then, from the repo root:
    python twin/pi_receiver.py --bind <this laptop's address> [--dry-run]

Specification: notes/PI_TELEMETRY_SPEC_2026-10-11.md. Walk-through:
notes/PI_BRINGUP_RUNBOOK_2026-10-11.md.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import socket
import socketserver
import sys
import threading
import time
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twin import pi_protocol as pp  # noqa: E402
from twin.config import NAMESPACE, thing  # noqa: E402
from twin.ditto_client import ditto, enc  # noqa: E402

PI_TYPE_ID = f"{NAMESPACE}:PiType"
PI_THING_ID = thing("pi_0")
PI_LINK_FEATURE = "link"

DEFAULT_PORT = 8765
DITTO_TIMEOUT_SEC = 3.0
SEEN_WINDOW = 1024     # recent seq values remembered per session, for duplicate detection
MAX_SESSIONS = 16      # sessions remembered; a Pi restart makes a new session
MIN_TOKEN_LEN = 16
DRAIN_LIMIT = 65536    # most unread request body discarded after an early rejection
MAX_CONNECTIONS = 64   # concurrent connections served; more are closed at once
RECEIVE_DEADLINE_SEC = 10.0  # TOTAL time allowed to receive one request (line, headers, body)
MAX_DECLARED_LENGTH = 10 ** 12  # any longer Content-Length number is clamped to this and refused as too large
TOKEN_ENV = "PI_SHARED_TOKEN"
_UINT_RE = re.compile(r"[0-9]+")


# --------------------------------------------------------------------------
# Run log (one JSON object per line) -- input for scripts/analyze_pi_run.py
# --------------------------------------------------------------------------
class JsonlRunLog:
    def __init__(self, path: Any):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()

    def write(self, record: Dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"), allow_nan=False)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()


def default_run_log_path(now_ms: Optional[int] = None) -> Path:
    """notes/_pi_runs/ is git-ignored (.gitignore: notes/_*)."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime((now_ms if now_ms is not None else pp.now_ms()) / 1000.0))
    return ROOT / "notes" / "_pi_runs" / f"receiver_{stamp}.jsonl"


# --------------------------------------------------------------------------
# Ditto PATCH (same call shape as twin/publisher.py patch_node)
# --------------------------------------------------------------------------
def make_ditto_patch(timeout: float = DITTO_TIMEOUT_SEC) -> Callable[[str, Dict[str, Any]], int]:
    def patch(thing_id: str, body: Dict[str, Any]) -> int:
        resp = ditto(
            "PATCH",
            f"/api/2/things/{enc(thing_id)}/features",
            json=body,
            timeout=timeout,
            content_type="application/merge-patch+json",
        )
        return resp.status_code

    return patch


def build_patch_body(t: pp.Telemetry, received_at_ms: int) -> Dict[str, Any]:
    body: Dict[str, Any] = {name: {"properties": {"value": m["value"]}} for name, m in t.measurements.items()}
    body[PI_LINK_FEATURE] = {
        "properties": {
            "session_id": t.session_id,
            "last_seq": t.seq,
            "last_received_epoch_ms": received_at_ms,
        }
    }
    return body


# --------------------------------------------------------------------------
# Decision logic (no sockets)
# --------------------------------------------------------------------------
class _Session:
    __slots__ = ("max_seq", "seen", "order")

    def __init__(self) -> None:
        self.max_seq = -1
        self.seen: Set[int] = set()
        self.order: Deque[int] = deque()

    def remember(self, seq: int) -> None:
        self.seen.add(seq)
        self.order.append(seq)
        while len(self.order) > SEEN_WINDOW:
            self.seen.discard(self.order.popleft())


class PiReceiver:
    def __init__(
        self,
        patch_fn: Callable[[str, Dict[str, Any]], int],
        thing_id: str,
        run_log: Optional[Any] = None,
        clock_ms: Callable[[], int] = pp.now_ms,
        dry_run: bool = False,
        log: Callable[[str], None] = print,
    ):
        self._patch = patch_fn
        self._thing_id = thing_id
        self._run_log = run_log
        self._clock = clock_ms
        self._dry_run = dry_run
        self._log = log
        self._lock = threading.Lock()
        self._sessions: "OrderedDict[str, _Session]" = OrderedDict()
        self._current: Optional[str] = None

    def now_ms(self) -> int:
        return self._clock()

    def known_session_count(self) -> int:
        return len(self._sessions)

    def log_start(self) -> None:
        self._write({
            "type": "start",
            "started_at_ms": self._clock(),
            "protocol": pp.PROTOCOL_VERSION,
            "thing_id": self._thing_id,
            "dry_run": self._dry_run,
        })

    def _write(self, record: Dict[str, Any]) -> None:
        if self._run_log is not None:
            self._run_log.write(record)

    # -- clock probe ---------------------------------------------------------
    def probe_reply(self, t1: int, t2: int) -> Dict[str, int]:
        """t3 is stamped last: no file I/O between it and the reply being written."""
        return {"v": pp.PROTOCOL_VERSION, "t1": t1, "t2": t2, "t3": self._clock()}

    def log_probe_served(self, reply: Dict[str, int]) -> None:
        self._write({"type": "probe_served", "t1": reply["t1"], "t2": reply["t2"], "t3": reply["t3"]})

    # -- telemetry -----------------------------------------------------------
    def _classify(self, t: pp.Telemetry) -> Tuple[str, Optional[int]]:
        session = self._sessions.get(t.session_id)
        if session is None:
            # Unseen session. A first seq of 0 means nothing earlier was lost; any other first
            # seq is unknown (the receiver may simply have started mid-run), so no loss is claimed.
            return "applied", (0 if t.seq == 0 else None)
        if t.session_id != self._current:
            return "stale_session", None
        if t.seq in session.seen:
            return "duplicate", None
        if t.seq <= session.max_seq:
            return "stale", None  # late, or too old for the seen window to tell
        return "applied", t.seq - session.max_seq - 1

    def _commit(self, t: pp.Telemetry, status: str) -> None:
        session = self._sessions.get(t.session_id)
        if session is None:
            session = _Session()
            self._sessions[t.session_id] = session
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)
        if status == "applied":
            session.max_seq = t.seq
            self._current = t.session_id
        session.remember(t.seq)

    def handle_telemetry(self, t: pp.Telemetry, received_at_ms: int) -> Dict[str, Any]:
        notes: List[str] = []  # console lines, printed after the lock is released (a stalled console must block nobody)
        with self._lock:
            status, missed = self._classify(t)
            ditto_state = "skipped"
            patched_at_ms: Optional[int] = None
            if status in ("applied", "stale"):
                self._commit(t, status)
            if status == "applied":
                if self._dry_run:
                    ditto_state = "disabled"
                else:
                    ditto_state, patched_at_ms = self._apply_to_ditto(t, received_at_ms, notes)

            probes: List[Dict[str, Any]] = []
            if status != "duplicate":
                for p in t.probes:
                    offset, delay = pp.compute_offset_delay(p["t1"], p["t2"], p["t3"], p["t4"])
                    probes.append(dict(p, offset_ms=offset, delay_ms=delay))
            self._write({
                "type": "sample",
                "session_id": t.session_id,
                "seq": t.seq,
                "status": status,
                "missed": missed if status == "applied" else None,
                "sent_at_ms": t.sent_at_ms,
                "received_at_ms": received_at_ms,
                "patched_at_ms": patched_at_ms,
                "ditto": ditto_state,
                "ntp_synchronized": t.ntp_synchronized,
                "measurements": t.measurements,
                "rejected_features": t.rejected_features,
                "probes": probes,
            })
            notes.append(f"seq={t.seq} {status} ditto={ditto_state} features={len(t.measurements)}")
            reply = {
                "v": pp.PROTOCOL_VERSION,
                "status": status,
                "received_at_ms": received_at_ms,
                "ditto": ditto_state,
                "rejected_features": t.rejected_features,
            }
        for line in notes:
            self._log(line)
        return reply

    def _apply_to_ditto(self, t: pp.Telemetry, received_at_ms: int, notes: List[str]) -> Tuple[str, Optional[int]]:
        body = build_patch_body(t, received_at_ms)
        try:
            code = self._patch(self._thing_id, body)
        except Exception as exc:  # network error, timeout, anything: the receiver keeps listening
            notes.append(f"WARN Ditto PATCH failed: {type(exc).__name__}")
            return "failed", None
        if not 200 <= code < 300:
            hint = (" (pi_0 does not exist in Ditto; run scripts/setup_pi_type.py --apply)" if code == 404 else "")
            notes.append(f"WARN Ditto PATCH HTTP {code}{hint}")
            return "failed", None
        return "ok", self._clock()


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------
def _printable(text: str, limit: int = 80) -> str:
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in text[:limit])


def _declared_length(raw: Optional[str]) -> Optional[int]:
    """Content-Length as a number, or None if absent/garbage. Absurdly long digit strings are
    clamped (they are 'too large' anyway) so they never reach int(), whose 4300-digit limit
    would raise."""
    if raw is None:
        return None
    raw = raw.strip()
    if not _UINT_RE.fullmatch(raw):
        return None
    return int(raw) if len(raw) <= 12 else MAX_DECLARED_LENGTH


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets another process share the port; keep it off there.
    allow_reuse_address = os.name != "nt"

    def __init__(self, server_address: Any, handler_class: Any, max_connections: int = MAX_CONNECTIONS,
                 log: Callable[[str], None] = print):
        self._slots = threading.BoundedSemaphore(max_connections)
        self._log = log
        super().__init__(server_address, handler_class)

    def server_bind(self) -> None:
        # HTTPServer.server_bind() also calls socket.getfqdn(), a reverse DNS lookup of the bind
        # address that can stall start-up for seconds on a hotspot network. Nothing uses the result.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)  # over the cap: shed at once instead of queueing
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # socketserver's default prints a traceback and the peer's address to stderr
        exc = sys.exc_info()[1]
        self._log(f"ERROR handling connection: {type(exc).__name__}")


def make_server(
    core: PiReceiver,
    token: bytes,
    bind: str,
    port: int,
    log: Callable[[str], None] = print,
    request_timeout: float = 10.0,
    receive_deadline: float = RECEIVE_DEADLINE_SEC,
    max_connections: int = MAX_CONNECTIONS,
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "PiReceiver"
        sys_version = ""
        protocol_version = "HTTP/1.0"  # one request per connection; Connection: close
        timeout = request_timeout      # per recv; the TOTAL receive time is bounded by receive_deadline
        wbufsize = 65536               # buffered: status line, headers and body leave in ONE send ...
        disable_nagle_algorithm = True  # ... and are never held back waiting for the peer's ACK
        _body_read = False
        _watchdog: Optional[threading.Timer] = None

        # -- connection life cycle ------------------------------------------
        def setup(self) -> None:
            super().setup()
            # A per-recv timeout is reset by every byte, so a host dripping one byte at a time
            # could hold this thread for ever. The watchdog bounds the whole request instead.
            self._watchdog = threading.Timer(receive_deadline, self._abort_connection)
            self._watchdog.daemon = True
            self._watchdog.start()

        def finish(self) -> None:
            self._stop_watchdog()
            super().finish()

        def _stop_watchdog(self) -> None:
            if self._watchdog is not None:
                self._watchdog.cancel()

        def _abort_connection(self) -> None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)  # wakes any blocked read
            except OSError:
                pass

        def log_message(self, fmt: str, *args: Any) -> None:  # silence the stdlib access log
            pass

        # -- replies ---------------------------------------------------------
        def _reply(self, code: int, obj: Dict[str, Any], extra: Optional[Dict[str, str]] = None) -> None:
            data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
            self.close_connection = True
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)
            self.wfile.flush()
            log(f"{self.command} {_printable(self.path.split('?', 1)[0])} -> {code}")
            self._finish_unread_body()

        def _finish_unread_body(self) -> None:
            """After an early rejection (401, 404, 413, 415 ...) the request body may still be
            in flight. Closing a socket that holds unread data makes Windows reset the
            connection, and the reset destroys the reply the client has not read yet (found
            by a flaky test run, WinError 10053). So half-close first -- the client gets the
            reply and EOF at once -- then discard what is still arriving, bounded in size
            and, through the watchdog, in time."""
            if self._body_read:
                return
            self._body_read = True
            pending = _declared_length(self.headers.get("Content-Length")) or 0
            if pending == 0 and self.headers.get("Transfer-Encoding") is None:
                return
            try:
                self.connection.shutdown(socket.SHUT_WR)
                self.rfile.read(min(pending or DRAIN_LIMIT, DRAIN_LIMIT))
            except (OSError, ValueError):
                pass

        def _method_not_allowed(self) -> None:
            self._reply(405, {"error": "method not allowed"}, {"Allow": "POST"})

        def __getattr__(self, name: str) -> Any:
            # BaseHTTPRequestHandler answers an unknown method with 501; every method other
            # than POST must be 405, whatever its name (TRACE, PROPFIND, ...).
            if name.startswith("do_"):
                return self._method_not_allowed
            raise AttributeError(name)

        # -- checks ----------------------------------------------------------
        def _authorized(self) -> bool:
            scheme, _, presented = self.headers.get("Authorization", "").partition(" ")
            candidate = presented.strip().encode("utf-8") if scheme.lower() == "bearer" else b""
            return hmac.compare_digest(candidate, token)

        def _content_length(self) -> Optional[int]:
            if self.headers.get("Transfer-Encoding") is not None:
                return None
            return _declared_length(self.headers.get("Content-Length"))

        # -- POST ------------------------------------------------------------
        def do_POST(self) -> None:
            try:
                self._handle_post()
            except (ConnectionError, TimeoutError) as exc:  # the client reset/aborted/stalled: nothing to answer
                self.close_connection = True
                log(f"WARN client connection lost: {type(exc).__name__}")
            except Exception as exc:
                log(f"ERROR handling request: {type(exc).__name__}")
                try:
                    self._reply(500, {"error": "internal error"})
                except (OSError, ValueError):
                    pass

        def _handle_post(self) -> None:
            route = self.path.split("?", 1)[0]
            if route not in (pp.TELEMETRY_PATH, pp.PROBE_PATH):
                return self._reply(404, {"error": "not found"})
            if not self._authorized():
                return self._reply(401, {"error": "unauthorized"}, {"WWW-Authenticate": "Bearer"})
            length = self._content_length()
            if length is None:
                return self._reply(411, {"error": "Content-Length required"})
            if length > pp.MAX_BODY_BYTES:
                return self._reply(413, {"error": "body too large"})
            ctype = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                return self._reply(415, {"error": "Content-Type must be application/json"})
            try:
                raw = self.rfile.read(length)
            except OSError:  # slow or stalled client: drop it
                self.close_connection = True
                return
            self._body_read = True
            self._stop_watchdog()  # the request has been received; processing time is not bounded by it
            stamp = core.now_ms()  # t2 for probes, received_at for telemetry: taken as early as possible
            if len(raw) != length:
                return self._reply(400, {"error": "incomplete body"})
            try:
                obj = pp.parse_json_body(raw)
                if route == pp.PROBE_PATH:
                    t1 = pp.validate_probe_request(obj)
                    reply = core.probe_reply(t1, stamp)
                    self._reply(200, reply)
                    core.log_probe_served(reply)  # after the reply: file I/O must not sit inside the return leg
                    return
                telemetry = pp.validate_telemetry(obj)
            except pp.ProtocolError as exc:
                return self._reply(400, {"error": str(exc)})
            self._reply(200, core.handle_telemetry(telemetry, stamp))

    return _Server((bind, port), Handler, max_connections, log)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------
def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return port


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        number = 0.0
    if not number > 0:
        raise argparse.ArgumentTypeError("must be a number greater than 0")
    return number


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="pi_receiver",
        description="Receive Raspberry Pi telemetry and PATCH it into the local Ditto "
                    f"Thing {PI_THING_ID}. The shared secret is read from ${TOKEN_ENV}, never from an argument.",
    )
    p.add_argument("--bind", required=True,
                   help="IPv4 address to listen on (required; use this laptop's address on the Pi's network)")
    p.add_argument("--port", type=_port, default=DEFAULT_PORT, help=f"TCP port (default {DEFAULT_PORT})")
    p.add_argument("--dry-run", action="store_true",
                   help="accept and log samples but make no Ditto request at all")
    p.add_argument("--run-log", default=None,
                   help="JSONL run log path (default: notes/_pi_runs/receiver_<UTC>.jsonl, git-ignored)")
    p.add_argument("--ditto-timeout", type=_positive_float, default=DITTO_TIMEOUT_SEC,
                   help=f"seconds before a Ditto PATCH is abandoned (default {DITTO_TIMEOUT_SEC})")
    return p.parse_args(argv)


def bind_warning(bind: str) -> Optional[str]:
    if bind in ("0.0.0.0", "::", ""):
        return (f"WARNING: --bind {bind or '(empty)'} listens on all network interfaces. "
                "The token travels in clear text over plain HTTP; prefer this laptop's address on the Pi's network.")
    return None


def load_token(env: Any) -> bytes:
    token = env.get(TOKEN_ENV, "")
    if len(token) < MIN_TOKEN_LEN or not pp.token_ok(token):
        raise SystemExit(f"{TOKEN_ENV} must be set to a secret of at least {MIN_TOKEN_LEN} printable ASCII "
                         "characters without spaces (the same value on the Pi). It is not printed anywhere.")
    return token.encode("utf-8")


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    token = load_token(os.environ)

    def say(line: str) -> None:
        print(time.strftime("%H:%M:%SZ", time.gmtime()) + " " + line, flush=True)

    run_log = JsonlRunLog(Path(args.run_log) if args.run_log else default_run_log_path())
    core = PiReceiver(
        patch_fn=make_ditto_patch(args.ditto_timeout),
        thing_id=PI_THING_ID,
        run_log=run_log,
        clock_ms=pp.now_ms,
        dry_run=args.dry_run,
        log=say,
    )
    try:
        server = make_server(core, token, args.bind, args.port, log=say)
    except OSError as exc:
        run_log.close()
        raise SystemExit(f"cannot listen on {args.bind}:{args.port}: {exc.strerror or exc}")
    warning = bind_warning(args.bind)
    if warning:
        say(warning)
    say(f"Listening on {args.bind}:{args.port}  thing={PI_THING_ID}  "
        f"{'DRY RUN (no Ditto requests)' if args.dry_run else 'live Ditto PATCH'}  run log: {run_log.path}")
    core.log_start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("stopping")
    finally:
        server.server_close()
        run_log.close()


if __name__ == "__main__":
    main()
