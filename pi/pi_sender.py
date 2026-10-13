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
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urlsplit

try:
    import ctypes
except ImportError:  # a Python built without libffi: the kernel clock state is then unknown (None), with a warning
    ctypes = None

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
NTP_MAXERROR_LIMIT_US = 16_000_000  # systemd's ntp_synced(): "synchronized" = reported maximum error under 16 s
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


def read_timedatectl(runner: Callable[[], str] = _run_timedatectl) -> Optional[bool]:
    """The `timedatectl` answer (True / False / None), for the start and end cross-check ONLY. It starts a
    subprocess (a 258-349 ms lag when it ran inside the loop on the Pi 3 B+, measured 2026-10-13), so it is
    never called between cycles; the loop reads the kernel directly (read_kernel_sync)."""
    try:
        return pp.parse_ntp_synchronized(runner())
    except Exception:  # missing binary, timeout, anything: unknown, not "yes"
        return None


class KernelClock(NamedTuple):
    """What one read-only adjtimex() call returned: its return value, struct timex.status, and
    struct timex.maxerror in microseconds."""

    rc: int
    status: int
    maxerror_us: int


def make_timex(long_t: Any) -> Any:
    """struct timex as declared in <sys/timex.h>; `long_t` is the C `long`. On Linux LP64 (aarch64 and x86_64)
    sizeof is 208, maxerror is at offset 24 and status at 40: checked with gcc and glibc on x86_64 only, not
    on the Pi. Layout order: man adjtimex(2); the eleven trailing ints are the kernel header's padding."""

    class Timeval(ctypes.Structure):
        _fields_ = [("tv_sec", long_t), ("tv_usec", long_t)]

    class Timex(ctypes.Structure):
        _fields_ = [
            ("modes", ctypes.c_uint),
            ("offset", long_t), ("freq", long_t), ("maxerror", long_t), ("esterror", long_t),
            ("status", ctypes.c_int),
            ("constant", long_t), ("precision", long_t), ("tolerance", long_t),
            ("time", Timeval),
            ("tick", long_t), ("ppsfreq", long_t), ("jitter", long_t),
            ("shift", ctypes.c_int),
            ("stabil", long_t), ("jitcnt", long_t), ("calcnt", long_t), ("errcnt", long_t), ("stbcnt", long_t),
            ("tai", ctypes.c_int),
            ("_reserved", ctypes.c_int * 11),
        ]

    return Timex


_Timex = make_timex(ctypes.c_long) if ctypes is not None else None
_adjtimex_fn: Any = None


def _libc_adjtimex(tx: Any) -> int:
    """Call the C library's adjtimex(tx). The process's own symbol table is used (CDLL(None), i.e.
    dlopen(NULL)) and not ctypes.util.find_library, which starts subprocesses (ldconfig, gcc, ld) on Linux.
    Raises OSError when adjtimex returns -1, AttributeError when this libc has no adjtimex."""
    global _adjtimex_fn
    if _adjtimex_fn is None:
        fn = ctypes.CDLL(None, use_errno=True).adjtimex
        fn.argtypes = [ctypes.POINTER(_Timex)]
        fn.restype = ctypes.c_int
        _adjtimex_fn = fn
    rc = _adjtimex_fn(ctypes.byref(tx))
    if rc < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return rc


def read_adjtimex(call: Optional[Callable[[Any], int]] = None) -> Optional[KernelClock]:
    """One read-only adjtimex (modes = 0: nothing is set) -> (rc, status, maxerror_us), or None when the
    kernel clock state cannot be read: not Linux, no ctypes, a libc without adjtimex, an OSError, any ctypes
    error. Never raises. `call(struct)` is the test seam; it fills the struct and returns adjtimex's value."""
    try:
        if call is None:
            if ctypes is None or not sys.platform.startswith("linux"):
                return None
            call = _libc_adjtimex
        tx = _Timex()  # zeroed, so modes = 0
        rc = call(tx)
        return KernelClock(int(rc), int(tx.status), int(tx.maxerror))
    except Exception:  # AttributeError, OSError, ctypes.ArgumentError, ...: unknown, never "yes" or "no"
        return None


def sync_verdict(clock: Optional[KernelClock]) -> Optional[bool]:
    """True / False from a KernelClock; None when there is none or adjtimex failed (rc < 0)."""
    if clock is None or clock.rc < 0:
        return None
    return clock.maxerror_us < NTP_MAXERROR_LIMIT_US


def read_kernel_sync(call: Optional[Callable[[Any], int]] = None) -> Optional[bool]:
    """Whether the kernel considers the system clock synchronized: True / False, or None when unknown.

    This reproduces what `timedatectl` prints as "System clock synchronized", which is systemd's
    ntp_synced() (src/timedate/timedated.c; read at v257, the systemd version on the Pi has NOT been
    checked): adjtimex() succeeded and the reported maximum error (struct timex.maxerror, microseconds) is
    under 16,000,000, i.e. 16 s. Like systemd, this ignores STA_UNSYNC (systemd: it may have been set to keep
    the kernel from touching the RTC) and never compares the return value with TIME_ERROR.

    Deliberate difference: systemd maps an adjtimex failure to "not synchronized"; here it is None (unknown),
    as are non-Linux, a Python without ctypes, a libc without adjtimex and any ctypes error. Never raises.

    The flag is coarse: it says the reported maximum error is under 16 s. It is not a measure of clock
    quality. The call only reads (modes = 0; unprivileged callers may, adjtimex(2); not tested on the Pi),
    starts no subprocess, and its cost has not been measured."""
    return sync_verdict(read_adjtimex(call))


def info_stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class NtpProbe:
    """Whether the Pi's clock is synchronized, as `timedatectl` reports it: True / False / None (unknown).
    Never assumed; refreshed at most every NTP_REFRESH_SEC. `reader` returns that value (default: the kernel
    clock state, read_kernel_sync)."""

    def __init__(self, reader: Optional[Callable[[], Optional[bool]]] = None,
                 monotonic: Callable[[], float] = time.monotonic):
        self._reader = reader if reader is not None else read_kernel_sync
        self._monotonic = monotonic
        self._value: Optional[bool] = None
        self._at: Optional[float] = None

    def get(self) -> Optional[bool]:
        now = self._monotonic()
        if self._at is not None and now - self._at < NTP_REFRESH_SEC:
            return self._value
        try:
            self._value = self._reader()
        except Exception:  # whatever went wrong: unknown, not "yes"
            self._value = None
        self._at = now
        return self._value


class NtpCrossCheck:
    """Prints, to stderr, the kernel clock state next to the `timedatectl` answer, once at the start of a
    run and once at its end, so that a disagreement on the Pi can be read off one line. The start call comes
    before the first CPU baseline read and the end call after the last send, so the `timedatectl` subprocess
    falls in no reported CPU window. Never raises."""

    def __init__(self, reference: Callable[[], Optional[bool]] = read_timedatectl,
                 kernel: Callable[[], Optional[KernelClock]] = read_adjtimex,
                 log: Callable[[str], None] = info_stderr):
        self._reference = reference
        self._kernel = kernel
        self._log = log

    def report(self, when: str) -> None:
        try:
            clock = self._kernel()
        except Exception:
            clock = None
        try:
            reference = self._reference()
        except Exception:
            reference = None
        if clock is None:
            kernel_text = "adjtimex unavailable -> None"
        else:
            kernel_text = "adjtimex rc=%d status=0x%04x maxerror_us=%d -> %s" % (
                clock.rc, clock.status, clock.maxerror_us, sync_verdict(clock))
        try:
            self._log("ntp check (%s): %s; timedatectl -> %s" % (when, kernel_text, reference))
        except Exception:  # a closed stderr or a full disk must not kill the sender or change its exit code
            pass


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
        cross_check: Optional[Any] = None,
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
        self.cross_check = cross_check  # NtpCrossCheck or None: prints the kernel state next to timedatectl at start and end
        self._final_checked = False
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
        """Reads the kernel clock state (NtpProbe, at most every NTP_REFRESH_SEC): one system call, no
        subprocess, cost not measured. Called only from run(), outside cycle(): a slow refresh between the
        first measurement read and the request would age every measurement of that message (the former
        `timedatectl` refresh did, by 258-349 ms on the Pi 3 B+, measured 2026-10-13)."""
        self.ntp_synchronized = self.ntp.get()

    def final_check(self) -> None:
        """The end-of-run cross-check (once): called by main() after run() has returned or been interrupted,
        i.e. after the last send, so the `timedatectl` subprocess falls in no reported CPU window. Not run
        after SIGTERM: the process has no handler for it and is killed at once (accepted)."""
        if self.cross_check is None or self._final_checked:
            return
        self._final_checked = True
        self.cross_check.report("end")

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
            if self.ntp_synchronized is None:  # one warning, at startup; no fallback to timedatectl in the loop
                self.warn("ntp_synchronized unavailable (kernel clock state not readable via adjtimex); sent as null until it recovers")
            if self.cross_check is not None:
                self.cross_check.report("start")  # the timedatectl subprocess: before the first cycle's CPU baseline read
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
        cross_check=NtpCrossCheck(),
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
    try:
        sender.final_check()  # after the last send; SIGTERM kills the process before this line (accepted: no handler)
    except KeyboardInterrupt:
        pass
    say(f"done: {sender.stats['ok']} sent ok, {sender.stats['failed']} failed")
    sys.exit(code)


if __name__ == "__main__":
    main()
