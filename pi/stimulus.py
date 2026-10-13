#!/usr/bin/env python3
"""
Pi-side load generator for the accuracy protocol (notes/PI_ACCURACY_PROTOCOL_*.md). Linux only, standard
library only, no root, no network. Python 3.9 syntax.

    python3 pi/stimulus.py --schedule pi/stimulus_schedule.example.json --print-plan
    python3 pi/stimulus.py --schedule SCHEDULE.json --out stimulus_<name>.jsonl [--shuffle-seed N]

WHAT IT IS FOR. It gives two of the four quantities a ground truth that exists by construction:
  * CPU: N worker processes (default 4), each pinned to one core with os.sched_setaffinity and niced +10,
    each spinning `level` percent of every 100 ms period (monotonic clock) and sleeping the rest; level 100
    never sleeps. `level` is therefore the percent of TOTAL capacity only if the machine has as many cores as
    workers (the log records n_cpus and the pinning that actually succeeded). The log also records the duty
    each worker really achieved (its process_time delta over the wall delta), because nominal is not truth.
  * Memory: a bytearray of `level` MiB with one byte written per 4096-byte page so that it is resident, held
    for the phase. The log records VmRSS from /proc/self/status at mid-phase.
Frequency and temperature get no stimulus truth: this tool does not control them.

WHAT IT DOES NOT SHOW. Nothing about the sensors' absolute accuracy is established here: this only produces
a known load. Other processes (the sender, the reference logger, the OS) add CPU and memory on top of it,
and the workers' own cost is part of the measured system. The achieved duty is measured with a resolution of
one 100 ms period at the phase edges.

SAFETY (first-class).
  * A mem phase is refused unless MemAvailable minus the request leaves at least --reserve-mib (200) MiB.
    The whole schedule is checked before anything starts, and each mem phase is checked again when it is
    about to start (memory may have been used by something else meanwhile); an unreadable /proc/meminfo
    refuses mem phases instead of guessing.
  * Hard cap on the total schedule duration (HARD_MAX_TOTAL_S); --max-total-s can only lower it.
  * SIGINT, SIGTERM and SIGHUP (a dropped SSH session) stop the workers, free the memory and write the end
    event. They are noticed within one 0.2 s sleep slice, EXCEPT while memory is being allocated: the
    allocation is not interruptible, so a signal during a large allocation waits for it to finish. A worker
    also exits by itself if its parent disappears, so a killed parent cannot leave busy loops behind. A second
    signal does not force anything; only SIGKILL does. Run it under tmux or nohup for a long schedule.
  * Thermal guard (on by default): during CPU phases the SoC temperature is read about once a second and
    the run stops at --max-temp-c (default 80; it cannot be set higher). 80 C is where the Pi 3 B+ starts
    progressive throttling of the ARM cores; its soft limit, where the clock drops from 1.4 to 1.2 GHz, is 60 C
    (spec section 11). This is a safety stop, not a measurement threshold. If the temperature cannot be read
    at the start, CPU phases are refused unless --no-thermal-guard is given; if it becomes unreadable during
    a CPU phase, a warning event is logged once and the guard is blind from then on.
  * The safety values can only be made stricter from the command line: --reserve-mib cannot go below 200,
    --max-temp-c cannot go above 80, --workers is limited to 64, and --max-total-s must be finite.
  * The output file is created exclusively and never overwritten.

EVENT LOG (JSONL, one object per line, flushed per line). Times are the Pi clock: t_epoch_ms (wall) and
t_mono_ns (monotonic).
  start        seed, order (phase names), n_workers, n_cpus, pinned_cpus, total_s, reserve_mib, max_temp_c,
               vmrss_kib_baseline
  phase_start  index, name, kind, level, duration_s (a mem phase is stamped BEFORE its allocation; the hold of
               duration_s starts when the allocation is done)
  phase_end    index, name, kind, level, status (completed | interrupted | error), wall_s,
               worker_duty (each worker's fraction of the phase; null without workers),
               achieved_total_percent (all workers' CPU time over wall time times n_cpus: a percent of ALL cpus,
               like /proc/stat's aggregate; null without workers), and for mem phases vmrss_kib_mid,
               mem_alloc_mib, mem_alloc_wall_s (how long the memory took to become resident) and
               mem_available_kib_at_start
  phase_refused  index, name, reason
  warning      message (e.g. the thermal guard went blind)
  end          reason (completed | SIGINT | SIGTERM | SIGHUP | thermal-limit | refused | error), phases_completed

Exit codes: 0 completed; 1 stopped early; 2 bad arguments, schedule, platform or an existing output file;
3 a safety refusal.

Nothing in this file has run on the Pi: affinity, nice and the worker behaviour there are UNVERIFIED.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import random
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, NamedTuple, Optional

KINDS = ("idle", "cpu", "mem")
PERIOD_S = 0.1            # one duty-cycle period of a worker
SLICE_S = 0.2             # longest uninterrupted sleep, so a stop request is seen within this long
NICE_INCREMENT = 10
PAGE_BYTES = 4096
MIB = 1024 * 1024
DEFAULT_WORKERS = 4
MAX_WORKERS = 64
DEFAULT_RESERVE_MIB = 200
DEFAULT_MAX_TEMP_C = 80.0
HARD_MAX_TOTAL_S = 7200   # two hours: no schedule may exceed this, whatever the command line says
MAX_PHASES = 500
THERMAL_CHECK_S = 1.0


class ScheduleError(ValueError):
    """The schedule file is not acceptable."""


class SafetyRefusal(Exception):
    """A phase would be unsafe; nothing was started."""


class Phase(NamedTuple):
    name: str
    kind: str
    level: float
    duration_s: float
    group: Optional[str] = None
    fixed: bool = False


class Env(NamedTuple):
    """Everything that touches the machine, injectable so the logic runs without a Pi."""

    now_ms: Callable[[], int]
    mono_ns: Callable[[], int]
    sleep: Callable[[float], None]
    read_mem_available_kib: Callable[[], Optional[int]]
    read_vmrss_kib: Callable[[], Optional[int]]
    read_temp_c: Callable[[], Optional[float]]
    alloc: Callable[[int], Any]
    release: Callable[[Any], None]
    make_pool: Callable[[int], Any]
    n_cpus: int


# --------------------------------------------------------------------------
# Schedule
# --------------------------------------------------------------------------
def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def parse_schedule(obj: Any) -> List[Phase]:
    if not isinstance(obj, dict) or not isinstance(obj.get("phases"), list) or not obj["phases"]:
        raise ScheduleError('the schedule must be an object {"phases": [...]} with at least one phase')
    if len(obj["phases"]) > MAX_PHASES:
        raise ScheduleError("more than %d phases" % MAX_PHASES)
    out: List[Phase] = []
    seen = set()
    for i, raw in enumerate(obj["phases"]):
        where = "phase %d" % (i + 1)
        if not isinstance(raw, dict):
            raise ScheduleError(where + ": not an object")
        name, kind, level, duration = raw.get("name"), raw.get("kind"), raw.get("level"), raw.get("duration_s")
        if not isinstance(name, str) or not name:
            raise ScheduleError(where + ": name must be a non-empty string")
        if name in seen:
            raise ScheduleError(where + ": duplicate name %r" % name)
        seen.add(name)
        if kind not in KINDS:
            raise ScheduleError("%s (%s): kind must be one of %s" % (where, name, ", ".join(KINDS)))
        if not _is_number(level):
            raise ScheduleError("%s (%s): level must be a number" % (where, name))
        if kind == "cpu" and not 0 <= level <= 100:
            raise ScheduleError("%s (%s): a cpu level is a percent between 0 and 100" % (where, name))
        if kind == "mem" and not level > 0:
            raise ScheduleError("%s (%s): a mem level is a positive number of MiB" % (where, name))
        if not _is_number(duration) or not duration > 0:
            raise ScheduleError("%s (%s): duration_s must be a number greater than 0" % (where, name))
        group = raw.get("group")
        if group is not None and not isinstance(group, str):
            raise ScheduleError("%s (%s): group must be a string" % (where, name))
        out.append(Phase(name, kind, level, duration, group, bool(raw.get("fixed", False))))
    return out


def load_schedule(path: Any) -> List[Phase]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return parse_schedule(json.load(handle))
    except (OSError, ValueError) as exc:
        if isinstance(exc, ScheduleError):
            raise
        raise ScheduleError("cannot read the schedule %s: %s" % (path, exc))


def total_duration(plan: List[Phase]) -> float:
    return sum(p.duration_s for p in plan)


def check_total(plan: List[Phase], max_total_s: Optional[float]) -> None:
    if max_total_s is not None:
        if not math.isfinite(max_total_s) or max_total_s <= 0:
            raise ScheduleError("--max-total-s must be a finite number greater than 0")
        if max_total_s > HARD_MAX_TOTAL_S:
            raise ScheduleError("--max-total-s cannot exceed the hard cap of %d s" % HARD_MAX_TOTAL_S)
    limit = HARD_MAX_TOTAL_S if max_total_s is None else max_total_s
    total = total_duration(plan)
    if not total <= limit:  # written so that a NaN total fails too
        raise ScheduleError("the schedule lasts %.0f s, more than the allowed %.0f s" % (total, limit))


def order_phases(plan: List[Phase], seed: Optional[int]) -> List[Phase]:
    """Seeded shuffle. Consecutive phases sharing a `group` move together (an activity and its recovery
    phase); a phase marked `fixed` keeps its position (and its group's). Without a seed: as written."""
    if seed is None:
        return list(plan)
    units: List[List[Phase]] = []
    for p in plan:
        if units and p.group is not None and units[-1][0].group == p.group:
            units[-1].append(p)
        else:
            units.append([p])
    movable = [u for u in units if not u[0].fixed]
    random.Random(seed).shuffle(movable)
    pick = iter(movable)
    return [p for u in units for p in (u if u[0].fixed else next(pick))]


def mem_verdict(available_kib: Optional[int], request_mib: float, reserve_mib: float) -> Optional[str]:
    """None if the request is allowed, else the reason it is refused."""
    if available_kib is None:
        return "MemAvailable cannot be read, so the request cannot be checked"
    left_mib = available_kib / 1024.0 - request_mib
    if left_mib < reserve_mib:
        return "MemAvailable is %.0f MiB, so %.0f MiB would leave %.0f MiB, under the %.0f MiB reserve" % (
            available_kib / 1024.0, request_mib, left_mib, reserve_mib)
    return None


def plan_text(plan: List[Phase], seed: Optional[int], env: Env, reserve_mib: float, workers: int) -> str:
    lines = ["stimulus plan (%s): %d phases" % ("seed %d" % seed if seed is not None else "no shuffle", len(plan))]
    available = env.read_mem_available_kib()
    for i, p in enumerate(plan, 1):
        if p.kind == "cpu":
            what = ("cpu  %g %% per worker, %d workers (= %g %% of total capacity only if the machine has %d cpus; "
                    "this machine has %d cpus)" % (p.level, workers, p.level, workers, env.n_cpus))
        elif p.kind == "mem":
            what = "mem  %g MiB" % p.level
        else:
            what = "idle"
        line = "  %2d. %-12s %s, %g s" % (i, p.name, what, p.duration_s)
        if p.kind == "mem":
            if available is None:
                line += "   [memory check unavailable here]"
            else:
                reason = mem_verdict(available, p.level, reserve_mib)
                line += "   [memory check against MemAvailable now: " + ("ok" if reason is None else "WOULD BE REFUSED: " + reason) + "]"
        lines.append(line)
    total = total_duration(plan)
    lines.append("total %g s (%.1f min); hard cap %d s" % (total, total / 60.0, HARD_MAX_TOTAL_S))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Workers
# --------------------------------------------------------------------------
def busy_seconds(level: float, period_s: float) -> float:
    return period_s * max(0.0, min(100.0, level)) / 100.0


def sleep_seconds(level: float, period_s: float) -> float:
    """level 100 never sleeps."""
    return 0.0 if level >= 100 else period_s - busy_seconds(level, period_s)


def worker_should_exit(parent_pid: int, current_ppid: int, stop_set: bool) -> bool:
    return stop_set or current_ppid != parent_pid


def assign_cpus(n_workers: int, allowed: List[int]) -> List[int]:
    """One core per worker, round-robin over the CPUs this process may use (doubled up if there are fewer)."""
    return [allowed[i % len(allowed)] for i in range(n_workers)]


def wait_until_reported(read: Callable[[], List[int]], sleep: Callable[[float], None], mono: Callable[[], float],
                        timeout_s: float = 2.0) -> List[int]:
    """Poll `read()` until no worker is still at -2 ("not reported yet"), or until the timeout; returns the last
    reading. The workers are forked processes and take a moment to pin themselves; logging earlier would record
    "not reported" for workers that did pin."""
    start = mono()
    while True:
        pins = read()
        if -2 not in pins or mono() - start >= timeout_s:
            return pins
        sleep(0.01)


def _worker_main(index: int, cpu_id: int, nice_increment: int, level: Any, stop: Any, cpu_seconds: Any,
                 pinned: Any, period_s: float, parent_pid: int) -> None:  # pragma: no cover - runs in the child
    signal.signal(signal.SIGTERM, signal.SIG_DFL)  # the parent's flag-setting handler was inherited by fork: terminate() must work
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C reaches the whole process group; the parent decides
    try:
        os.sched_setaffinity(0, {cpu_id})
        pinned.value = cpu_id
    except (AttributeError, OSError):
        pinned.value = -1
    try:
        os.nice(nice_increment)
    except (AttributeError, OSError):
        pass
    while not worker_should_exit(parent_pid, os.getppid(), stop.is_set()):
        lvl = level.value
        start = time.monotonic()
        busy_until = start + busy_seconds(lvl, period_s)
        while time.monotonic() < busy_until:
            pass
        rest = sleep_seconds(lvl, period_s)
        if lvl >= 100:
            while time.monotonic() < start + period_s:  # level 100: no sleep at all
                pass
        elif rest > 0:
            time.sleep(max(0.0, start + period_s - time.monotonic()))
        cpu_seconds.value = time.process_time()


class ProcessPool:  # pragma: no cover - needs a real Linux
    """N forked workers sharing a level and reporting their CPU time through shared memory."""

    def __init__(self, n_workers: int, period_s: float = PERIOD_S):
        import multiprocessing

        ctx = multiprocessing.get_context("fork")
        self.n_workers = n_workers
        self._stop = ctx.Event()
        self._level = ctx.Value("d", 0.0)
        self._cpu = [ctx.Value("d", 0.0) for _ in range(n_workers)]
        self._pinned = [ctx.Value("i", -2) for _ in range(n_workers)]
        cpus = assign_cpus(n_workers, sorted(os.sched_getaffinity(0)))
        self._procs = [ctx.Process(target=_worker_main, daemon=True,
                                   args=(i, cpus[i], NICE_INCREMENT, self._level, self._stop, self._cpu[i],
                                         self._pinned[i], period_s, os.getpid()))
                       for i in range(n_workers)]
        try:
            for proc in self._procs:
                proc.start()
        except BaseException:  # e.g. fork failed for the 3rd worker: stop the ones already running before re-raising
            self._stop.set()
            for proc in self._procs:
                if proc.pid is not None:
                    proc.join(1.0)
                    if proc.is_alive():
                        proc.kill()
                        proc.join(1.0)
            raise

    def set_level(self, percent: float) -> None:
        self._level.value = float(percent)

    def cpu_seconds(self) -> List[float]:
        return [v.value for v in self._cpu]

    def pinned_cpus(self) -> List[int]:
        """The core each worker actually got (-1: pinning failed, -2: still not reported after the wait)."""
        return wait_until_reported(lambda: [v.value for v in self._pinned], time.sleep, time.monotonic)

    def stop(self) -> None:
        self._stop.set()
        for proc in self._procs:
            proc.join(2.0)
        for proc in self._procs:
            if proc.is_alive():
                proc.terminate()
                proc.join(2.0)
            if proc.is_alive():
                proc.kill()
                proc.join(2.0)


# --------------------------------------------------------------------------
# Machine access
# --------------------------------------------------------------------------
def _read_kib(path: str, key: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith(key + ":"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _read_temp_c() -> Optional[float]:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r", encoding="utf-8") as handle:
            return int(handle.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def alloc_resident(nbytes: int) -> bytearray:
    """A zero-filled buffer with one byte written per page, so every page is really resident."""
    buf = bytearray(nbytes)  # CPython zero-fills it (memset), so it is already resident; this call is not interruptible
    for offset in range(0, nbytes, PAGE_BYTES):  # one byte per page: makes residency explicit, whatever the allocator did
        buf[offset] = 1
    return buf


def real_env() -> Env:
    try:
        n_cpus = len(os.sched_getaffinity(0))
    except AttributeError:
        n_cpus = os.cpu_count() or 1
    return Env(
        now_ms=lambda: time.time_ns() // 1_000_000,
        mono_ns=time.monotonic_ns,
        sleep=time.sleep,
        read_mem_available_kib=lambda: _read_kib("/proc/meminfo", "MemAvailable"),
        read_vmrss_kib=lambda: _read_kib("/proc/self/status", "VmRSS"),
        read_temp_c=_read_temp_c,
        alloc=alloc_resident,
        release=lambda buf: buf.clear(),
        make_pool=ProcessPool,
        n_cpus=n_cpus,
    )


def open_exclusive(path: Any):
    """Create the file; raises FileExistsError instead of overwriting."""
    return open(path, "x", encoding="utf-8")


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------
class Stimulus:
    def __init__(self, plan: List[Phase], emit: Callable[[Dict[str, Any]], None], env: Env, *, seed: Optional[int],
                 workers: int = DEFAULT_WORKERS, reserve_mib: float = DEFAULT_RESERVE_MIB,
                 max_temp_c: float = DEFAULT_MAX_TEMP_C, thermal_guard: bool = True):
        self.plan, self.emit, self.env, self.seed = plan, emit, env, seed
        self.workers, self.reserve_mib = workers, reserve_mib
        self.max_temp_c, self.thermal_guard = max_temp_c, thermal_guard
        self._stop_reason: Optional[str] = None
        self._thermal_blind = False
        self._pool: Any = None
        self._buf: Any = None

    # -- control -------------------------------------------------------------
    def request_stop(self, reason: str) -> None:
        """Safe to call from a signal handler. The first reason wins."""
        if self._stop_reason is None:
            self._stop_reason = reason

    def _stamp(self, event: Dict[str, Any]) -> Dict[str, Any]:
        event["t_epoch_ms"] = self.env.now_ms()
        event["t_mono_ns"] = self.env.mono_ns()
        return event

    # -- safety --------------------------------------------------------------
    def preflight(self) -> List[str]:
        refusals = []
        available = self.env.read_mem_available_kib()
        for p in self.plan:
            if p.kind == "mem":
                reason = mem_verdict(available, p.level, self.reserve_mib)
                if reason is not None:
                    refusals.append("phase %s: %s" % (p.name, reason))
        if self.thermal_guard and any(p.kind == "cpu" for p in self.plan) and self.env.read_temp_c() is None:
            refusals.append("the SoC temperature cannot be read, so the thermal guard cannot work "
                            "(use --no-thermal-guard to run CPU phases anyway)")
        return refusals

    # -- phases --------------------------------------------------------------
    def _wait(self, seconds: float, mid: Optional[Callable[[], None]], watch_temp: bool) -> None:
        start = self.env.mono_ns()
        deadline = start + int(seconds * 1e9)
        mid_at = start + int(seconds * 1e9 / 2)
        next_temp = start
        while self._stop_reason is None:
            now = self.env.mono_ns()
            if now >= deadline:
                return
            if mid is not None and now >= mid_at:
                mid()
                mid = None
            if watch_temp and now >= next_temp:
                temp = self.env.read_temp_c()
                if temp is None and not self._thermal_blind:
                    self._thermal_blind = True
                    self.emit(self._stamp({"type": "warning", "message": "the temperature became unreadable during a cpu phase: "
                                                                          "the thermal guard is blind from now on"}))
                if temp is not None and temp >= self.max_temp_c:
                    self.request_stop("thermal-limit")
                    return
                next_temp = now + int(THERMAL_CHECK_S * 1e9)
            self.env.sleep(min(SLICE_S, max(0.0, (deadline - now) / 1e9)))

    def _run_phase(self, index: int, p: Phase) -> bool:
        """Returns True if the phase ran to its end."""
        available = self.env.read_mem_available_kib() if p.kind == "mem" else None
        if p.kind == "mem":
            reason = mem_verdict(available, p.level, self.reserve_mib)
            if reason is not None:
                self.emit(self._stamp({"type": "phase_refused", "index": index, "name": p.name, "reason": reason}))
                self.request_stop("refused")
                return False
        self.emit(self._stamp({"type": "phase_start", "index": index, "name": p.name, "kind": p.kind,
                               "level": p.level, "duration_s": p.duration_s}))
        t0 = self.env.mono_ns()
        before = self._pool.cpu_seconds() if self._pool is not None else None
        extra: Dict[str, Any] = {"vmrss_kib_mid": None, "mem_alloc_mib": None, "mem_available_kib_at_start": None,
                                 "mem_alloc_wall_s": None}
        status = "error"
        try:
            if self._pool is not None:
                self._pool.set_level(p.level if p.kind == "cpu" else 0)
            if p.kind == "mem":
                extra["mem_available_kib_at_start"] = available
                extra["mem_alloc_mib"] = p.level
                alloc_began = self.env.mono_ns()
                self._buf = self.env.alloc(int(p.level * MIB))
                extra["mem_alloc_wall_s"] = (self.env.mono_ns() - alloc_began) / 1e9  # the memory rises over this long

            def mid() -> None:
                extra["vmrss_kib_mid"] = self.env.read_vmrss_kib()

            self._wait(p.duration_s, mid if p.kind == "mem" else None, watch_temp=(p.kind == "cpu" and self.thermal_guard))
            status = "interrupted" if self._stop_reason is not None else "completed"
        finally:
            wall_s = (self.env.mono_ns() - t0) / 1e9
            duty = achieved = None
            if before is not None and wall_s > 0:
                after = self._pool.cpu_seconds()
                duty = [(b - a) / wall_s for a, b in zip(before, after)]
                # percent of ALL cpus (like /proc/stat's aggregate): equal to the mean duty only if workers == cpus
                achieved = 100.0 * sum(b - a for a, b in zip(before, after)) / (wall_s * max(1, self.env.n_cpus))
            if self._buf is not None:
                self.env.release(self._buf)
                self._buf = None
            event = {"type": "phase_end", "index": index, "name": p.name, "kind": p.kind, "level": p.level,
                     "status": status, "wall_s": wall_s, "worker_duty": duty, "achieved_total_percent": achieved}
            event.update(extra)
            self.emit(self._stamp(event))
        return status == "completed"

    def run(self) -> str:
        refusals = self.preflight()
        if refusals:
            raise SafetyRefusal("; ".join(refusals))
        reason = "error"
        completed = 0
        try:
            if any(p.kind == "cpu" for p in self.plan):
                self._pool = self.env.make_pool(self.workers)
            pinned = getattr(self._pool, "pinned_cpus", lambda: None)() if self._pool is not None else None
            self.emit(self._stamp({
                "type": "start", "format": 1, "seed": self.seed, "order": [p.name for p in self.plan],
                "n_workers": self.workers if self._pool is not None else 0, "n_cpus": self.env.n_cpus,
                "pinned_cpus": pinned, "total_s": total_duration(self.plan), "reserve_mib": self.reserve_mib,
                "max_temp_c": self.max_temp_c if self.thermal_guard else None,
                "vmrss_kib_baseline": self.env.read_vmrss_kib()}))
            for index, p in enumerate(self.plan):
                if self._stop_reason is not None:
                    break
                if self._run_phase(index, p):
                    completed += 1
            reason = self._stop_reason or "completed"
            return reason
        finally:
            if self._pool is not None:
                try:
                    self._pool.set_level(0)
                finally:
                    self._pool.stop()
            if self._buf is not None:
                self.env.release(self._buf)
                self._buf = None
            self.emit(self._stamp({"type": "end", "reason": reason, "phases_completed": completed}))


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------
def _bounded(low: Optional[float], high: Optional[float], kind=float):
    def parse(text: str):
        try:
            value = kind(text)
        except ValueError:
            raise argparse.ArgumentTypeError("not a number: %r" % text)
        if not math.isfinite(value) or (low is not None and value < low) or (high is not None and value > high):
            raise argparse.ArgumentTypeError("must be between %s and %s" % (low if low is not None else "-inf", high if high is not None else "inf"))
        return value
    return parse


def _parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="stimulus", description="Known CPU and memory load for the accuracy protocol (Linux only).")
    p.add_argument("--schedule", required=True, help="JSON schedule: {\"phases\": [{name, kind, level, duration_s}, ...]}")
    p.add_argument("--out", default=None, help="event log (JSONL); created exclusively, never overwritten")
    p.add_argument("--shuffle-seed", type=int, default=None, help="seeded shuffle of the phase order (logged)")
    p.add_argument("--print-plan", action="store_true", help="print the plan and total duration; start nothing, write nothing")
    p.add_argument("--max-total-s", type=_bounded(1e-9, HARD_MAX_TOTAL_S), default=None,
                   help="refuse a longer schedule (finite; cannot exceed the hard cap of %d s)" % HARD_MAX_TOTAL_S)
    p.add_argument("--workers", type=_bounded(1, MAX_WORKERS, int), default=DEFAULT_WORKERS,
                   help="CPU worker processes (1 to %d, default 4)" % MAX_WORKERS)
    p.add_argument("--reserve-mib", type=_bounded(DEFAULT_RESERVE_MIB, None), default=DEFAULT_RESERVE_MIB,
                   help="MemAvailable that must remain after a mem phase (default 200; can only be raised)")
    p.add_argument("--max-temp-c", type=_bounded(1e-9, DEFAULT_MAX_TEMP_C), default=DEFAULT_MAX_TEMP_C,
                   help="thermal guard limit (default 80; can only be lowered)")
    p.add_argument("--no-thermal-guard", action="store_true", help="run CPU phases without the temperature stop")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None, *, env: Optional[Env] = None) -> int:
    args = _parse_args(argv)
    injected = env is not None
    try:
        plan = order_phases(load_schedule(args.schedule), args.shuffle_seed)
        check_total(plan, args.max_total_s)
    except ScheduleError as exc:
        print("ERROR " + str(exc), file=sys.stderr)
        return 2
    if env is None:
        if not sys.platform.startswith("linux") and not args.print_plan:
            print("ERROR the stimulus runs on Linux only (fork, sched_setaffinity, /proc)", file=sys.stderr)
            return 2
        env = real_env()
    if args.print_plan:
        print(plan_text(plan, args.shuffle_seed, env, args.reserve_mib, args.workers))
        return 0

    out = Path(args.out) if args.out else Path("stimulus_%s.jsonl" % datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    if out.exists():
        print("ERROR the output file exists and will not be overwritten: %s" % out, file=sys.stderr)
        return 2
    stim = Stimulus(plan, lambda e: None, env, seed=args.shuffle_seed, workers=args.workers,
                    reserve_mib=args.reserve_mib, max_temp_c=args.max_temp_c, thermal_guard=not args.no_thermal_guard)
    refusals = stim.preflight()  # before the log is created: a refused run leaves no file
    if refusals:
        print("REFUSED " + "; ".join(refusals), file=sys.stderr)
        return 3
    try:
        handle = open_exclusive(out)
    except FileExistsError:
        print("ERROR the output file exists and will not be overwritten: %s" % out, file=sys.stderr)
        return 2

    def emit(event: Dict[str, Any]) -> None:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")
        handle.flush()
        if event["type"] in ("phase_end", "end"):
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass

    stim.emit = emit
    previous = {}
    if not injected:  # real signals only in a real run
        for sig in [signal.SIGINT, signal.SIGTERM] + ([signal.SIGHUP] if hasattr(signal, "SIGHUP") else []):
            previous[sig] = signal.signal(sig, lambda signum, frame: stim.request_stop(signal.Signals(signum).name))
    try:
        reason = stim.run()
    except SafetyRefusal as exc:  # memory changed between the two preflight checks
        print("REFUSED " + str(exc), file=sys.stderr)
        return 3
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        handle.close()
    print("stimulus finished: %s (log %s)" % (reason, out), file=sys.stderr)
    return 0 if reason == "completed" else (3 if reason == "refused" else 1)


if __name__ == "__main__":
    sys.exit(main())
