#!/usr/bin/env python3
"""
Independent reference logger for the accuracy protocol (notes/PI_ACCURACY_PROTOCOL_*.md). Linux only,
standard library only, Python 3.9 syntax. Run on the Pi next to the sender.

    python3 pi/reference_logger.py --out reference_<name>.jsonl [--duration-s 3600]

INDEPENDENCE. This file does not import pi_sender, twin.pi_protocol or anything from twin/: it has its own
reading and parsing code and logs RAW values only (cumulative counters, kB fields, millidegrees), never a
derived percentage. A test asserts that it imports neither the sender nor the shared protocol module. The
analysis (scripts/analyze_accuracy.py) is the only place a percentage is computed from these raw values.

WHAT IT DOES AND DOES NOT PROVIDE.
  * /proc/stat and /proc/meminfo are the SAME kernel files the sender reads; only the code that reads and
    stamps them is independent. They check the sender's windowing, timestamps and arithmetic, not the
    kernel's counters.
  * Frequency: cpuinfo_cur_freq is a DIFFERENT sysfs file from the sender's scaling_cur_freq, served by the
    same cpufreq driver (whether it reads the hardware or a cached value is not verified here; it may be
    readable by root only, in which case the field is null with an error string). `vcgencmd measure_clock
    arm` goes through the firmware: a different software path, at 0.1 Hz. Whether it is independent of the
    sysfs path is not verified here.
  * Temperature: thermal_zone0/temp is the SAME file the sender reads. `vcgencmd measure_temp` is a different
    software path to the same sensor, at 0.1 Hz. Neither says anything about the sensor's absolute accuracy.

RECORDS (JSONL, one object per line, flushed per line, fsync every 10 lines and at the end; created
exclusively, never overwritten). Times are the Pi clock: t_epoch_ms (wall) and t_mono_ns (monotonic).
  start   interval_s, vcgencmd_every_s, vcgencmd_available, sources (the paths read)
  sample  seq; t_epoch_ms/t_mono_ns taken IMMEDIATELY AFTER the /proc/stat read (the instant of the
          counters); t_done_mono_ns after the last source; process_time_s (this process's CPU time);
          proc_stat {cpu, cpu0, ...: every field of each cpu line, cumulative ticks} + proc_stat_error;
          meminfo_kib {every key: the number as printed (kB; HugePages_* are counts)} + meminfo_error;
          cpuinfo_cur_freq_khz + cpuinfo_cur_freq_error; thermal_zone0_type; thermal_zone0_temp_millic +
          thermal_error; vcgencmd_calls (cumulative); vcgencmd {arm_clock_raw, arm_clock_hz,
          arm_clock_error, temp_raw, temp_c, temp_error} on the samples where it ran (first sample, then
          every vcgencmd_every_s).
  end     reason (completed | SIGINT | SIGTERM | SIGHUP), samples, vcgencmd_runs, vcgencmd_calls_total
A source that is missing or unreadable gives null plus an error string, never a crash.

STALLS. A hung `vcgencmd` (two calls, 3 s timeout each) can delay one sample by up to 6 s; the grid then
re-anchors and the reference has a gap there, which the analyzer reports as a wider interpolation gap.
Stop it with Ctrl+C, SIGTERM or SIGHUP (a dropped SSH session); under tmux or nohup it survives the latter.

OWN COST. The CPU cost of this logger is UNMEASURED. Every sample record carries its process_time_s so that
the cost can be assessed afterwards, and the vcgencmd subprocess (every 10 s) adds CPU in that second on top
of it. It runs on the machine being measured, so it perturbs what it observes by an amount not known here.

Nothing in this file has run on the Pi: the sysfs paths, permissions and vcgencmd behaviour there are
UNVERIFIED.

Exit codes: 0 finished; 2 bad arguments, not Linux, or the output file already exists.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional

PROC_STAT = "/proc/stat"
PROC_MEMINFO = "/proc/meminfo"
CPUINFO_CUR_FREQ = "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq"  # the sender reads scaling_cur_freq
THERMAL_TYPE = "/sys/class/thermal/thermal_zone0/type"
THERMAL_TEMP = "/sys/class/thermal/thermal_zone0/temp"  # the same file the sender reads
FSYNC_EVERY = 10
_MEMINFO_LINE = re.compile(r"^([A-Za-z_()0-9]+):\s+(\d+)(?:\s*kB)?\s*$")
_VCG_NUMBER = re.compile(r"=\s*([-+]?\d+(?:\.\d+)?)")


class Env(NamedTuple):
    now_ms: Callable[[], int]
    mono_ns: Callable[[], int]
    sleep: Callable[[float], None]
    process_time: Callable[[], float]
    read_text: Callable[[str], str]
    run_vcgencmd: Callable[[List[str]], str]
    vcgencmd_path: Optional[str]


def _err(exc: BaseException) -> str:
    return "%s: %s" % (type(exc).__name__, exc)


def parse_proc_stat(text: str) -> Dict[str, List[int]]:
    out: Dict[str, List[int]] = {}
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0].startswith("cpu"):
            try:
                out[parts[0]] = [int(p) for p in parts[1:]]
            except ValueError:
                continue
    if "cpu" not in out or len(out["cpu"]) < 8:
        raise ValueError("no aggregate cpu line with at least 8 integer fields")
    return out


def parse_meminfo(text: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for line in text.splitlines():
        match = _MEMINFO_LINE.match(line)
        if match:
            out[match.group(1)] = int(match.group(2))
    if "MemTotal" not in out or "MemAvailable" not in out:
        raise ValueError("MemTotal or MemAvailable missing")
    return out


def open_exclusive(path: Any):
    """Create the file; raises FileExistsError instead of overwriting."""
    return open(path, "x", encoding="utf-8")


class ReferenceLogger:
    def __init__(self, handle: Any, env: Env, *, fsync: Callable[[int], None] = os.fsync,
                 interval_s: float = 1.0, vcgencmd_every_s: float = 10.0):
        self.handle, self.env, self.fsync = handle, env, fsync
        self.interval_s, self.vcgencmd_every_s = interval_s, vcgencmd_every_s
        self._lines = 0
        self._stop: Optional[str] = None
        self._vcg_calls = 0
        self._vcg_runs = 0

    def request_stop(self, reason: str) -> None:
        """Safe to call from a signal handler. The first reason wins."""
        if self._stop is None:
            self._stop = reason

    # -- output --------------------------------------------------------------
    def _write(self, record: Dict[str, Any]) -> None:
        self.handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.handle.flush()
        self._lines += 1
        if self._lines % FSYNC_EVERY == 0:
            self._sync()

    def _sync(self) -> None:
        try:
            self.fsync(self.handle.fileno())
        except (OSError, ValueError, AttributeError):
            pass

    # -- sources -------------------------------------------------------------
    def _read(self, path: str) -> Any:
        """(text, None) or (None, error string)."""
        try:
            return self.env.read_text(path), None
        except Exception as exc:  # a missing or unreadable source is data, not a crash
            return None, _err(exc)

    def _vcgencmd(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for label, args in (("arm_clock", ["measure_clock", "arm"]), ("temp", ["measure_temp"])):
            raw, parsed, error = None, None, None
            self._vcg_calls += 1
            try:
                raw = self.env.run_vcgencmd(args).strip()
                match = _VCG_NUMBER.search(raw)
                if match is None:
                    error = "unparsable vcgencmd output"
                else:
                    parsed = int(float(match.group(1))) if label == "arm_clock" else float(match.group(1))
            except Exception as exc:
                error = _err(exc)
            out[label + "_raw"] = raw
            out["arm_clock_hz" if label == "arm_clock" else "temp_c"] = parsed
            out[label + "_error"] = error
        self._vcg_runs += 1
        return out

    def _sample(self, seq: int, vcg_due: bool) -> Dict[str, Any]:
        text, stat_err = self._read(PROC_STAT)
        t_epoch_ms, t_mono_ns = self.env.now_ms(), self.env.mono_ns()  # right after the counters were read
        record: Dict[str, Any] = {"type": "sample", "seq": seq, "t_epoch_ms": t_epoch_ms, "t_mono_ns": t_mono_ns}
        stat = None
        if text is not None:
            try:
                stat = parse_proc_stat(text)
            except ValueError as exc:
                stat_err = "unparsable /proc/stat: %s" % exc
        record["proc_stat"], record["proc_stat_error"] = stat, stat_err

        text, mem_err = self._read(PROC_MEMINFO)
        mem = None
        if text is not None:
            try:
                mem = parse_meminfo(text)
            except ValueError as exc:
                mem_err = "unparsable /proc/meminfo: %s" % exc
        record["meminfo_kib"], record["meminfo_error"] = mem, mem_err

        text, freq_err = self._read(CPUINFO_CUR_FREQ)
        freq = None
        if text is not None:
            try:
                freq = int(text.strip())
            except ValueError:
                freq_err = "unparsable cpuinfo_cur_freq: %r" % text.strip()[:40]
        record["cpuinfo_cur_freq_khz"], record["cpuinfo_cur_freq_error"] = freq, freq_err

        type_text, type_err = self._read(THERMAL_TYPE)
        temp_text, temp_err = self._read(THERMAL_TEMP)
        temp = None
        if temp_text is not None:
            try:
                temp = int(temp_text.strip())
            except ValueError:
                temp_err = "unparsable thermal temp: %r" % temp_text.strip()[:40]
        errors = [e for e in (type_err, temp_err) if e]
        record["thermal_zone0_type"] = type_text.strip() if type_text is not None else None
        record["thermal_zone0_temp_millic"] = temp
        record["thermal_error"] = "; ".join(errors) if errors else None

        if vcg_due:
            record["vcgencmd"] = self._vcgencmd()
        record["vcgencmd_calls"] = self._vcg_calls
        record["t_done_mono_ns"] = self.env.mono_ns()
        record["process_time_s"] = self.env.process_time()
        return record

    # -- the run -------------------------------------------------------------
    def run(self, count: Optional[int] = None, duration_s: Optional[float] = None) -> str:
        env = self.env
        interval_ns = int(self.interval_s * 1e9)
        vcg_every_ns = int(self.vcgencmd_every_s * 1e9)
        vcg_available = env.vcgencmd_path is not None
        start = env.mono_ns()
        self._write({"type": "start", "format": 1, "t_epoch_ms": env.now_ms(), "t_mono_ns": start,
                     "interval_s": self.interval_s, "vcgencmd_every_s": self.vcgencmd_every_s,
                     "vcgencmd_available": vcg_available,
                     "sources": {"proc_stat": PROC_STAT, "meminfo": PROC_MEMINFO, "cpuinfo_cur_freq": CPUINFO_CUR_FREQ,
                                 "thermal_type": THERMAL_TYPE, "thermal_temp": THERMAL_TEMP,
                                 "vcgencmd": env.vcgencmd_path}})
        next_ns, next_vcg_ns, seq = start, start, 0
        try:
            while self._stop is None:
                if count is not None and seq >= count:
                    break
                if duration_s is not None and env.mono_ns() - start >= int(duration_s * 1e9):
                    break
                began = env.mono_ns()
                due = vcg_available and began >= next_vcg_ns
                if due:
                    next_vcg_ns = max(next_vcg_ns + vcg_every_ns, began + 1)
                self._write(self._sample(seq, due))
                seq += 1
                next_ns += interval_ns
                if next_ns <= began:  # this sample began after its slot: skip the missed slots, never two samples closer than the interval
                    next_ns = began + interval_ns
                now = env.mono_ns()
                if next_ns > now:
                    env.sleep((next_ns - now) / 1e9)
                else:  # the sample itself overran its slot: the next one follows at once, still a full duration later
                    next_ns = now
            return self._stop or "completed"
        finally:
            self._write({"type": "end", "reason": self._stop or "completed", "t_epoch_ms": env.now_ms(),
                         "t_mono_ns": env.mono_ns(), "samples": seq, "vcgencmd_runs": self._vcg_runs,
                         "vcgencmd_calls_total": self._vcg_calls})
            self._sync()


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------
def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _run_vcgencmd(path: str, args: List[str]) -> str:
    return subprocess.run([path] + args, capture_output=True, text=True, timeout=3, check=True).stdout


def real_env() -> Env:
    path = shutil.which("vcgencmd")
    return Env(
        now_ms=lambda: time.time_ns() // 1_000_000,
        mono_ns=time.monotonic_ns,
        sleep=time.sleep,
        process_time=time.process_time,
        read_text=_read_text,
        run_vcgencmd=lambda args: _run_vcgencmd(path or "vcgencmd", args),
        vcgencmd_path=path,
    )


def _positive(value: str) -> float:
    number = float(value)
    if not number > 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def main(argv: Optional[List[str]] = None, *, env: Optional[Env] = None) -> int:
    p = argparse.ArgumentParser(prog="reference_logger", description="Independent raw-value reference logger (Linux only).")
    p.add_argument("--out", required=True, help="JSONL output; created exclusively, never overwritten")
    p.add_argument("--interval-s", type=_positive, default=1.0, help="sample interval (default 1)")
    p.add_argument("--vcgencmd-every-s", type=_positive, default=10.0, help="vcgencmd cadence (default 10)")
    p.add_argument("--duration-s", type=_positive, default=None, help="stop after this long (default: until Ctrl+C or SIGTERM)")
    args = p.parse_args(argv)
    injected = env is not None
    if env is None:
        if not sys.platform.startswith("linux"):
            print("ERROR the reference logger runs on Linux only (/proc, /sys)", file=sys.stderr)
            return 2
        env = real_env()
    out = Path(args.out)
    if out.exists():
        print("ERROR the output file exists and will not be overwritten: %s" % out, file=sys.stderr)
        return 2
    try:
        handle = open_exclusive(out)
    except FileExistsError:
        print("ERROR the output file exists and will not be overwritten: %s" % out, file=sys.stderr)
        return 2
    logger = ReferenceLogger(handle, env, interval_s=args.interval_s, vcgencmd_every_s=args.vcgencmd_every_s)
    previous = {}
    if not injected:
        for sig in [signal.SIGINT, signal.SIGTERM] + ([signal.SIGHUP] if hasattr(signal, "SIGHUP") else []):
            previous[sig] = signal.signal(sig, lambda signum, frame: logger.request_stop(signal.Signals(signum).name))
    try:
        reason = logger.run(duration_s=args.duration_s)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        handle.close()
    print("reference logger finished: %s (log %s)" % (reason, out), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
