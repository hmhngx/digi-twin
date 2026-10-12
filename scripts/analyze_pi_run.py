"""
Analyze a receiver run log (notes/_pi_runs/receiver_<UTC>.jsonl) for the four
fidelity measurements defined in notes/PI_TELEMETRY_SPEC_2026-10-11.md section 10:
latency, accuracy, detectability, consistency.

    python scripts/analyze_pi_run.py notes/_pi_runs/receiver_20261011T201500Z.jsonl
    python scripts/analyze_pi_run.py RUNLOG --silence-threshold-s 9 --json result.json

Offline and read-only: it only reads the log file. Percentiles are nearest-rank.
Clock offsets are always RECOMPUTED here from the four probe timestamps with the
shared formula in twin/pi_protocol.py; values stored in the log are not trusted.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twin import pi_protocol as pp  # noqa: E402

DRIFT_BUCKET_MS = 60_000
DRIFT_MIN_BUCKETS = 3
DRIFT_MIN_SPAN_MS = 120_000
ALIVE_STATUSES = ("applied", "stale", "stale_session")  # a message that carried new information


# --------------------------------------------------------------------------
# Loading and basic statistics
# --------------------------------------------------------------------------
def load_run(path: Any) -> Tuple[List[Dict[str, Any]], int]:
    """Read a JSONL run log. Unparsable lines (e.g. a line cut short when the
    receiver was killed) are skipped and counted, never fatal."""
    records: List[Dict[str, Any]] = []
    bad = 0
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(obj, dict):
                records.append(obj)
            else:
                bad += 1
    return records, bad


def percentile(values: Iterable[float], p: float) -> Optional[float]:
    """Nearest-rank percentile: the value at rank ceil(p/100 * n)."""
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(p * len(ordered) / 100.0))
    return ordered[rank - 1]


def summarize(values: Iterable[float]) -> Dict[str, Any]:
    data = list(values)
    if not data:
        return {"n": 0}
    return {
        "n": len(data),
        "min": min(data),
        "p50": percentile(data, 50),
        "p95": percentile(data, 95),
        "p99": percentile(data, 99),
        "max": max(data),
        "mean": round(sum(data) / len(data), 3),
        "stdev": round(statistics.stdev(data), 3) if len(data) > 1 else 0.0,
    }


# --------------------------------------------------------------------------
# Clock probes
# --------------------------------------------------------------------------
def collect_probes(samples: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """All distinct probes in the log with offset/delay recomputed. Probes with a
    negative delay (a clock step or quantization during the probe) are excluded and counted."""
    seen = set()
    valid: List[Dict[str, Any]] = []
    negative = 0
    for sample in samples:
        for probe in sample.get("probes") or []:
            try:
                key = tuple(int(probe[k]) for k in ("t1", "t2", "t3", "t4"))
            except (KeyError, TypeError, ValueError):
                continue
            if key in seen:
                continue
            seen.add(key)
            offset, delay = pp.compute_offset_delay(*key)
            if delay < 0:
                negative += 1
                continue
            valid.append({"t1": key[0], "t2": key[1], "t3": key[2], "t4": key[3],
                          "offset_ms": offset, "delay_ms": delay, "at_ms": key[1]})
    return valid, negative


def pick_offset(probes: List[Dict[str, Any]], at_ms: int, window_ms: float) -> Optional[Tuple[float, int, bool]]:
    """(offset_ms, delay_ms, used_fallback) for a sample received at `at_ms` (receiver
    clock): the minimum-delay probe within +/- window_ms, else the best probe of the
    whole run (flagged). The offset is receiver clock minus Pi clock."""
    if not probes:
        return None
    near = [p for p in probes if abs(p["at_ms"] - at_ms) <= window_ms]
    if near:
        best = min(near, key=lambda p: (p["delay_ms"], abs(p["at_ms"] - at_ms)))
        return best["offset_ms"], best["delay_ms"], False
    best = min(probes, key=lambda p: p["delay_ms"])
    return best["offset_ms"], best["delay_ms"], True


def estimate_drift_ppm(probes: List[Dict[str, Any]]) -> Optional[float]:
    """Indicative clock drift: least-squares slope of offset against time, using the
    minimum-delay probe of each minute. Needs >= 3 distinct minutes spanning >= 2 min."""
    best_per_bucket: Dict[int, Dict[str, Any]] = {}
    for p in probes:
        bucket = p["at_ms"] // DRIFT_BUCKET_MS
        if bucket not in best_per_bucket or p["delay_ms"] < best_per_bucket[bucket]["delay_ms"]:
            best_per_bucket[bucket] = p
    chosen = list(best_per_bucket.values())
    if len(chosen) < DRIFT_MIN_BUCKETS:
        return None
    if max(p["at_ms"] for p in chosen) - min(p["at_ms"] for p in chosen) < DRIFT_MIN_SPAN_MS:
        return None
    xs = [p["at_ms"] / 1000.0 for p in chosen]
    ys = [p["offset_ms"] for p in chosen]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None
    slope_ms_per_s = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
    return slope_ms_per_s * 1000.0  # 1 ms/s = 1000 ppm


# --------------------------------------------------------------------------
# Analysis sections
# --------------------------------------------------------------------------
def _by_time(samples: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(samples, key=lambda s: (s.get("received_at_ms", 0), s.get("seq", 0)))


def _latency(applied: List[Dict[str, Any]], probes: List[Dict[str, Any]], window_ms: float) -> Dict[str, Any]:
    age: List[float] = []
    net: List[float] = []
    ditto_hop: List[float] = []
    e2e: List[float] = []
    by_feature: Dict[str, List[float]] = {name: [] for name in pp.FEATURES}
    fallback = 0
    without_offset = 0
    for s in applied:
        sent, received, patched = s["sent_at_ms"], s["received_at_ms"], s.get("patched_at_ms")
        measurements = s.get("measurements") or {}
        for m in measurements.values():
            age.append(sent - m["measured_at_ms"])
        chosen = pick_offset(probes, received, window_ms)
        if chosen is None:
            without_offset += 1
        else:
            offset, _, used_fallback = chosen
            fallback += 1 if used_fallback else 0
            net.append(received - sent - offset)
        if patched is not None:
            ditto_hop.append(patched - received)
            if chosen is not None:
                for name, m in measurements.items():
                    value = patched - (m["measured_at_ms"] + chosen[0])
                    e2e.append(value)
                    by_feature.setdefault(name, []).append(value)
    return {
        "age_at_send_ms": summarize(age),
        "net_ms": summarize(net),
        "ditto_ms": summarize(ditto_hop),
        "end_to_end_ms": summarize(e2e),
        "end_to_end_by_feature_ms": {name: summarize(vals) for name, vals in by_feature.items()},
        "offset_fallback_samples": fallback,
        "samples_without_offset": without_offset,
    }


def _detectability(samples: List[Dict[str, Any]], applied: List[Dict[str, Any]], threshold_s: float) -> Dict[str, Any]:
    alive = _by_time([s for s in samples if s.get("status") in ALIVE_STATUSES])
    episodes = []
    longest = None
    for prev, nxt in zip(alive, alive[1:]):
        gap_ms = nxt["received_at_ms"] - prev["received_at_ms"]
        longest = gap_ms if longest is None else max(longest, gap_ms)
        if gap_ms > threshold_s * 1000.0:
            episodes.append({
                "after_received_ms": prev["received_at_ms"],
                "before_received_ms": nxt["received_at_ms"],
                "gap_s": round(gap_ms / 1000.0, 3),
                "flagged_after_s": threshold_s,
            })
    ordered = _by_time(applied)
    longest_missing: Dict[str, int] = {}
    omitted_fraction: Dict[str, Optional[float]] = {}
    for name in pp.FEATURES:
        run = current = missing = 0
        for s in ordered:
            if name in (s.get("measurements") or {}):
                current = 0
            else:
                current += 1
                missing += 1
                run = max(run, current)
        longest_missing[name] = run
        omitted_fraction[name] = round(missing / len(ordered), 4) if ordered else None
    return {
        "threshold_s": threshold_s,
        "silence_episodes": episodes,
        "longest_gap_s": round(longest / 1000.0, 3) if longest is not None else None,
        "per_feature_longest_missing_run": longest_missing,
        "per_feature_omitted_fraction": omitted_fraction,
    }


def _consistency(applied: List[Dict[str, Any]]) -> Dict[str, Any]:
    ordered = _by_time(applied)
    complete = sum(1 for s in ordered if all(name in (s.get("measurements") or {}) for name in pp.FEATURES))
    skew: List[float] = []
    windows: List[float] = []
    intervals: List[float] = []
    last_cpu_stamp: Dict[str, int] = {}
    for s in ordered:
        measurements = s.get("measurements") or {}
        stamps = [m["measured_at_ms"] for m in measurements.values()]
        if len(stamps) >= 2:
            skew.append(max(stamps) - min(stamps))
        cpu = measurements.get("cpu_utilization")
        if cpu is not None:
            if "window_ms" in cpu:
                windows.append(cpu["window_ms"])
            session = s.get("session_id", "")
            if session in last_cpu_stamp:
                intervals.append(cpu["measured_at_ms"] - last_cpu_stamp[session])
            last_cpu_stamp[session] = cpu["measured_at_ms"]
    return {
        "bundle_complete_fraction": round(complete / len(ordered), 4) if ordered else None,
        "intra_message_skew_ms": summarize(skew),
        "cpu_interval_ms": summarize(intervals),
        "cpu_window_ms": summarize(windows),
    }


def analyze(records: List[Dict[str, Any]], silence_threshold_s: float = 9.0,
            offset_window_s: float = 300.0, bad_lines: int = 0) -> Dict[str, Any]:
    samples = [r for r in records if r.get("type") == "sample"]
    start = next((r for r in records if r.get("type") == "start"), None)
    applied = [s for s in samples if s.get("status") == "applied"]
    counts = {status: sum(1 for s in samples if s.get("status") == status)
              for status in ("applied", "duplicate", "stale", "stale_session")}
    counts["missed_total"] = sum(s.get("missed") or 0 for s in applied)
    ditto = {state: sum(1 for s in samples if s.get("ditto") == state)
             for state in ("ok", "failed", "skipped", "disabled")}
    valid, negative = collect_probes(samples)
    best = min(valid, key=lambda p: p["delay_ms"]) if valid else None
    ntp = {"true": 0, "false": 0, "unknown": 0}
    for s in applied:
        flag = s.get("ntp_synchronized")
        ntp["true" if flag is True else "false" if flag is False else "unknown"] += 1
    received = [s["received_at_ms"] for s in samples if "received_at_ms" in s]
    return {
        "run": {
            "samples_logged": len(samples),
            "sessions": len({s.get("session_id") for s in samples}),
            "dry_run": bool(start.get("dry_run")) if start else None,
            "first_received_ms": min(received) if received else None,
            "last_received_ms": max(received) if received else None,
            "duration_s": round((max(received) - min(received)) / 1000.0, 3) if received else None,
            "unparsable_lines": bad_lines,
        },
        "sequence": counts,
        "ditto": ditto,
        "clock": {
            "probes_valid": len(valid),
            "probes_negative_delay": negative,
            "best_probe": ({"offset_ms": best["offset_ms"], "delay_ms": best["delay_ms"], "at_ms": best["at_ms"]}
                           if best else None),
            "delay_ms": summarize(p["delay_ms"] for p in valid),
            "offset_ms": summarize(p["offset_ms"] for p in valid),
            "drift_ppm": estimate_drift_ppm(valid),
            "ntp": ntp,
        },
        "latency": _latency(applied, valid, offset_window_s * 1000.0),
        "accuracy": {
            "status": "not measured (no reference log): accuracy needs an independent reference reading "
                      "of the same quantity; see spec sections 10.2 and 13",
        },
        "detectability": _detectability(samples, applied, silence_threshold_s),
        "consistency": _consistency(applied),
    }


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
def _line(summary: Dict[str, Any], unit: str = "ms") -> str:
    if not summary.get("n"):
        return "n=0 (not computable)"
    return (f"n={summary['n']} min={summary['min']:.1f} p50={summary['p50']:.1f} p95={summary['p95']:.1f} "
            f"p99={summary['p99']:.1f} max={summary['max']:.1f} mean={summary['mean']:.1f} {unit}")


def format_report(r: Dict[str, Any]) -> str:
    run, seq, d = r["run"], r["sequence"], r["ditto"]
    clock, lat, det, con = r["clock"], r["latency"], r["detectability"], r["consistency"]
    out: List[str] = []
    out.append("== Run ==")
    out.append(f"samples logged: {run['samples_logged']}  sessions: {run['sessions']}  "
               f"duration: {run['duration_s']} s  dry-run: {run['dry_run']}  unparsable lines: {run['unparsable_lines']}")
    out.append("== Sequence health ==")
    out.append(f"applied={seq['applied']} duplicate={seq['duplicate']} stale={seq['stale']} "
               f"stale_session={seq['stale_session']} missed(gaps)={seq['missed_total']}")
    out.append(f"ditto: ok={d['ok']} failed={d['failed']} skipped={d['skipped']} disabled={d['disabled']}")
    out.append("== Clock (receiver minus Pi; bound on offset error = delay/2) ==")
    out.append(f"probes: valid={clock['probes_valid']} negative-delay(excluded)={clock['probes_negative_delay']}")
    if clock["best_probe"]:
        out.append(f"best probe: offset={clock['best_probe']['offset_ms']:.1f} ms  delay={clock['best_probe']['delay_ms']} ms")
    out.append("probe delay: " + _line(clock["delay_ms"]))
    out.append("probe offset spread: " + _line(clock["offset_ms"]))
    out.append("drift: " + ("not enough probes" if clock["drift_ppm"] is None else f"{clock['drift_ppm']:.1f} ppm (indicative; positive = the receiver's clock is gaining on the Pi's)"))
    out.append(f"NTP flag on applied messages: {clock['ntp']}")
    out.append("== Latency ==")
    out.append("age at send (Pi clock):      " + _line(lat["age_at_send_ms"]))
    out.append("network, offset-corrected:   " + _line(lat["net_ms"]))
    out.append("receiver -> Ditto ack:        " + _line(lat["ditto_ms"]))
    out.append("end to end (per measurement): " + _line(lat["end_to_end_ms"]))
    for name, summary in lat["end_to_end_by_feature_ms"].items():
        out.append(f"  {name}: " + _line(summary))
    out.append(f"samples using the whole-run best probe (no probe in window): {lat['offset_fallback_samples']}; "
               f"samples with no probe at all: {lat['samples_without_offset']}")
    out.append("(Ditto -> Mosquitto -> Telegraf -> InfluxDB is not measured by this pipeline.)")
    out.append("== Accuracy ==")
    out.append(r["accuracy"]["status"])
    out.append("== Detectability ==")
    out.append(f"silence threshold: {det['threshold_s']} s  longest gap between messages: {det['longest_gap_s']} s  "
               f"episodes: {len(det['silence_episodes'])}")
    for ep in det["silence_episodes"]:
        out.append(f"  gap {ep['gap_s']} s (would be flagged {ep['flagged_after_s']} s after the last message)")
    out.append(f"longest run of consecutive messages missing each feature: {det['per_feature_longest_missing_run']}")
    out.append("== Consistency ==")
    out.append(f"messages carrying all four measurements: {con['bundle_complete_fraction']}")
    out.append("skew between measurements in one message: " + _line(con["intra_message_skew_ms"]))
    out.append("CPU sampling interval:        " + _line(con["cpu_interval_ms"]))
    out.append("CPU averaging window:         " + _line(con["cpu_window_ms"]))
    out.append("")
    out.append("Record with these numbers: network = phone hotspot (variable Wi-Fi latency), date/time, how the "
               "laptop was connected, any disconnects. Report latency as a distribution under those conditions.")
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Analyze a Pi receiver run log (offline, read-only).")
    p.add_argument("runlog", help="JSONL file written by twin/pi_receiver.py")
    p.add_argument("--silence-threshold-s", type=float, default=9.0,
                   help="gap that counts as silence (default 9 = FAILURE_TOLERANCE_SEC's default)")
    p.add_argument("--offset-window-s", type=float, default=300.0,
                   help="use the best probe within this many seconds of a sample (default 300)")
    p.add_argument("--json", default=None, help="also write the full result to this file")
    args = p.parse_args(argv)
    if not Path(args.runlog).is_file():
        p.error(f"run log not found: {args.runlog}")
    records, bad = load_run(args.runlog)
    result = analyze(records, args.silence_threshold_s, args.offset_window_s, bad)
    if result["run"]["samples_logged"] == 0:
        print("No samples in this run log (was the receiver started, and did the Pi connect?).")
        return 1
    print(format_report(result))
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
