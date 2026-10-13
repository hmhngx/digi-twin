"""Synthetic logs for the accuracy tools: a receiver log, a reference log and a stimulus log generated from ONE
model of a Pi, so that the right answer is known exactly and errors can be injected on purpose.

The model (times are the Pi clock, T0 = the start of the first phase):
  * 4 cores, USER_HZ 100: 400 ticks per second in total. In second [k, k+1) the CPU is busy for 4 * pct ticks,
    so integer percentages give exact integer counters, and the CPU percent over any 3 s window made of whole
    seconds is exact.
  * Memory: MemTotal 1,000,000 kB; used = BASE_USED_KIB (+ level MiB during a mem phase).
  * The sender reports every 3 s (measured_at = T0 + 3000 k, CPU window 3000 ms).
Injected errors (all default to none): cpu_bias, mem_bias, freq_bias_khz, temp_bias_c added to what the
sender reports; dropped sender messages; dropped or nulled reference records.
No addresses, tokens or network names appear anywhere.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

T0 = 1_791_000_000_000
PROBE_OFFSET_MS = -58       # receiver clock minus Pi clock, from the one probe in the first message
NET_MS = 15                 # sender -> receiver, true one-way
ACK_MS = 110                # receiver -> Ditto acknowledgement
MEM_TOTAL_KIB = 1_000_000
BASE_USED_KIB = 200_000
VMRSS_BASELINE_KIB = 9_000

PHASES = [
    ("idle0", "idle", 0, 60),
    ("cpu25", "cpu", 25, 60),
    ("cpu75", "cpu", 75, 60),
    ("mem100", "mem", 100, 60),
]


def phase_at(phases: Sequence[Tuple[str, str, float, float]], t_s: float) -> Tuple[str, float]:
    """(kind, level) in force at t seconds after T0."""
    start = 0.0
    for _, kind, level, duration in phases:
        if t_s < start + duration:
            return kind, level
        start += duration
    return "idle", 0


def true_cpu_pct(phases, second: int, background: float = 0.0) -> float:
    """CPU percent in force during [second, second + 1): the stimulus load plus a steady background."""
    kind, level = phase_at(phases, second + 0.5)
    return (float(level) if kind == "cpu" else 0.0) + background


def true_used_kib(phases, t_s: float) -> int:
    kind, level = phase_at(phases, t_s)
    return BASE_USED_KIB + (int(level * 1024) if kind == "mem" else 0)


def total_seconds(phases) -> int:
    return int(sum(p[3] for p in phases))


# -------------------------------------------------------------------------------------------------- stimulus
def stimulus_records(phases=PHASES, seed=None, achieved: Optional[Dict[str, float]] = None,
                     interrupted: Iterable[str] = (), alloc_wall_s: Optional[float] = None, vmrss_extra_kib: int = 0) -> List[dict]:
    achieved = achieved or {}
    records = [{"type": "start", "format": 1, "t_epoch_ms": T0 - 1000, "t_mono_ns": 1, "seed": seed,
                "order": [p[0] for p in phases], "n_workers": 4, "n_cpus": 4, "pinned_cpus": [0, 1, 2, 3],
                "vmrss_kib_baseline": VMRSS_BASELINE_KIB}]
    t = 0
    for i, (name, kind, level, duration) in enumerate(phases):
        records.append({"type": "phase_start", "index": i, "name": name, "kind": kind, "level": level,
                        "duration_s": duration, "t_epoch_ms": T0 + int(t * 1000), "t_mono_ns": 10 + int(t * 1e9)})
        t += duration
        records.append({
            "type": "phase_end", "index": i, "name": name, "kind": kind, "level": level,
            "status": "interrupted" if name in set(interrupted) else "completed", "wall_s": duration,
            "worker_duty": None, "achieved_total_percent": achieved.get(name, float(level) if kind == "cpu" else 0.0),
            "vmrss_kib_mid": VMRSS_BASELINE_KIB + int(level * 1024) + vmrss_extra_kib if kind == "mem" else None,
            "mem_alloc_mib": level if kind == "mem" else None,
            "mem_alloc_wall_s": alloc_wall_s if kind == "mem" else None,
            "t_epoch_ms": T0 + int(t * 1000), "t_mono_ns": 10 + int(t * 1e9)})
    records.append({"type": "end", "reason": "completed", "phases_completed": len(phases),
                    "t_epoch_ms": T0 + int(t * 1000), "t_mono_ns": 99})
    return records


# -------------------------------------------------------------------------------------------------- reference
def reference_records(phases=PHASES, drop_seconds: Iterable[int] = (), null_proc_stat: Iterable[int] = (),
                      null_freq: Iterable[int] = (), freq_khz: int = 600_000, temp_millic: int = 41_318,
                      vcg_temp_c: float = 40.8, vcg_hz: int = 600_000_000, vcg_every: int = 10,
                      extra_seconds: int = 3, background_pct: float = 0.0) -> List[dict]:
    drop, null_stat, null_f = set(drop_seconds), set(null_proc_stat), set(null_freq)
    n = total_seconds(phases) + extra_seconds
    busy_cum = idle_cum = 0
    records = [{"type": "start", "format": 1, "t_epoch_ms": T0 - 2000, "t_mono_ns": 1, "interval_s": 1.0,
                "vcgencmd_every_s": 10.0, "vcgencmd_available": True, "sources": {}}]
    for k in range(0, n + 1):
        if k in drop:
            continue
        rec = {"type": "sample", "seq": k, "t_epoch_ms": T0 + 1000 * k, "t_mono_ns": 10 + 1_000_000_000 * k,
               "t_done_mono_ns": 20 + 1_000_000_000 * k, "process_time_s": 0.002 * k,
               "proc_stat": {"cpu": [busy_cum, 0, 0, idle_cum, 0, 0, 0, 0, 0, 0]}, "proc_stat_error": None,
               "meminfo_kib": {"MemTotal": MEM_TOTAL_KIB, "MemFree": 1, "MemAvailable": MEM_TOTAL_KIB - true_used_kib(phases, k)},
               "meminfo_error": None, "cpuinfo_cur_freq_khz": freq_khz, "cpuinfo_cur_freq_error": None,
               "thermal_zone0_type": "cpu-thermal", "thermal_zone0_temp_millic": temp_millic, "thermal_error": None,
               "vcgencmd_calls": 0}
        if k in null_stat:
            rec["proc_stat"], rec["proc_stat_error"] = None, "PermissionError: simulated"
        if k in null_f:
            rec["cpuinfo_cur_freq_khz"], rec["cpuinfo_cur_freq_error"] = None, "PermissionError: simulated"
        if k % vcg_every == 0:
            rec["vcgencmd"] = {"arm_clock_raw": "frequency(48)=%d" % vcg_hz, "arm_clock_hz": vcg_hz, "arm_clock_error": None,
                               "temp_raw": "temp=%.1f'C" % vcg_temp_c, "temp_c": vcg_temp_c, "temp_error": None}
        records.append(rec)
        # counters for the NEXT second use that second's percentage
        busy = int(round(4 * true_cpu_pct(phases, k, background_pct)))
        busy_cum += busy
        idle_cum += 400 - busy
    return records


# -------------------------------------------------------------------------------------------------- receiver
def window_average_pct(phases, start_ms: float, end_ms: float, background: float = 0.0) -> float:
    """True CPU percent over [start_ms, end_ms] (ms after T0): the counters are linear within each second."""
    total = 0.0
    second = int(start_ms // 1000)
    while second * 1000 < end_ms:
        overlap = min(end_ms, (second + 1) * 1000) - max(start_ms, second * 1000)
        if overlap > 0:
            total += true_cpu_pct(phases, second, background) * overlap
        second += 1
    return total / (end_ms - start_ms)


def receiver_records(phases=PHASES, cpu_bias=0.0, mem_bias=0.0, freq_bias_khz=0, temp_bias_c=0.0,
                     drop_messages: Iterable[int] = (), dry_run=False, instant_offset_ms=1, cpu_edge_offset_ms=0,
                     cpu_override: Optional[Dict[int, float]] = None, mem_override: Optional[Dict[int, float]] = None,
                     freq_khz=600_000, temp_c=41.318, unpatched: Iterable[int] = (),
                     stale_at: Iterable[int] = (), background_pct: float = 0.0,
                     window_missing: Iterable[int] = ()) -> List[dict]:
    """cpu_edge_offset_ms shifts every stamp, so the CPU window edges fall between reference records."""
    drop, unpatched, stale_at, window_missing = set(drop_messages), set(unpatched), set(stale_at), set(window_missing)
    cpu_override, mem_override = cpu_override or {}, mem_override or {}
    records = [{"type": "start", "started_at_ms": T0 + PROBE_OFFSET_MS - 5000, "protocol": 1, "thing_id": "x", "dry_run": dry_run}]
    n = total_seconds(phases) // 3
    for k in range(1, n + 1):
        if k in drop:
            continue
        rel_end = 3000 * k + cpu_edge_offset_ms
        measured = T0 + rel_end
        cpu_true = window_average_pct(phases, rel_end - 3000, rel_end, background_pct)
        cpu_val = cpu_override.get(k, round(cpu_true + cpu_bias, 2))
        t_inst = measured + instant_offset_ms
        used = true_used_kib(phases, (rel_end + instant_offset_ms) / 1000.0)
        mem_val = mem_override.get(k, round(100.0 * used / MEM_TOTAL_KIB + mem_bias, 2))
        sent = measured + 1
        received = sent + PROBE_OFFSET_MS + NET_MS
        probes = []
        if k == 1:
            t1 = T0 + 1000
            probes = [{"t1": t1, "t2": t1 + PROBE_OFFSET_MS + 4, "t3": t1 + PROBE_OFFSET_MS + 4, "t4": t1 + 8,
                       "offset_ms": float(PROBE_OFFSET_MS), "delay_ms": 8}]
        records.append({
            "type": "sample", "session_id": "s", "seq": k, "status": "applied", "missed": 0 if k == 1 else None,
            "sent_at_ms": sent, "received_at_ms": received,
            "patched_at_ms": None if (dry_run or k in unpatched) else received + ACK_MS,
            "ditto": "disabled" if dry_run else ("failed" if k in unpatched else "ok"),
            "ntp_synchronized": True,
            "measurements": {
                "cpu_utilization": {"value": cpu_val, "measured_at_ms": measured, "window_ms": 3000},
                "memory_utilization": {"value": mem_val, "measured_at_ms": t_inst},
                "cpu_temperature_c": {"value": round(temp_c + temp_bias_c, 3), "measured_at_ms": t_inst + 1},
                "cpu_frequency_khz": {"value": freq_khz + freq_bias_khz, "measured_at_ms": t_inst + 1},
            },
            "rejected_features": [], "probes": probes})
        if k in window_missing:  # a CPU measurement that arrived without its averaging window
            del records[-1]["measurements"]["cpu_utilization"]["window_ms"]
        if k in stale_at:  # a late duplicate of the same message with absurd values: must be ignored
            stale = json.loads(json.dumps(records[-1]))
            stale["status"] = "stale"
            stale["measurements"]["cpu_utilization"]["value"] = 999.0
            records.append(stale)
    return records


# -------------------------------------------------------------------------------------------------- files
def write_jsonl(path, records) -> Path:
    path = Path(path)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


FULL_THRESHOLDS = {
    "cpu_bias_abs_max_pp": 1.5, "cpu_mae_max_pp": 1.5, "cpu_p95_abs_error_max_pp": 1.5,
    "memory_bias_abs_max_pp": 1.5, "memory_mae_max_pp": 1.5, "memory_p95_abs_error_max_pp": 1.5,
    "frequency_bias_abs_max_khz": 300000, "frequency_mae_max_khz": 300000, "frequency_p95_abs_error_max_khz": 300000,
    "temperature_bias_abs_max_c": 5.0, "temperature_mae_max_c": 5.0, "temperature_p95_abs_error_max_c": 5.0,
    "cpu_reference_vs_stimulus_abs_max_pp": 1.0, "memory_reference_vs_stimulus_abs_max_mib": 5.0,
    "max_cpu_edge_gap_ms": 10000, "max_instant_distance_1hz_ms": 6000, "max_instant_distance_10s_ms": 6000,
    "step_response_pi_clock_max_ms": 20000, "step_response_twin_visible_max_ms": 20000,
    "min_samples_per_phase": 5,
}
# These numbers exist only so the tests can exercise PASS / FAIL logic. They are test fixtures, not thresholds
# anyone should use: the real thresholds are pre-registered by the person running the experiment.


# -------------------------------------------------------------------------------------------------- counter faults
def _cpu_samples(reference):
    return [r for r in reference if r["type"] == "sample" and r.get("proc_stat")]


def spike_counter(reference, seq: int, extra: int = 10_000_000):
    """One /proc/stat read that is far too high (a glitch); the records around it are normal."""
    for rec in _cpu_samples(reference):
        if rec["seq"] == seq:
            rec["proc_stat"]["cpu"][0] += extra
    return reference


def reset_counter(reference, from_seq: int):
    """The counters restart from zero at `from_seq` (a reboot) and keep rising at the same rate."""
    samples = _cpu_samples(reference)
    base = next(r for r in samples if r["seq"] == from_seq - 1)["proc_stat"]["cpu"][:]
    for rec in samples:
        if rec["seq"] >= from_seq:
            rec["proc_stat"]["cpu"] = [v - b for v, b in zip(rec["proc_stat"]["cpu"], base)]
    return reference


def offline_cpu(reference, from_seq: int, fraction: float = 0.25):
    """One of four CPUs goes offline at `from_seq`: its accumulated ticks leave the aggregate line at once."""
    samples = _cpu_samples(reference)
    base = next(r for r in samples if r["seq"] == from_seq - 1)["proc_stat"]["cpu"][:]
    for rec in samples:
        if rec["seq"] >= from_seq:
            rec["proc_stat"]["cpu"] = [int(v - fraction * b) for v, b in zip(rec["proc_stat"]["cpu"], base)]
    return reference
