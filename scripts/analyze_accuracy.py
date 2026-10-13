"""
Accuracy and step-response analysis of one sensor-accuracy run (notes/PI_ACCURACY_PROTOCOL_*.md).
Offline and read-only. Inputs, all JSONL except the thresholds:

    python scripts/analyze_accuracy.py --receiver notes/_pi_runs/receiver_<UTC>.jsonl \\
        --reference reference_<name>.jsonl --stimulus stimulus_<name>.jsonl --thresholds my_thresholds.json
    ... --exploratory      (needed while any threshold is null; every output then carries a banner)

It reuses the loader, the clock-offset pairing and the nearest-rank percentile of scripts/analyze_pi_run.py by
import; that file is not modified. Field names come from the receiver's run log (twin/pi_receiver.py), from
pi/reference_logger.py and from pi/stimulus.py.

WHAT IS COMPARED. Per quantity and per stimulus phase, in steady state only (the first --skip-windows sender
windows after each phase boundary are excluded, and a CPU window must lie entirely inside the phase):
  * sender value minus reference value: n, bias (mean), mean absolute error, p95 absolute error (nearest-rank).
    CPU: the reference is the logger's cumulative /proc/stat counters, linearly interpolated at the window edges
    [measured_at - window_ms, measured_at], with the sender's documented CPU definition (spec section 5)
    applied to them; the largest interpolation gap used is reported. A CPU message without a usable window_ms
    is excluded and counted, never given a default window. Isolated bad counter reads are dropped and a reset
    (or a CPU going offline) splits the series: a window that touches either costs only itself. Memory, frequency, temperature: the
    reference value nearest in time (memory percent derived with the sender's definition from MemTotal and
    MemAvailable); the time distance is reported.
  * reference versus the stimulus truth: CPU as the INCREASE over the preceding idle phase against the
    achieved duty's increase (cancelling a steady background), with the raw numbers beside it, labelled as
    including background load; memory growth against the VmRSS growth.
  * step response (CPU and memory): time from a phase start to the first message that crosses the MIDPOINT
    between the previous phase's plateau and this phase's plateau, on the Pi clock via measured_at, and as the
    twin sees it via patched_at minus the clock offset (from the first crossing message that Ditto
    acknowledged). The midpoint equals "50 % of the phase's level" only when the previous plateau is 0, which
    is not exactly so for CPU and not at all for memory (an idle Pi already uses about a third of its memory),
    so this is a deliberate reading of that wording, stated in the output. With no previous plateau or no step
    the row is N/A; no plateau is ever replaced by 0.
Every comparison states whether its reference path differs from the sender's. The pre-registered thresholds
are never defaulted: this script refuses null thresholds unless --exploratory is given. The output ends with
"What this does not show". Exit code: 0 ok, 1 if any verdict is FAIL, 2 bad input or null thresholds.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import analyze_pi_run as apr  # noqa: E402  (the existing analyzer: loader, offset pairing, percentile)
from analyze_pi_run import percentile  # noqa: E402

BANNER = "EXPLORATORY: NO PRE-REGISTERED THRESHOLDS"
DEFAULT_SKIP_WINDOWS = 2
DEFAULT_WINDOW_MS = 3000
OFFSET_WINDOW_MS = 300_000.0  # the analyzer's own default for pairing a sample with a probe

THRESHOLD_FIELDS = [
    "cpu_bias_abs_max_pp", "cpu_mae_max_pp", "cpu_p95_abs_error_max_pp",
    "memory_bias_abs_max_pp", "memory_mae_max_pp", "memory_p95_abs_error_max_pp",
    "frequency_bias_abs_max_khz", "frequency_mae_max_khz", "frequency_p95_abs_error_max_khz",
    "temperature_bias_abs_max_c", "temperature_mae_max_c", "temperature_p95_abs_error_max_c",
    "cpu_reference_vs_stimulus_abs_max_pp", "memory_reference_vs_stimulus_abs_max_mib",
    "max_cpu_edge_gap_ms", "max_instant_distance_1hz_ms", "max_instant_distance_10s_ms",
    "step_response_pi_clock_max_ms", "step_response_twin_visible_max_ms",
    "min_samples_per_phase",
]
_LIMITS = {
    "cpu": ("cpu_bias_abs_max_pp", "cpu_mae_max_pp", "cpu_p95_abs_error_max_pp"),
    "memory": ("memory_bias_abs_max_pp", "memory_mae_max_pp", "memory_p95_abs_error_max_pp"),
    "frequency": ("frequency_bias_abs_max_khz", "frequency_mae_max_khz", "frequency_p95_abs_error_max_khz"),
    "temperature": ("temperature_bias_abs_max_c", "temperature_mae_max_c", "temperature_p95_abs_error_max_c"),
}
_FEATURE = {"cpu": "cpu_utilization", "memory": "memory_utilization", "temperature": "cpu_temperature_c",
            "frequency": "cpu_frequency_khz"}
_UNITS = {"cpu": "percentage points", "memory": "percentage points", "frequency": "kHz", "temperature": "deg C"}

WHAT_THIS_DOES_NOT_SHOW = [
    "Nothing here establishes absolute accuracy: no value is compared with an independent physical measurement. "
    "Only CPU (a known duty cycle) and memory (a known allocation) have a ground truth, and only by construction "
    "of the stimulus.",
    "CPU and memory are compared with the same kernel file (/proc/stat, /proc/meminfo) read by independent "
    "code: that checks the sender's windowing, timestamps and arithmetic, not the kernel's counters.",
    "Frequency and temperature are two software paths to the same sensor or driver: they check the sender's "
    "read and timestamp path, not the sensor. The vcgencmd references are logged every 10 s and compared at the "
    "nearest time, so they cannot resolve faster changes.",
    "The sender, the reference logger and the stimulus workers all run on the machine being measured. The "
    "logger's own CPU cost and its vcgencmd subprocess are unmeasured and are part of the CPU numbers.",
    "Only steady state is covered: the first windows after each phase boundary are excluded, so transients "
    "are not in the error statistics. Step response is measured against the sender's own plateau.",
    "Temperature is excluded from step response (thermal inertia) and frequency is not controlled by the "
    "stimulus, so neither has a step response here.",
    "All three logs use the Pi's wall clock; a clock step during the run would distort every comparison. The "
    "offset to the receiver clock is used only for the twin-visible time and carries the probe's offset error.",
    "The CPU comparison with the stimulus is reported both as the increase over the preceding idle phase (which "
    "cancels background load that is steady between the two phases) and raw (which includes it). The increase does "
    "not cancel background that changes between phases (the sender's once-a-minute work, the logger's vcgencmd "
    "call, other processes), so a difference is not by itself a reference error; in an idle phase the raw "
    "difference IS the background load.",
    "patched_at is the time Ditto acknowledged the PATCH, not the time the value is visible downstream "
    "(Mosquitto, Telegraf, InfluxDB): twin-visible times stop at Ditto's reply.",
    "The reference counters are sampled at 1 Hz and interpolated linearly. If the load changes within a second "
    "the interpolation error is counted as sender error.",
    "--skip-windows is an analysis option, not part of the pre-registered thresholds file. Use the default and "
    "do not change it after seeing data; the value used is printed.",
    "One device and the phases of one run: nothing here generalises beyond them.",
]


class ThresholdError(ValueError):
    """The thresholds file is unusable, or has null values and --exploratory was not given."""


# --------------------------------------------------------------------------
# Thresholds
# --------------------------------------------------------------------------
def check_thresholds(raw: Optional[Dict[str, Any]], exploratory: bool) -> Dict[str, Optional[float]]:
    raw = raw or {}
    unknown = [k for k in raw if k not in THRESHOLD_FIELDS and not str(k).startswith("_")]
    if unknown:
        raise ThresholdError("unknown threshold field(s): " + ", ".join(sorted(map(str, unknown))))
    clean: Dict[str, Optional[float]] = {}
    for key in THRESHOLD_FIELDS:
        value = raw.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or value < 0):
            raise ThresholdError("threshold %s must be a non-negative number or null, got %r" % (key, value))
        clean[key] = value
    nulls = [k for k in THRESHOLD_FIELDS if clean[k] is None]
    if nulls and not exploratory:
        raise ThresholdError("these thresholds are null: %s. Thresholds must be pre-registered before the run; "
                             "use --exploratory to look at the numbers without verdicts." % ", ".join(nulls))
    return clean


def load_thresholds(path: Any, exploratory: bool) -> Dict[str, Optional[float]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ThresholdError("cannot read the thresholds file %s: %s" % (path, exc))
    if not isinstance(raw, dict):
        raise ThresholdError("the thresholds file must contain a JSON object")
    return check_thresholds(raw, exploratory)


def _verdict(value: Optional[float], limit: Optional[float], absolute: bool = True) -> Optional[str]:
    if limit is None:
        return None
    if value is None:
        return "FAIL"
    return "PASS" if (abs(value) if absolute else value) <= limit else "FAIL"


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------
def valid_window(w: Any) -> bool:
    """A usable averaging window: a positive number (not a bool, not a string)."""
    return isinstance(w, (int, float)) and not isinstance(w, bool) and w > 0


class Point:
    __slots__ = ("t", "value", "window_ms", "sample")

    def __init__(self, t: int, value: float, window_ms: Optional[int], sample: Dict[str, Any]):
        self.t, self.value, self.window_ms, self.sample = t, value, window_ms, sample


def sender_points(records: List[Dict[str, Any]]) -> Dict[str, List[Point]]:
    out: Dict[str, List[Point]] = {q: [] for q in _FEATURE}
    for s in records:
        if s.get("type") != "sample" or s.get("status") != "applied":
            continue
        for quantity, feature in _FEATURE.items():
            m = (s.get("measurements") or {}).get(feature)
            if isinstance(m, dict) and isinstance(m.get("value"), (int, float)) and isinstance(m.get("measured_at_ms"), int):
                out[quantity].append(Point(m["measured_at_ms"], m["value"], m.get("window_ms"), s))
    for pts in out.values():
        pts.sort(key=lambda p: p.t)
    return out


class Series:
    """Time-ordered (t_ms, values...) tuples with bisect lookups. `segments` (one id per item) marks counter
    resets: interpolation never crosses from one segment to another."""

    def __init__(self, items: List[Tuple], segments: Optional[List[int]] = None):
        order = sorted(range(len(items)), key=lambda i: items[i][0])
        self.items = [items[i] for i in order]
        self.segments = [segments[i] for i in order] if segments else [0] * len(items)
        self.times = [x[0] for x in self.items]

    def __len__(self) -> int:
        return len(self.items)

    def nearest(self, t: int) -> Optional[Tuple[Tuple, int]]:
        if not self.items:
            return None
        i = bisect.bisect_left(self.times, t)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(self.items):
                dist = abs(self.times[j] - t)
                if best is None or dist < best[1]:
                    best = (self.items[j], dist)
        return best

    def interpolate_ex(self, t: int) -> Tuple[Optional[Tuple[float, ...]], int, str, int]:
        """(values, width of the bracketing interval (0 on a record), reason, segment id). reason is "ok",
        "outside" (before the first or after the last record) or "discontinuity" (the bracketing records belong
        to different segments, i.e. the counters were reset between them)."""
        i = bisect.bisect_left(self.times, t)
        if i < len(self.times) and self.times[i] == t:
            return tuple(float(v) for v in self.items[i][1:]), 0, "ok", self.segments[i]
        if i == 0 or i == len(self.times):
            return None, 0, "outside", -1
        if self.segments[i - 1] != self.segments[i]:
            return None, 0, "discontinuity", -1
        a, b = self.items[i - 1], self.items[i]
        frac = (t - a[0]) / float(b[0] - a[0])
        return tuple(x + frac * (y - x) for x, y in zip(a[1:], b[1:])), b[0] - a[0], "ok", self.segments[i]

    def interpolate(self, t: int) -> Optional[Tuple[Tuple[float, ...], int]]:
        values, gap, _, _ = self.interpolate_ex(t)
        return None if values is None else (values, gap)


def clean_cpu_counters(rows: List[Tuple[int, int, int]]) -> Tuple[List[Tuple[int, int, int]], List[int], int, int]:
    """Make the cumulative /proc/stat series usable without losing more than the bad read itself.

    rows: (t_ms, total, idle) in time order. Records are judged against their neighbours, not against the last
    value seen, so one bad read cannot poison what follows:
      * a read that is out of line while its two neighbours agree with each other (one spike, high or low) is
        dropped as an outlier;
      * a decrease that the next read confirms (the counters restarted lower and keep rising: a reboot, or a CPU
        going offline, which removes its ticks from the aggregate) starts a new segment;
      * the last read, if lower than its predecessor, is dropped (a glitch cannot be told from a reset).
    Two or more consecutive bad reads are not recognised as outliers. A decrease smaller than one sampling
    step's increase is invisible. Returns (kept, segment ids, outliers dropped, resets)."""
    def ge(a: Tuple[int, int, int], b: Tuple[int, int, int]) -> bool:
        return a[1] >= b[1] and a[2] >= b[2]

    kept: List[Tuple[int, int, int]] = []
    segs: List[int] = []
    dropped = resets = segment = 0
    for i, cur in enumerate(rows):
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        prev = kept[-1] if kept else None
        if prev is None:
            if nxt is not None and not ge(nxt, cur):  # the first read is above its successor: a spike
                dropped += 1
                continue
        elif ge(cur, prev):
            if nxt is not None and not ge(nxt, cur) and ge(nxt, prev):  # a high spike between two consistent reads
                dropped += 1
                continue
        else:  # lower than the previous read
            if nxt is None or ge(nxt, prev):  # a low glitch, or the last read: drop it
                dropped += 1
                continue
            resets += 1  # the successor is below the previous read too: the counters restarted
            segment += 1
        kept.append(cur)
        segs.append(segment)
    return kept, segs, dropped, resets


class Reference:
    def __init__(self, records: List[Dict[str, Any]]):
        samples = [r for r in records if r.get("type") == "sample" and isinstance(r.get("t_epoch_ms"), int)]
        samples.sort(key=lambda r: r["t_epoch_ms"])
        cpu, mem, f_sys, f_vcg, t_sys, t_vcg = [], [], [], [], [], []
        self.counts = {"samples": len(samples), "proc_stat_null": 0, "meminfo_null": 0, "cpuinfo_cur_freq_null": 0,
                       "thermal_null": 0, "vcgencmd_samples": 0, "cpu_counter_regressions": 0, "cpu_counter_resets": 0}
        for r in samples:
            t = r["t_epoch_ms"]
            stat = (r.get("proc_stat") or {}).get("cpu") if isinstance(r.get("proc_stat"), dict) else None
            if (isinstance(stat, list) and len(stat) >= 8 and all(isinstance(x, int) for x in stat[:8])):
                cpu.append((t, sum(stat[:8]), stat[3] + stat[4]))
            else:
                self.counts["proc_stat_null"] += 1
            mi = r.get("meminfo_kib")
            if isinstance(mi, dict) and isinstance(mi.get("MemTotal"), int) and isinstance(mi.get("MemAvailable"), int) and mi["MemTotal"] > 0:
                mem.append((t, mi["MemTotal"], mi["MemAvailable"]))
            else:
                self.counts["meminfo_null"] += 1
            if isinstance(r.get("cpuinfo_cur_freq_khz"), int):
                f_sys.append((t, r["cpuinfo_cur_freq_khz"]))
            else:
                self.counts["cpuinfo_cur_freq_null"] += 1
            if isinstance(r.get("thermal_zone0_temp_millic"), int):
                t_sys.append((t, r["thermal_zone0_temp_millic"] / 1000.0))
            else:
                self.counts["thermal_null"] += 1
            v = r.get("vcgencmd")
            if isinstance(v, dict):
                self.counts["vcgencmd_samples"] += 1
                if isinstance(v.get("arm_clock_hz"), (int, float)):
                    f_vcg.append((t, v["arm_clock_hz"] / 1000.0))
                if isinstance(v.get("temp_c"), (int, float)):
                    t_vcg.append((t, float(v["temp_c"])))
        kept, segs, dropped, resets = clean_cpu_counters(cpu)
        self.counts["cpu_counter_regressions"], self.counts["cpu_counter_resets"] = dropped, resets
        self.cpu, self.mem = Series(kept, segs), Series(mem)
        self.freq_sysfs, self.freq_vcg = Series(f_sys), Series(f_vcg)
        self.temp_sysfs, self.temp_vcg = Series(t_sys), Series(t_vcg)


def parse_stimulus(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    start = next((r for r in records if r.get("type") == "start"), {})
    begun = {r["index"]: r for r in records if r.get("type") == "phase_start"}
    ended = {r["index"]: r for r in records if r.get("type") == "phase_end"}
    phases, excluded = [], []
    for index in sorted(begun):
        b, e = begun[index], ended.get(index)
        status = e.get("status") if e else "incomplete"
        if status != "completed":
            excluded.append({"name": b["name"], "status": status})
            continue
        phases.append({"index": index, "name": b["name"], "kind": b["kind"], "level": b["level"],
                       "t_start": b["t_epoch_ms"], "t_end": e["t_epoch_ms"],
                       "achieved": e.get("achieved_total_percent"), "vmrss_mid_kib": e.get("vmrss_kib_mid"),
                       "alloc_wall_s": e.get("mem_alloc_wall_s")})
    return {"seed": start.get("seed"), "vmrss_baseline_kib": start.get("vmrss_kib_baseline"),
            "phases": phases, "excluded": excluded}


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------
def _stats(errors: List[float]) -> Dict[str, Any]:
    if not errors:
        return {"n": 0, "bias": None, "mae": None, "p95_abs": None}
    absolute = [abs(e) for e in errors]
    return {"n": len(errors), "bias": sum(errors) / len(errors), "mae": sum(absolute) / len(absolute),
            "p95_abs": percentile(absolute, 95)}


def _mean(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return None if a is None or b is None else a - b


class Context:
    def __init__(self, th: Dict[str, Optional[float]], skip_ms: int):
        self.th, self.skip_ms = th, skip_ms


def _steady_start(phase: Dict[str, Any], ctx: Context) -> int:
    return phase["t_start"] + ctx.skip_ms


def _row_verdicts(quantity: str, row: Dict[str, Any], th: Dict[str, Optional[float]]) -> Dict[str, Optional[str]]:
    bias_key, mae_key, p95_key = _LIMITS[quantity]
    minimum = th["min_samples_per_phase"]
    return {"bias": _verdict(row["bias"], th[bias_key]), "mae": _verdict(row["mae"], th[mae_key], False),
            "p95_abs": _verdict(row["p95_abs"], th[p95_key], False),
            "n": None if minimum is None else ("PASS" if row["n"] >= minimum else "FAIL")}


# --------------------------------------------------------------------------
# Comparisons
# --------------------------------------------------------------------------
def _cpu_rows(points: List[Point], ref: Reference, phases, ctx: Context) -> Tuple[List[Dict[str, Any]], Dict[str, List[Tuple[float, float]]], Optional[int]]:
    rows, used_by_phase, overall_gap = [], {}, None
    gap_limit = ctx.th["max_cpu_edge_gap_ms"]
    for ph in phases:
        start = _steady_start(ph, ctx)
        errors, used, gaps = [], [], []
        no_ref = skipped_gap = degenerate = discontinuity = no_window = 0
        for p in points:
            if not valid_window(p.window_ms):  # never given a default window
                if start <= p.t <= ph["t_end"]:
                    no_window += 1
                continue
            window = p.window_ms
            if p.t - window < start or p.t > ph["t_end"]:
                continue
            a, b = ref.cpu.interpolate_ex(p.t - window), ref.cpu.interpolate_ex(p.t)
            if a[0] is None or b[0] is None:
                if "discontinuity" in (a[2], b[2]):
                    discontinuity += 1
                else:
                    no_ref += 1
                continue
            if a[3] != b[3]:  # the two edges are in different segments: the counters were reset in between
                discontinuity += 1
                continue
            gap = max(a[1], b[1])
            if gap_limit is not None and gap > gap_limit:
                skipped_gap += 1
                continue
            d_total, d_idle = b[0][0] - a[0][0], b[0][1] - a[0][1]
            if d_total <= 0:
                degenerate += 1
                continue
            ref_pct = 100.0 * (d_total - d_idle) / d_total
            errors.append(p.value - ref_pct)
            used.append((p.value, ref_pct))
            gaps.append(gap)
        row = {"phase": ph["name"], "kind": ph["kind"], "level": ph["level"], **_stats(errors),
               "max_interpolation_gap_ms": max(gaps) if gaps else None, "n_skipped_no_reference": no_ref,
               "n_skipped_gap": skipped_gap, "n_skipped_degenerate": degenerate,
               "n_skipped_discontinuity": discontinuity, "n_skipped_no_window": no_window}
        row["verdicts"] = _row_verdicts("cpu", row, ctx.th)
        rows.append(row)
        used_by_phase[ph["name"]] = used
        if gaps:
            overall_gap = max(gaps) if overall_gap is None else max(overall_gap, max(gaps))
    return rows, used_by_phase, overall_gap


def _instant_rows(quantity: str, points: List[Point], provider, phases, ctx: Context, limit_key: str) -> List[Dict[str, Any]]:
    """provider(t) -> (reference value, distance_ms) or None. limit_key: max_instant_distance_1hz_ms for a 1 Hz
    reference, max_instant_distance_10s_ms for a 10 s one."""
    rows = []
    limit = ctx.th[limit_key]
    for ph in phases:
        start = _steady_start(ph, ctx)
        errors, distances = [], []
        skipped_distance = no_ref = 0
        for p in points:
            if p.t < start or p.t >= ph["t_end"]:  # half-open: a read on the boundary belongs to the next phase
                continue
            hit = provider(p.t)
            if hit is None:
                no_ref += 1
                continue
            value, dist = hit
            if limit is not None and dist > limit:
                skipped_distance += 1
                continue
            errors.append(p.value - value)
            distances.append(dist)
        row = {"phase": ph["name"], "kind": ph["kind"], "level": ph["level"], **_stats(errors),
               "max_distance_ms": max(distances) if distances else None,
               "median_distance_ms": statistics.median(distances) if distances else None,
               "n_skipped_distance": skipped_distance, "n_skipped_no_reference": no_ref}
        row["verdicts"] = _row_verdicts(quantity, row, ctx.th)
        rows.append(row)
    return rows


def _mem_provider(ref: Reference):
    def provider(t: int):
        hit = ref.mem.nearest(t)
        if hit is None:
            return None
        (_, total, available), dist = hit
        return 100.0 * (total - available) / total, dist
    return provider


def _series_provider(series: Series):
    def provider(t: int):
        hit = series.nearest(t)
        return None if hit is None else (hit[0][1], hit[1])
    return provider


def _ref_used_mib(ref: Reference, ph: Dict[str, Any], ctx: Context) -> Tuple[Optional[float], int]:
    values = [(total - available) / 1024.0 for t, total, available in ref.mem.items
              if _steady_start(ph, ctx) <= t < ph["t_end"]]  # half-open: a record on the boundary belongs to the next phase
    return _mean(values), len(values)


def _cpu_vs_stimulus(phases, used_by_phase, ctx: Context) -> List[Dict[str, Any]]:
    """Per phase: the raw numbers (whole-machine CPU, which includes background load) and, where a preceding idle
    phase exists, the INCREASE over it, which cancels a background that is steady between the two phases. The
    verdict uses the increase when there is one and the raw numbers otherwise (labelled); an idle phase is the
    baseline and gets no verdict."""
    means = {}
    for ph in phases:
        used = used_by_phase.get(ph["name"], [])
        means[ph["name"]] = (_mean([r for _, r in used]), _mean([s_ for s_, _ in used]), len(used))
    rows = []
    for pos, ph in enumerate(phases):
        ref_mean, sender_mean, n = means[ph["name"]]
        nominal = float(ph["level"]) if ph["kind"] == "cpu" else 0.0
        base = next((q for q in reversed(phases[:pos]) if q["kind"] == "idle"), None)
        row = {"phase": ph["name"], "kind": ph["kind"], "n": n, "ref_mean": ref_mean, "achieved": ph["achieved"],
               "nominal": nominal, "ref_minus_achieved": _diff(ref_mean, ph["achieved"]),
               "ref_minus_nominal": _diff(ref_mean, nominal), "achieved_minus_nominal": _diff(ph["achieved"], nominal),
               "sender_mean_minus_achieved": _diff(sender_mean, ph["achieved"]),
               "baseline_phase": None, "ref_increase": None, "achieved_increase": None,
               "ref_increase_minus_achieved_increase": None, "na_reason": None,
               "basis": "raw (includes background load)"}
        judged = row["ref_minus_achieved"]
        if ph["kind"] == "idle":
            row["basis"] = "raw (an idle phase: the difference is the background load, and the baseline of the others)"
            judged = None
        elif base is None:
            row["na_reason"] = "no preceding idle phase to take an increase over"
        else:
            base_ref, _, _ = means[base["name"]]
            row["baseline_phase"] = base["name"]
            if base_ref is None or base["achieved"] is None or ref_mean is None or ph["achieved"] is None:
                row["na_reason"] = "the preceding idle phase (%s) has no usable windows" % base["name"]
            else:
                row["ref_increase"] = ref_mean - base_ref
                row["achieved_increase"] = ph["achieved"] - base["achieved"]
                row["ref_increase_minus_achieved_increase"] = row["ref_increase"] - row["achieved_increase"]
                row["basis"] = "increase over %s" % base["name"]
                judged = row["ref_increase_minus_achieved_increase"]
        row["verdicts"] = {"ref_vs_achieved": _verdict(judged, ctx.th["cpu_reference_vs_stimulus_abs_max_pp"]) if judged is not None else None}
        rows.append(row)
    return rows


def _mem_vs_stimulus(phases, ref: Reference, stim: Dict[str, Any], ctx: Context) -> List[Dict[str, Any]]:
    rows = []
    for ph in phases:
        if ph["kind"] != "mem":
            continue
        baseline = next((q for q in reversed(phases) if q["index"] < ph["index"] and q["kind"] == "idle"), None)
        used, n = _ref_used_mib(ref, ph, ctx)
        base_used = _ref_used_mib(ref, baseline, ctx)[0] if baseline else None
        mid, base_rss = ph["vmrss_mid_kib"], stim["vmrss_baseline_kib"]
        truth = (mid - base_rss) / 1024.0 if mid is not None and base_rss is not None else None
        ref_delta = _diff(used, base_used)
        row = {"phase": ph["name"], "n_reference_records": n, "baseline_phase": baseline["name"] if baseline else None,
               "ref_delta_mib": ref_delta, "truth_delta_mib": truth, "nominal_mib": float(ph["level"]),
               "ref_minus_truth_mib": _diff(ref_delta, truth), "ref_minus_nominal_mib": _diff(ref_delta, float(ph["level"]))}
        row["verdicts"] = {"ref_vs_truth": _verdict(row["ref_minus_truth_mib"], ctx.th["memory_reference_vs_stimulus_abs_max_mib"])}
        rows.append(row)
    return rows


def _plateau(points: List[Point], ph: Dict[str, Any], ctx: Context, needs_window: bool = False) -> Optional[float]:
    start = _steady_start(ph, ctx)
    values = [p.value for p in points
              if (not needs_window or valid_window(p.window_ms)) and p.t - (p.window_ms if needs_window else 0) >= start and p.t <= ph["t_end"]]
    return _mean(values)


def _step_rows(quantity: str, points: List[Point], phases, wanted_kind: str, offsets, ctx: Context, dry_run: bool) -> List[Dict[str, Any]]:
    """Step response per phase of the wanted kind.

    Threshold rule: the MIDPOINT between the previous phase's plateau and this phase's plateau. That equals 50 %
    of this phase's level only when the previous plateau is 0, which is not exactly true for CPU and not at all
    for memory (an idle Pi already uses about a third of its memory), where "50 % of the level" would lie below
    the starting value. Where the rule cannot be applied (no previous plateau, or no step) the row says so
    (na_reason) and no value is computed: no plateau is ever silently replaced by 0."""
    rows = []
    twin_limit = ctx.th["step_response_twin_visible_max_ms"]
    pi_limit = ctx.th["step_response_pi_clock_max_ms"]
    for pos, ph in enumerate(phases):
        if ph["kind"] != wanted_kind:
            continue
        needs_window = quantity == "cpu"
        level = _plateau(points, ph, ctx, needs_window)
        previous_phase = phases[pos - 1] if pos > 0 else None
        previous = _plateau(points, previous_phase, ctx, needs_window) if previous_phase else None
        ramp = ph.get("alloc_wall_s") if wanted_kind == "mem" else None
        row = {"phase": ph["name"], "level_steady": level, "previous_level": previous, "threshold": None,
               "pi_clock_ms": None, "twin_visible_ms": None, "twin_note": None, "na_reason": None,
               "previous_phase": previous_phase["name"] if previous_phase else None,
               "stimulus_ramp_ms": int(round(ramp * 1000)) if ramp is not None else None}  # a mem phase is stamped before its allocation
        if level is None:
            row["na_reason"] = "no steady-state value in this phase"
        elif previous is None:
            row["na_reason"] = "no previous plateau (first phase, or the previous phase has no steady-state value)"
        elif abs(level - previous) < 1e-12:
            row["na_reason"] = "no step: this plateau equals the previous one"
        else:
            row["threshold"] = previous + 0.5 * (level - previous)
            rising = level >= previous
            crossing = None
            for i, p in enumerate(points):
                if p.t <= ph["t_start"] or p.t > ph["t_end"]:
                    continue
                if (p.value >= row["threshold"]) if rising else (p.value <= row["threshold"]):
                    crossing = i
                    row["pi_clock_ms"] = p.t - ph["t_start"]
                    break
            if crossing is not None:
                # the first message at or after the crossing that Ditto acknowledged
                for j in range(crossing, len(points)):
                    q = points[j]
                    if q.t > ph["t_end"] + 30_000:  # do not wander into other phases
                        break
                    patched, offset = q.sample.get("patched_at_ms"), offsets(q.sample)
                    if patched is None:
                        continue
                    if offset is None:
                        row["twin_note"] = "no valid clock probe: patched_at cannot be put on the Pi clock"
                        break
                    row["twin_visible_ms"] = patched - offset - ph["t_start"]
                    if j != crossing:
                        row["twin_note"] = "the crossing message was not acknowledged by Ditto; this is from a later message"
                    break
                if row["twin_visible_ms"] is None and row["twin_note"] is None:
                    row["twin_note"] = "no patched_at in the log (%s)" % ("dry run" if dry_run else "no message at or after the crossing was acknowledged by Ditto")
        na = row["na_reason"] is not None
        if na:
            row["verdicts"] = {"pi_clock": None if pi_limit is None else "N/A", "twin_visible": None if twin_limit is None else "N/A"}
        else:
            twin_verdict = _verdict(row["twin_visible_ms"], twin_limit, False)
            if twin_limit is not None and row["twin_visible_ms"] is None and (dry_run or row["pi_clock_ms"] is None):
                twin_verdict = "N/A"  # a dry run has no patched_at; a never-reached plateau already fails the Pi-clock check
            row["verdicts"] = {"pi_clock": _verdict(row["pi_clock_ms"], pi_limit, False), "twin_visible": twin_verdict}
        rows.append(row)
    return rows


# --------------------------------------------------------------------------
# The whole analysis
# --------------------------------------------------------------------------
def analyze(receiver_records: List[Dict[str, Any]], reference_records: List[Dict[str, Any]],
            stimulus_records: List[Dict[str, Any]], thresholds: Optional[Dict[str, Any]], *,
            exploratory: bool = False, skip_windows: int = DEFAULT_SKIP_WINDOWS,
            thresholds_sha256: Optional[str] = None, thresholds_path: Optional[str] = None,
            unparsable: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    th = check_thresholds(thresholds, exploratory)
    points = sender_points(receiver_records)
    ref = Reference(reference_records)
    stim = parse_stimulus(stimulus_records)
    phases = stim["phases"]
    windows = [p.window_ms for p in points["cpu"] if valid_window(p.window_ms)]
    window_ms = int(statistics.median(windows)) if windows else DEFAULT_WINDOW_MS
    window_source = ("median of the window_ms in the CPU messages" if windows
                     else "default %d ms for the exclusion zone only (no message carried window_ms)" % DEFAULT_WINDOW_MS)
    without_window = sum(1 for p in points["cpu"] if not valid_window(p.window_ms))
    ctx = Context(th, skip_windows * window_ms)
    samples = [r for r in receiver_records if r.get("type") == "sample"]
    valid_probes, _ = apr.collect_probes(samples)
    start_rec = next((r for r in receiver_records if r.get("type") == "start"), {})
    dry_run = bool(start_rec.get("dry_run"))

    def offsets(sample: Dict[str, Any]) -> Optional[float]:
        chosen = apr.pick_offset(valid_probes, sample.get("received_at_ms", 0), OFFSET_WINDOW_MS)
        return None if chosen is None else chosen[0]

    cpu_rows, used_by_phase, cpu_gap = _cpu_rows(points["cpu"], ref, phases, ctx)
    quantities: Dict[str, Any] = {
        "cpu": {
            "label": "CPU utilisation",
            "sender_source": "/proc/stat, first cpu line, two reads (the window between them is window_ms)",
            "truth": "ground truth by construction for the increase over the preceding idle phase that the stimulus adds (achieved duty "
                     "measured per worker, minus that of the idle phase); the raw whole-machine CPU also includes background load "
                     "(the sender, the logger, the kernel) that the achieved duty does not",
            "comparisons": [{
                "name": "/proc/stat counters, interpolated at the window edges", "cadence": "1 Hz reference",
                "same_file_as_sender": True,
                "path_note": "SAME file as the sender (/proc/stat), read by independent code with its own timestamps; "
                             "the sender's CPU definition (spec section 5) is applied to the counters",
                "phases": cpu_rows}],
            "max_interpolation_gap_ms": cpu_gap,
            "reference_vs_stimulus": _cpu_vs_stimulus(phases, used_by_phase, ctx),
            "step_response": _step_rows("cpu", points["cpu"], phases, "cpu", offsets, ctx, dry_run),
        },
        "memory": {
            "label": "Memory utilisation",
            "sender_source": "/proc/meminfo: 100 * (MemTotal - MemAvailable) / MemTotal",
            "truth": "ground truth by construction: the stimulus allocation (VmRSS growth of the stimulus process)",
            "comparisons": [{
                "name": "/proc/meminfo raw fields, nearest record", "cadence": "1 Hz reference",
                "same_file_as_sender": True,
                "path_note": "SAME file as the sender (/proc/meminfo), read by independent code; the percentage is "
                             "derived here with the sender's definition",
                "phases": _instant_rows("memory", points["memory"], _mem_provider(ref), phases, ctx, "max_instant_distance_1hz_ms")}],
            "reference_vs_stimulus": _mem_vs_stimulus(phases, ref, stim, ctx),
            "step_response": _step_rows("memory", points["memory"], phases, "mem", offsets, ctx, dry_run),
        },
        "frequency": {
            "label": "CPU frequency",
            "sender_source": "scaling_cur_freq (kHz)",
            "truth": "none: no ground truth (the stimulus does not control frequency)",
            "comparisons": [
                {"name": "cpuinfo_cur_freq (sysfs, 1 Hz, nearest record)", "same_file_as_sender": False, "cadence": "1 Hz reference",
                 "path_note": "DIFFERENT file from the sender (cpuinfo_cur_freq versus scaling_cur_freq), served by the "
                              "same cpufreq driver; whether it reads the hardware or a cached value is not verified here. "
                              "It may be readable by root only.",
                 "phases": _instant_rows("frequency", points["frequency"], _series_provider(ref.freq_sysfs), phases, ctx, "max_instant_distance_1hz_ms")},
                {"name": "vcgencmd measure_clock arm (firmware, every 10 s, nearest record)", "same_file_as_sender": False, "cadence": "10 s reference",
                 "path_note": "DIFFERENT path (firmware via vcgencmd), at 0.1 Hz: the nearest record can be several "
                              "seconds away, see max_distance_ms. Whether it is independent of the sysfs path is not verified here.",
                 "phases": _instant_rows("frequency", points["frequency"], _series_provider(ref.freq_vcg), phases, ctx, "max_instant_distance_10s_ms")}],
        },
        "temperature": {
            "label": "SoC temperature",
            "sender_source": "thermal_zone0/temp (millidegrees / 1000)",
            "truth": "none: no ground truth (the stimulus does not control temperature)",
            "comparisons": [
                {"name": "thermal_zone0/temp (sysfs, 1 Hz, nearest record)", "same_file_as_sender": True, "cadence": "1 Hz reference",
                 "path_note": "SAME file as the sender (thermal_zone0/temp): this checks the read and timestamp path only",
                 "phases": _instant_rows("temperature", points["temperature"], _series_provider(ref.temp_sysfs), phases, ctx, "max_instant_distance_1hz_ms")},
                {"name": "vcgencmd measure_temp (firmware, every 10 s, nearest record)", "same_file_as_sender": False, "cadence": "10 s reference",
                 "path_note": "DIFFERENT software path to the same sensor, at 0.1 Hz; agreement says nothing about the "
                              "sensor's absolute accuracy",
                 "phases": _instant_rows("temperature", points["temperature"], _series_provider(ref.temp_vcg), phases, ctx, "max_instant_distance_10s_ms")}],
        },
    }
    return {
        "exploratory": bool(exploratory),
        "banner": BANNER if exploratory else None,
        "thresholds": th, "thresholds_sha256": thresholds_sha256, "thresholds_path": thresholds_path,
        "run": {"applied_messages": len(samples), "dry_run": dry_run, "clock_probes": len(valid_probes),
                "window_ms": window_ms, "window_ms_source": window_source, "cpu_messages_without_window": without_window,
                "skip_windows": skip_windows, "exclusion_ms": ctx.skip_ms,
                "unparsable_lines": dict(unparsable or {})},
        "stimulus": {"seed": stim["seed"], "phases_used": [p["name"] for p in phases], "phases_excluded": stim["excluded"]},
        "reference": dict(ref.counts),
        "quantities": quantities,
    }


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def _f(x: Optional[float], nd: int = 3) -> str:
    return "-" if x is None else ("%d" % x if isinstance(x, int) else ("%.*f" % (nd, x)))


def _v(verdicts: Dict[str, Optional[str]]) -> str:
    shown = ["%s=%s" % (k, v) for k, v in verdicts.items() if v is not None]
    return " ".join(shown) if shown else "no threshold"


def format_report(result: Dict[str, Any]) -> str:
    banner = result["banner"]
    lines: List[str] = []

    def section(title: str) -> None:
        if banner:
            lines.append(banner)
        lines.append("== %s ==" % title)

    run, stim, ref = result["run"], result["stimulus"], result["reference"]
    section("Run")
    lines.append("receiver messages applied: %d  dry run: %s  clock probes: %d  CPU window: %d ms (%s); the first %d windows "
                 "(%d ms) after each phase boundary are excluded" % (run["applied_messages"], run["dry_run"], run["clock_probes"],
                                                                      run["window_ms"], run["window_ms_source"], run["skip_windows"],
                                                                      run["exclusion_ms"]))
    lines.append("CPU messages without window_ms: %d (excluded from every CPU comparison)" % run["cpu_messages_without_window"])
    if run["unparsable_lines"]:
        lines.append("unparsable lines: " + ", ".join("%s %d" % (k, v) for k, v in run["unparsable_lines"].items()))
    lines.append("stimulus phases used: %s%s" % (", ".join(stim["phases_used"]) or "none",
                 ("; EXCLUDED (not completed): " + ", ".join("%s (%s)" % (e["name"], e["status"]) for e in stim["phases_excluded"]))
                 if stim["phases_excluded"] else ""))
    lines.append("reference samples: %d  null /proc/stat: %d  null /proc/meminfo: %d  null cpuinfo_cur_freq: %d  null thermal: %d  "
                 "vcgencmd records: %d" % (
                     ref["samples"], ref["proc_stat_null"], ref["meminfo_null"], ref["cpuinfo_cur_freq_null"], ref["thermal_null"],
                     ref["vcgencmd_samples"]))
    lines.append("reference CPU counters: records dropped as outliers: %d  resets: %d (a window that touches a reset is unusable: "
                 "'discontinuity' below)" % (ref["cpu_counter_regressions"], ref["cpu_counter_resets"]))
    if result.get("thresholds_path"):
        lines.append("thresholds file: %s  sha256 %s" % (result["thresholds_path"], result["thresholds_sha256"]))

    for key in ("cpu", "memory", "frequency", "temperature"):
        q = result["quantities"][key]
        section(q["label"])
        lines.append("sender source: " + q["sender_source"])
        lines.append("truth: " + q["truth"])
        for cmp_ in q["comparisons"]:
            lines.append("")
            lines.append("sender minus reference: " + cmp_["name"])
            lines.append("  reference path: " + cmp_["path_note"])
            if cmp_.get("cadence"):
                lines.append("  distance limit that applies: %s" % ("max_instant_distance_10s_ms (10 s reference)" if cmp_["cadence"].startswith("10")
                                                                    else "max_instant_distance_1hz_ms (1 Hz reference)") if key != "cpu" else
                             "  edge-gap limit that applies: max_cpu_edge_gap_ms (1 Hz reference)")
            instant = key != "cpu"
            lines.append("  %-8s %3s %12s %12s %12s  %s" % ("phase", "n", "bias", "MAE", "p95 |err|",
                         "max distance ms (median)" if instant else "max interpolation gap ms; skipped: no ref / gap / discontinuity / no window"))
            for r in cmp_["phases"]:
                extra = ("%s (%s)  skipped: dist %d, no ref %d" % (_f(r["max_distance_ms"]), _f(r["median_distance_ms"]),
                         r["n_skipped_distance"], r["n_skipped_no_reference"]) if instant else
                         "%s; %d / %d / %d / %d" % (_f(r["max_interpolation_gap_ms"]), r["n_skipped_no_reference"], r["n_skipped_gap"],
                                                    r["n_skipped_discontinuity"], r["n_skipped_no_window"]))
                lines.append("  %-8s %3d %12s %12s %12s  %s  [%s]" % (r["phase"], r["n"], _f(r["bias"]), _f(r["mae"]), _f(r["p95_abs"]),
                                                                       extra, _v(r["verdicts"])))
        if key == "cpu":
            lines.append("  largest interpolation gap used: %s ms" % _f(q["max_interpolation_gap_ms"]))
        lines.append("  units: " + _UNITS[key])
        if "reference_vs_stimulus" in q:
            lines.append("")
            lines.append("reference versus stimulus truth:")
            if key == "cpu":
                lines.append("  (the reference is whole-machine CPU: the raw numbers include background load that the achieved duty does not; "
                             "the increase over the preceding idle phase cancels a steady background)")
                for r in q["reference_vs_stimulus"]:
                    if r["ref_increase"] is not None:
                        head = "increase over %s: reference %s, achieved %s: difference %s" % (
                            r["baseline_phase"], _f(r["ref_increase"]), _f(r["achieved_increase"]), _f(r["ref_increase_minus_achieved_increase"]))
                    else:
                        head = "increase: N/A (%s)" % (r["na_reason"] or "this is the baseline")
                    lines.append("  %-8s n=%3d %s | raw (includes background load): reference mean %s, achieved duty %s, nominal %s: "
                                 "ref-achieved %s, ref-nominal %s, achieved-nominal %s, sender-achieved %s  [basis: %s; %s]" % (
                                     r["phase"], r["n"], head, _f(r["ref_mean"]), _f(r["achieved"]), _f(r["nominal"]),
                                     _f(r["ref_minus_achieved"]), _f(r["ref_minus_nominal"]), _f(r["achieved_minus_nominal"]),
                                     _f(r["sender_mean_minus_achieved"]), r["basis"], _v(r["verdicts"])))
            else:
                for r in q["reference_vs_stimulus"]:
                    lines.append("  %-8s baseline phase %s: reference delta %s MiB, VmRSS growth %s MiB, nominal %s MiB: "
                                 "ref-truth %s, ref-nominal %s  [%s]" % (
                                     r["phase"], r["baseline_phase"], _f(r["ref_delta_mib"]), _f(r["truth_delta_mib"]),
                                     _f(r["nominal_mib"]), _f(r["ref_minus_truth_mib"]), _f(r["ref_minus_nominal_mib"]), _v(r["verdicts"])))
        if "step_response" in q:
            lines.append("")
            if key == "cpu":
                lines.append("note: the sender's CPU value averages the last %d ms, so the earliest possible crossing of the midpoint is about "
                             "%.1f s after the step; this is a property of the sender, not a defect." % (run["window_ms"], run["window_ms"] / 2000.0))
            lines.append("step response (threshold = midpoint between the previous plateau and this plateau; it equals 50 % of the level only "
                         "when the previous plateau is 0, which it is not exactly for CPU and not at all for memory):")
            for r in q["step_response"]:
                if r["na_reason"]:
                    lines.append("  %-8s N/A: %s  [%s]" % (r["phase"], r["na_reason"], _v(r["verdicts"])))
                    continue
                notes = []
                if r["twin_note"]:
                    notes.append(r["twin_note"])
                if r["stimulus_ramp_ms"] is not None:
                    notes.append("the stimulus allocation took %d ms and is inside these times" % r["stimulus_ramp_ms"])
                lines.append("  %-8s previous plateau (%s) %s -> plateau %s, threshold %s: Pi clock %s ms; twin-visible %s ms%s  [%s]" % (
                    r["phase"], r["previous_phase"], _f(r["previous_level"]), _f(r["level_steady"]), _f(r["threshold"]),
                    _f(r["pi_clock_ms"]), _f(r["twin_visible_ms"]), ("  (%s)" % "; ".join(notes)) if notes else "",
                    _v(r["verdicts"])))
        else:
            lines.append("")
            lines.append("step response: not computed (%s)." % (
                "temperature is excluded because of thermal inertia" if key == "temperature"
                else "frequency is not controlled by the stimulus, so there is no step to time"))
    section("What this does not show")
    lines.extend("- " + item for item in WHAT_THIS_DOES_NOT_SHOW)
    return "\n".join(lines) + "\n"


def any_fail(node: Any) -> bool:
    """True if any verdict anywhere in the result is FAIL."""
    if isinstance(node, dict):
        return any(v == "FAIL" for v in node.get("verdicts", {}).values()) or any(any_fail(v) for v in node.values())
    if isinstance(node, list):
        return any(any_fail(v) for v in node)
    return False


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Accuracy and step-response analysis (offline, read-only).")
    p.add_argument("--receiver", required=True, help="receiver run log (JSONL)")
    p.add_argument("--reference", required=True, help="reference logger log (JSONL)")
    p.add_argument("--stimulus", required=True, help="stimulus event log (JSONL)")
    p.add_argument("--thresholds", required=True, help="pre-registered thresholds (JSON; see scripts/accuracy_thresholds.template.json)")
    p.add_argument("--exploratory", action="store_true", help="allow null thresholds; every output carries a banner")
    p.add_argument("--skip-windows", type=int, default=DEFAULT_SKIP_WINDOWS,
                   help="windows to exclude after each phase boundary (default 2; not part of the thresholds file: do not tune it after seeing data)")
    p.add_argument("--json", default=None, help="also write the full result to this file")
    args = p.parse_args(argv)
    for label in ("receiver", "reference", "stimulus", "thresholds"):
        if not Path(getattr(args, label)).is_file():
            print("ERROR %s file not found: %s" % (label, getattr(args, label)), file=sys.stderr)
            return 2
    try:
        thresholds = load_thresholds(args.thresholds, args.exploratory)
    except ThresholdError as exc:
        print("ERROR " + str(exc), file=sys.stderr)
        return 2
    digest = hashlib.sha256(Path(args.thresholds).read_bytes()).hexdigest()
    receiver, bad_receiver = apr.load_run(args.receiver)
    reference, bad_reference = apr.load_run(args.reference)
    stimulus, bad_stimulus = apr.load_run(args.stimulus)
    unparsable = {k: v for k, v in (("receiver", bad_receiver), ("reference", bad_reference), ("stimulus", bad_stimulus)) if v}
    result = analyze(receiver, reference, stimulus, thresholds, exploratory=args.exploratory, skip_windows=args.skip_windows,
                     thresholds_sha256=digest, thresholds_path=args.thresholds, unparsable=unparsable)
    sys.stdout.write(format_report(result))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 1 if any_fail(result) else 0


if __name__ == "__main__":
    sys.exit(main())
