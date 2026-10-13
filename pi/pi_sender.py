#!/usr/bin/env python3
"""
Raspberry Pi side of the telemetry sync: measure, then POST raw JSON to the
laptop receiver (twin/pi_receiver.py). Standard library only. The Pi never talks
to Ditto and holds no Ditto credential.

    export PI_SHARED_TOKEN=...            # same secret as on the laptop
    python3 pi/pi_sender.py --receiver http://<laptop address>:8765

Needs this file plus twin/__init__.py and twin/pi_protocol.py copied to the Pi
with the same relative layout. Specification: notes/PI_TELEMETRY_SPEC_2026-10-11.md.
Step-by-step: notes/PI_BRINGUP_RUNBOOK_2026-10-11.md.

Written to stay valid Python 3.9 syntax. It has NOT been run on the Pi yet.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twin import pi_protocol as pp  # noqa: E402

PROC_STAT = "/proc/stat"
PROC_MEMINFO = "/proc/meminfo"
THERMAL_TYPE = "/sys/class/thermal/thermal_zone0/type"
THERMAL_TEMP = "/sys/class/thermal/thermal_zone0/temp"
CPUFREQ = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"

DEFAULT_INTERVAL_SEC = 3.0   # mirrors twin.config PUBLISH_INTERVAL_SEC's default
TOKEN_ENV = "PI_SHARED_TOKEN"
NTP_REFRESH_SEC = 60.0
PROBE_GAP_SEC = 0.2


class AuthRejected(Exception):
    """The receiver answered HTTP 401: the shared token does not match."""


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def warn_stderr(message: str) -> None:
    print("WARN " + message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Measuring
# --------------------------------------------------------------------------
class Sampler:
    """Reads the four measurements. A read that fails, cannot be parsed, or is not
    credible is OMITTED (never 0.0, never carried over). CPU needs two reads, so
    the first read after start (or after any failed read) yields no CPU value."""

    def __init__(
        self,
        read: Callable[[str], str] = read_text,
        clock_ms: Callable[[], int] = pp.now_ms,
        warn: Callable[[str], None] = warn_stderr,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._read = read
        self._clock = clock_ms
        self._warn = warn
        self._monotonic = monotonic  # the CPU window is measured on this, never on wall-clock stamps
        self._prev: Optional[pp.CpuTicks] = None
        self._prev_mono: Optional[float] = None
        self._failing: set = set()

    def _note(self, feature: str, problem: Optional[str]) -> None:
        """Warn once per failure episode, not on every cycle."""
        if problem is None:
            self._failing.discard(feature)
        elif feature not in self._failing:
            self._failing.add(feature)
            self._warn(f"{feature} unavailable ({problem}); omitted until it recovers")

    def _cpu(self, out: Dict[str, Dict[str, Any]]) -> None:
        try:
            text = self._read(PROC_STAT)
            stamp = self._clock()
            mono = self._monotonic()
        except (OSError, ValueError):  # ValueError: undecodable bytes in the file
            self._prev, self._prev_mono = None, None
            self._note("cpu_utilization", "cannot read " + PROC_STAT)
            return
        ticks = pp.parse_proc_stat(text)
        if ticks is None:
            self._prev, self._prev_mono = None, None
            self._note("cpu_utilization", "unparsable " + PROC_STAT)
            return
        prev, prev_mono = self._prev, self._prev_mono
        self._prev, self._prev_mono = ticks, mono  # the current good read is the next baseline
        if prev is None or prev_mono is None:
            return  # lone first sample: no baseline, not a failure
        window_ms = int(round((mono - prev_mono) * 1000.0))
        if window_ms < 1:
            self._note("cpu_utilization", "averaging window under 1 ms")
            return
        percent = pp.cpu_percent(prev, ticks)
        if percent is None or not pp.feature_value_ok("cpu_utilization", percent):
            self._note("cpu_utilization", "counters unusable this cycle")
            return
        self._note("cpu_utilization", None)
        out["cpu_utilization"] = {
            "value": percent,
            "measured_at_ms": stamp,
            "window_ms": window_ms,
        }

    def _instant(self, out: Dict[str, Dict[str, Any]], feature: str, read_and_parse: Callable[[], Any]) -> None:
        try:
            value = read_and_parse()
            stamp = self._clock()
        except (OSError, ValueError):  # ValueError: undecodable bytes in the file
            self._note(feature, "cannot read source file")
            return
        if value is None:
            self._note(feature, "unparsable or unexpected content")
        elif not pp.feature_value_ok(feature, value):
            self._note(feature, "reading not credible")
        else:
            self._note(feature, None)
            out[feature] = {"value": value, "measured_at_ms": stamp}

    def sample(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        self._cpu(out)
        self._instant(out, "memory_utilization", lambda: pp.parse_meminfo_used_percent(self._read(PROC_MEMINFO)))
        self._instant(
            out,
            "cpu_temperature_c",
            lambda: pp.parse_thermal_temp_c(self._read(THERMAL_TYPE), self._read(THERMAL_TEMP)),
        )
        self._instant(out, "cpu_frequency_khz", lambda: pp.parse_cpu_freq_khz(self._read(CPUFREQ)))
        return out


def _run_timedatectl() -> str:
    env = dict(os.environ, LC_ALL="C")  # keep the English "System clock synchronized:" label
    return subprocess.run(["timedatectl"], capture_output=True, text=True, timeout=3, check=False, env=env).stdout


class NtpProbe:
    """Whether the Pi's clock is NTP-synchronized: True / False / None (unknown).
    Never assumed; refreshed at most every NTP_REFRESH_SEC."""

    def __init__(self, runner: Callable[[], str] = _run_timedatectl, monotonic: Callable[[], float] = time.monotonic):
        self._runner = runner
        self._monotonic = monotonic
        self._value: Optional[bool] = None
        self._at: Optional[float] = None

    def get(self) -> Optional[bool]:
        now = self._monotonic()
        if self._at is not None and now - self._at < NTP_REFRESH_SEC:
            return self._value
        try:
            self._value = pp.parse_ntp_synchronized(self._runner())
        except Exception:  # missing binary, timeout, anything: unknown, not "yes"
            self._value = None
        self._at = now
        return self._value


# --------------------------------------------------------------------------
# Talking to the receiver
# --------------------------------------------------------------------------
class HttpLink:
    """One short HTTP/1.0-style request per call. The token only ever goes into the
    Authorization header; it is not printed, logged or part of any exception text."""

    def __init__(self, host: str, port: int, token: str, timeout: float):
        self.host, self.port, self.timeout = host, port, timeout
        self._token = token

    def __repr__(self) -> str:
        return f"HttpLink({self.host}:{self.port})"

    def post(self, path: str, obj: Dict[str, Any]) -> Tuple[int, Any]:
        body = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode("utf-8")
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.request(
                "POST",
                path,
                body=body,
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._token},
            )
            resp = conn.getresponse()
            data = resp.read(65536)
            status = resp.status
        finally:
            conn.close()
        return status, _json_or_none(data)

    def probe(self, clock_ms: Callable[[], int]) -> Tuple[int, Any, int, int]:
        """One clock probe -> (status, reply, t1, t4), t1/t4 on the Pi clock.

        t1 is stamped AFTER the TCP connection is up and t4 as soon as the reply is read.
        The handshake and the server's per-connection startup would otherwise sit in the
        forward leg only, biasing the offset estimate by up to delay/2 (seen as +7..+10 ms
        on loopback, where the true offset is 0)."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.connect()
            t1 = clock_ms()
            body = json.dumps({"v": pp.PROTOCOL_VERSION, "t1": t1}, separators=(",", ":")).encode("utf-8")
            conn.request(
                "POST",
                pp.PROBE_PATH,
                body=body,
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._token},
            )
            resp = conn.getresponse()
            data = resp.read(65536)
            t4 = clock_ms()
            status = resp.status
        finally:
            conn.close()
        return status, _json_or_none(data), t1, t4


def _json_or_none(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):  # anyone on a plain-HTTP network can answer in the receiver's place
        return None


class Sender:
    def __init__(
        self,
        link: Any,
        sampler: Sampler,
        ntp: NtpProbe,
        clock_ms: Callable[[], int],
        sleep: Callable[[float], None],
        monotonic: Callable[[], float],
        session_id: str,
        interval: float,
        probe_burst: int,
        probe_every: float,
        warn: Callable[[str], None],
        say: Callable[[str], None],
    ):
        self.link = link
        self.sampler = sampler
        self.ntp = ntp
        self.clock = clock_ms
        self.sleep = sleep
        self.monotonic = monotonic
        self.session_id = session_id
        self.interval = interval
        self.probe_burst = probe_burst
        self.probe_every = probe_every
        self.warn = warn
        self.say = say
        self.seq = 0
        self.ntp_synchronized: Optional[bool] = None  # last result of self.ntp.get(); None = not checked or unknown
        self.pending: List[Dict[str, int]] = []
        self.stats = {"ok": 0, "failed": 0}
        self._started = False
        self._next_cycle = 0.0
        self._next_probe = 0.0
        self._ditto_warned = False

    # -- clock probe ---------------------------------------------------------
    def probe_once(self) -> Optional[Dict[str, int]]:
        """t1 = Pi send, t4 = Pi receive (both on the Pi clock); t2, t3 come back from the receiver."""
        try:
            status, reply, t1, t4 = self.link.probe(self.clock)
        except (OSError, http.client.HTTPException):
            return None
        if status == 401:
            raise AuthRejected("the receiver rejected the token (HTTP 401): check that " + TOKEN_ENV + " is identical on both ends")
        if status != 200:
            return None
        try:
            t2, t3 = pp.validate_probe_reply(reply, t1)
        except pp.ProtocolError:
            return None
        return {"t1": t1, "t2": t2, "t3": t3, "t4": t4}

    def _probe_into_pending(self) -> None:
        probe = self.probe_once()
        if probe is not None:
            self.pending.append(probe)
            del self.pending[: max(0, len(self.pending) - pp.MAX_PROBES)]

    def _refresh_ntp(self) -> None:
        """May run `timedatectl` (up to 3 s; NtpProbe runs it at most every NTP_REFRESH_SEC). Called only
        from run(), outside cycle(): between the first measurement read and the request it would age every
        measurement of that message by its run time (258-349 ms measured on the Pi 3 B+, 2026-10-13)."""
        self.ntp_synchronized = self.ntp.get()

    # -- one sample cycle ----------------------------------------------------
    def cycle(self) -> None:
        measurements = self.sampler.sample()
        probes, self.pending = self.pending[-pp.MAX_PROBES:], []  # cleared after every attempt
        message = {
            "v": pp.PROTOCOL_VERSION,
            "session_id": self.session_id,
            "seq": self.seq,
            "ntp_synchronized": self.ntp_synchronized,  # the cached value: never refreshed inside a cycle
            "measurements": measurements,
            "probes": probes,
        }
        seq, self.seq = self.seq, self.seq + 1  # a failed send still consumes its number
        message["sent_at_ms"] = self.clock()  # last thing before the request, so only transport is counted after it
        try:
            status, reply = self.link.post(pp.TELEMETRY_PATH, message)
        except (OSError, http.client.HTTPException) as exc:
            self._failed(seq, type(exc).__name__)
            return
        if status == 401:
            raise AuthRejected("the receiver rejected the token (HTTP 401): check that " + TOKEN_ENV + " is identical on both ends")
        if status != 200:
            self._failed(seq, "HTTP %d" % status)
            return
        self.stats["ok"] += 1
        outcome = reply.get("status") if isinstance(reply, dict) else None
        ditto = reply.get("ditto") if isinstance(reply, dict) else None
        if ditto == "failed" and not self._ditto_warned:
            self._ditto_warned = True
            self.warn("the receiver reports that its Ditto update failed (see the receiver's console)")
        elif ditto != "failed":
            self._ditto_warned = False
        shown = " ".join(f"{k}={v['value']}" for k, v in measurements.items()) or "(no measurements)"
        self.say(f"seq={seq} {shown} -> {outcome or 'sent'}")

    def _failed(self, seq: int, reason: str) -> None:
        self.stats["failed"] += 1
        self.warn(f"send failed for seq={seq}: {reason}")

    # -- main loop -----------------------------------------------------------
    def run(self, count: Optional[int] = None) -> None:
        if not self._started:
            self._started = True
            self._refresh_ntp()  # so the first message carries a real value; before the grid starts, so it cannot make a cycle late
            for i in range(self.probe_burst):
                if i:
                    self.sleep(PROBE_GAP_SEC)
                self._probe_into_pending()
            now = self.monotonic()
            self._next_cycle = now
            self._next_probe = now + self.probe_every
        done = 0
        while count is None or done < count:
            self.cycle()
            done += 1
            if self.monotonic() >= self._next_probe:
                self._probe_into_pending()
                self._next_probe = max(self._next_probe + self.probe_every, self.monotonic())
            self._refresh_ntp()  # after the send and any probe, inside the idle slack; usually a cache hit
            if count is not None and done >= count:
                break
            self._next_cycle += self.interval
            remaining = self._next_cycle - self.monotonic()
            if remaining > 0:
                self.sleep(remaining)
            else:
                self._next_cycle = self.monotonic()  # behind schedule: no catch-up burst


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------
def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        number = 0.0
    if not number > 0:
        raise argparse.ArgumentTypeError("must be a number greater than 0")
    return number


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError("must be an integer of at least 1")
    return number


def _non_negative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = -1
    if number < 0:
        raise argparse.ArgumentTypeError("must be an integer of at least 0")
    return number


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="pi_sender",
        description="Measure this Raspberry Pi and send the readings to the laptop receiver. "
                    f"The shared secret is read from ${TOKEN_ENV}, never from an argument.",
    )
    p.add_argument("--receiver", required=True, help="http://<laptop address>:<port> (plain HTTP; port required)")
    p.add_argument("--interval", type=_positive_float, default=DEFAULT_INTERVAL_SEC,
                   help=f"seconds between samples (default {DEFAULT_INTERVAL_SEC})")
    p.add_argument("--count", type=_positive_int, default=None, help="stop after this many samples (default: run until Ctrl+C)")
    p.add_argument("--probe-burst", type=_non_negative_int, default=5, help="clock probes at start (default 5)")
    p.add_argument("--probe-every", type=_positive_float, default=60.0, help="seconds between later clock probes (default 60)")
    p.add_argument("--timeout", type=_positive_float, default=5.0,
                   help="seconds before one request is abandoned (default 5; keep it above the receiver's 3 s Ditto timeout)")
    args = p.parse_args(argv)

    try:
        parts = urlsplit(args.receiver)
        port = parts.port
    except ValueError:
        p.error("--receiver: not a valid URL")
    if parts.scheme == "https":
        p.error("--receiver: TLS is not supported; use http://")
    if parts.scheme != "http" or not parts.hostname or port is None or parts.username or parts.query \
            or parts.path not in ("", "/"):
        p.error("--receiver must look like http://HOST:PORT")
    args.host, args.port = parts.hostname, port
    return args


def load_token() -> str:
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        raise SystemExit(f"{TOKEN_ENV} is not set. Export the same secret that the receiver uses (it is never printed).")
    if not pp.token_ok(token):
        raise SystemExit(f"{TOKEN_ENV} must be printable ASCII without spaces (copy the generated token exactly).")
    return token


def main(argv: Optional[List[str]] = None) -> None:
    args = parse_args(argv)
    token = load_token()
    session_id = secrets.token_hex(8)

    def say(line: str) -> None:
        print(time.strftime("%H:%M:%SZ", time.gmtime()) + " " + line, flush=True)

    sender = Sender(
        link=HttpLink(args.host, args.port, token, args.timeout),
        sampler=Sampler(),
        ntp=NtpProbe(),
        clock_ms=pp.now_ms,
        sleep=time.sleep,
        monotonic=time.monotonic,
        session_id=session_id,
        interval=args.interval,
        probe_burst=args.probe_burst,
        probe_every=args.probe_every,
        warn=warn_stderr,
        say=say,
    )
    say(f"pi_sender session={session_id} -> {args.host}:{args.port} every {args.interval}s (python {sys.version.split()[0]})")
    code = 0
    try:
        sender.run(count=args.count)
    except AuthRejected as exc:
        print("ERROR " + str(exc), file=sys.stderr, flush=True)
        code = 2
    except KeyboardInterrupt:
        pass
    say(f"done: {sender.stats['ok']} sent ok, {sender.stats['failed']} failed")
    sys.exit(code)


if __name__ == "__main__":
    main()
