"""
Pi telemetry wire protocol, shared by pi/pi_sender.py (on the Raspberry Pi) and
twin/pi_receiver.py (on the laptop).

Standard library only, and written to stay valid Python 3.9 syntax: the Pi
imports this module without `requests`, `python-dotenv` or `psutil`.
Specification: notes/PI_TELEMETRY_SPEC_2026-10-11.md (section numbers below).

Rules that run through every function here:
  * Time is integer epoch UTC milliseconds, never local time.
  * "Omitted is not fresh": a parser that cannot produce a trustworthy value
    returns None, and callers leave the measurement out. Nothing is clamped,
    defaulted to 0.0, or carried over from an earlier read.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

PROTOCOL_VERSION = 1
TELEMETRY_PATH = "/v1/telemetry"
PROBE_PATH = "/v1/probe"
MAX_BODY_BYTES = 8192
MAX_PROBES = 8
MAX_NAME_ECHO = 64

FEATURES = (
    "cpu_utilization",
    "memory_utilization",
    "cpu_temperature_c",
    "cpu_frequency_khz",
)

FEATURE_UNITS = {
    "cpu_utilization": "percent (0-100)",
    "memory_utilization": "percent (0-100)",
    "cpu_temperature_c": "degrees Celsius",
    "cpu_frequency_khz": "kilohertz",
}

# Sanity bounds that reject garbage reads. They are NOT Raspberry Pi limits
# (spec section 5): the soft/hard thermal limits are documented in section 11.
_RANGES = {
    "cpu_utilization": (0.0, 100.0),
    "memory_utilization": (0.0, 100.0),
    "cpu_temperature_c": (-50.0, 150.0),
}
_FREQ_MAX_KHZ = 10_000_000
MAX_INT = 2 ** 53  # every integer on the wire (seq, timestamps, windows) must fit: keeps all arithmetic exact

THERMAL_ZONE_TYPE = "cpu-thermal"  # the only value seen on the device (spec section 2)

_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
_UINT_RE = re.compile(r"[0-9]+")
_INT_RE = re.compile(r"-?[0-9]+")
_NTP_RE = re.compile(r"^\s*System clock synchronized:\s*(yes|no)\s*$", re.MULTILINE)
_TOKEN_RE = re.compile(r"[!-~]+")  # printable ASCII, no spaces: safe in an HTTP header on both ends


class ProtocolError(ValueError):
    """A message or body that must be rejected as a whole (HTTP 400)."""


class CpuTicks(NamedTuple):
    """Aggregate /proc/stat counters: total = user+nice+system+idle+iowait+
    irq+softirq+steal; idle = idle+iowait. Guest columns are excluded because
    the kernel already counts guest time inside user/nice."""

    total: int
    idle: int


class Telemetry(NamedTuple):
    session_id: str
    seq: int
    sent_at_ms: int
    ntp_synchronized: Optional[bool]
    measurements: Dict[str, Dict[str, Any]]
    rejected_features: List[str]
    probes: List[Dict[str, int]]


def _make_time_ns() -> Any:
    """Epoch UTC nanoseconds. On Windows, Python 3.12's time.time_ns() only advances with the
    system timer tick -- 1 ms while some process holds the timer at 1 ms, otherwise 15.6 ms --
    which would quantize every probe timestamp. GetSystemTimePreciseAsFileTime is the same wall
    clock at sub-microsecond resolution (Windows 8+). Elsewhere time.time_ns() is already precise."""
    if os.name == "nt":
        try:
            import ctypes

            precise = ctypes.windll.kernel32.GetSystemTimePreciseAsFileTime
            filetime_to_unix_100ns = 116444736000000000  # 1601-01-01 -> 1970-01-01 in 100 ns units

            def windows_time_ns() -> int:
                ft = ctypes.c_uint64()  # FILETIME = two little-endian uint32 = one uint64
                precise(ctypes.byref(ft))
                return (ft.value - filetime_to_unix_100ns) * 100

            windows_time_ns()  # fail now, not later, if the API is unavailable
            return windows_time_ns
        except (AttributeError, OSError, ImportError):
            pass
    return time.time_ns


time_ns = _make_time_ns()


def now_ms() -> int:
    return time_ns() // 1_000_000


def token_ok(token: str) -> bool:
    """Shared-secret shape: printable ASCII without spaces (it travels in a Bearer header)."""
    return _TOKEN_RE.fullmatch(token) is not None


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and -MAX_INT <= x <= MAX_INT


def _is_uint(x: Any) -> bool:
    return _is_int(x) and x >= 0


# --------------------------------------------------------------------------
# Linux file parsers: text -> value, or None when not trustworthy (section 6)
# --------------------------------------------------------------------------
def parse_proc_stat(text: str) -> Optional[CpuTicks]:
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] != "cpu":  # "cpu0", "intr" etc. are not the aggregate line
            continue
        if len(parts) < 9:  # label + user nice system idle iowait irq softirq steal
            return None
        cols = []
        for raw in parts[1:9]:
            if not _UINT_RE.fullmatch(raw):
                return None
            cols.append(int(raw))
        return CpuTicks(total=sum(cols), idle=cols[3] + cols[4])
    return None


def cpu_percent(prev: Optional[CpuTicks], cur: Optional[CpuTicks]) -> Optional[float]:
    """Busy percent between two reads, or None. busy = total - idle - iowait."""
    if prev is None or cur is None:
        return None
    d_total = cur.total - prev.total
    d_idle = cur.idle - prev.idle
    if d_total <= 0 or d_idle < 0:
        return None
    busy = d_total - d_idle
    if busy < 0:
        return None
    return round(100.0 * busy / d_total, 2)


def parse_meminfo_used_percent(text: str) -> Optional[float]:
    """100*(MemTotal-MemAvailable)/MemTotal. No MemFree fallback: it would
    silently change the definition."""
    values: Dict[str, int] = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep or key not in ("MemTotal", "MemAvailable"):
            continue
        fields = rest.split()
        if not fields or not _UINT_RE.fullmatch(fields[0]):
            return None
        values[key] = int(fields[0])
    if "MemTotal" not in values or "MemAvailable" not in values:
        return None
    total, avail = values["MemTotal"], values["MemAvailable"]
    if total <= 0 or avail > total:
        return None
    return round(100.0 * (total - avail) / total, 2)


def parse_thermal_temp_c(type_text: str, temp_text: str) -> Optional[float]:
    if type_text.strip() != THERMAL_ZONE_TYPE:
        return None
    raw = temp_text.strip()
    if not _INT_RE.fullmatch(raw):
        return None
    return round(int(raw) / 1000.0, 3)


def parse_cpu_freq_khz(text: str) -> Optional[int]:
    raw = text.strip()
    if not _UINT_RE.fullmatch(raw):
        return None
    value = int(raw)
    return value if value > 0 else None


def parse_ntp_synchronized(timedatectl_text: str) -> Optional[bool]:
    match = _NTP_RE.search(timedatectl_text)
    if match is None:
        return None
    return match.group(1) == "yes"


def feature_value_ok(name: str, value: Any) -> bool:
    """True if `value` is an acceptable reading for feature `name` (section 5)."""
    if name not in FEATURES or isinstance(value, bool):
        return False
    if name == "cpu_frequency_khz":
        return _is_int(value) and 0 < value <= _FREQ_MAX_KHZ
    if isinstance(value, float):
        if not math.isfinite(value):
            return False
    elif not isinstance(value, int):
        return False
    low, high = _RANGES[name]
    return low <= value <= high  # int-vs-float comparison is exact: no OverflowError for huge ints


# --------------------------------------------------------------------------
# Four-timestamp offset probe (section 4.1)
# --------------------------------------------------------------------------
def compute_offset_delay(t1: int, t2: int, t3: int, t4: int) -> Tuple[float, int]:
    """t1 = Pi send, t2 = receiver receive, t3 = receiver send, t4 = Pi receive.

    offset = ((t2 - t1) + (t3 - t4)) / 2   RECEIVER clock MINUS Pi clock
             (positive => the receiver's clock is ahead; t_receiver = t_pi + offset)
    delay  = (t4 - t1) - (t3 - t2)         round trip minus receiver processing

    Error of `offset` is (d_forward - d_back) / 2, bounded by +/- delay/2.
    """
    return ((t2 - t1) + (t3 - t4)) / 2, (t4 - t1) - (t3 - t2)


# --------------------------------------------------------------------------
# Bodies and messages
# --------------------------------------------------------------------------
def _reject_constant(name: str) -> Any:
    raise ValueError("non-finite JSON constant " + name)


def parse_json_body(raw: bytes) -> Dict[str, Any]:
    try:
        obj = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ProtocolError("body is not valid UTF-8 JSON") from None
    if not isinstance(obj, dict):
        raise ProtocolError("body must be a JSON object")
    return obj


def _check_version(obj: Any) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        raise ProtocolError("message must be a JSON object")
    version = obj.get("v")
    if not _is_int(version) or version != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    return obj


def validate_probe_request(obj: Any) -> int:
    obj = _check_version(obj)
    t1 = obj.get("t1")
    if not _is_uint(t1):
        raise ProtocolError("t1 must be a non-negative integer")
    return t1


def validate_probe_reply(obj: Any, expected_t1: int) -> Tuple[int, int]:
    obj = _check_version(obj)
    if obj.get("t1") != expected_t1 or not _is_int(obj.get("t1")):
        raise ProtocolError("probe reply does not echo t1")
    t2, t3 = obj.get("t2"), obj.get("t3")
    if not _is_uint(t2) or not _is_uint(t3):
        raise ProtocolError("t2 and t3 must be non-negative integers")
    return t2, t3


def _validate_measurement(name: str, entry: Any) -> Optional[Dict[str, Any]]:
    """Return the cleaned entry, or None when this one measurement is unusable."""
    if not isinstance(entry, dict):
        return None
    if not feature_value_ok(name, entry.get("value")):
        return None
    if not _is_uint(entry.get("measured_at_ms")):
        return None
    clean: Dict[str, Any] = {"value": entry["value"], "measured_at_ms": entry["measured_at_ms"]}
    if name == "cpu_utilization" and "window_ms" in entry:
        window = entry["window_ms"]
        if not _is_int(window) or window < 1:
            return None
        clean["window_ms"] = window
    return clean


def validate_telemetry(obj: Any) -> Telemetry:
    obj = _check_version(obj)
    session_id = obj.get("session_id")
    if not isinstance(session_id, str) or _SESSION_ID_RE.fullmatch(session_id) is None:
        raise ProtocolError("bad session_id")
    seq = obj.get("seq")
    if not _is_uint(seq):
        raise ProtocolError("seq must be a non-negative integer")
    sent_at_ms = obj.get("sent_at_ms")
    if not _is_uint(sent_at_ms):
        raise ProtocolError("sent_at_ms must be a non-negative integer")
    ntp = obj.get("ntp_synchronized")
    if ntp is not None and not isinstance(ntp, bool):
        raise ProtocolError("ntp_synchronized must be true, false or null")
    raw_measurements = obj.get("measurements")
    if not isinstance(raw_measurements, dict):
        raise ProtocolError("measurements must be an object")

    probes: List[Dict[str, int]] = []
    raw_probes = obj.get("probes", [])
    if not isinstance(raw_probes, list) or len(raw_probes) > MAX_PROBES:
        raise ProtocolError("probes must be a list of at most %d" % MAX_PROBES)
    for entry in raw_probes:
        if not isinstance(entry, dict) or not all(_is_uint(entry.get(k)) for k in ("t1", "t2", "t3", "t4")):
            raise ProtocolError("each probe needs integer t1..t4")
        probes.append({k: entry[k] for k in ("t1", "t2", "t3", "t4")})

    measurements: Dict[str, Dict[str, Any]] = {}
    rejected: List[str] = []
    for name, entry in raw_measurements.items():
        clean = _validate_measurement(name, entry) if name in FEATURES else None
        if clean is None:
            rejected.append(str(name)[:MAX_NAME_ECHO])
        else:
            measurements[name] = clean

    return Telemetry(
        session_id=session_id,
        seq=seq,
        sent_at_ms=sent_at_ms,
        ntp_synchronized=ntp,
        measurements=measurements,
        rejected_features=rejected,
        probes=probes,
    )
