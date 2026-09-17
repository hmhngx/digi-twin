"""
Hardware measurement plan, item (b) -- real sensor read jitter at rest, and
whether it would survive the actual UNCHANGED_TOLERANCE / _within_tolerance()
logic in twin/failure_detector.py.

Run this ON the Raspberry Pi 5 itself (Linux-only: reads /proc and /sys
paths that do not exist on the laptop). Run it AT REST -- no stress-ng, no
deliberate load, nothing else competing for the CPU -- because the question
this answers is "how much does an ostensibly-stable healthy reading wobble
on its own," which a loaded Pi would not tell you.

    python3 notes/pi_idle_jitter_measurement.py --duration 1800

This does two things, not one:
  1. Records real cpu/mem/temp readings every ~3s (matching PUBLISH_INTERVAL_SEC)
     and reports the observed jitter between consecutive samples.
  2. Feeds that exact same sequence through the REAL, imported
     twin.failure_detector._within_tolerance() and UNCHANGED_TOLERANCE --
     not a reimplementation -- to answer the actual question: would the
     deployed code treat this real, healthy signal as "unchanged" for long
     enough to misfire?

See notes/HARDWARE_MEASUREMENT_PLAN.md item (b) for the concrete thresholds
and for why "misfire" here specifically means a false FAILED, not just a
wide number.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if not Path("/proc/stat").exists():
    sys.exit(
        "This script reads /proc and /sys paths that only exist on Linux "
        "(the Pi itself). It cannot run on the Windows dev machine -- that's "
        "expected, not a bug. Run it on the Pi once it's reachable."
    )

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twin.failure_detector import FEATURES, UNCHANGED_TOLERANCE, _within_tolerance  # noqa: E402

RESULTS_JSON = ROOT / "notes" / "pi_idle_jitter_results.json"
THERMAL_ZONE = Path("/sys/class/thermal/thermal_zone0/temp")


def read_cpu_ticks() -> tuple[int, int]:
    """Return (idle_ticks, total_ticks) from the first 'cpu' line of /proc/stat."""
    with open("/proc/stat") as f:
        parts = f.readline().split()
    vals = [int(x) for x in parts[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    total = sum(vals)
    return idle, total


def read_mem_pct() -> float:
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            info[k] = int(v.strip().split()[0])  # kB
    total = info["MemTotal"]
    avail = info.get("MemAvailable", info["MemFree"])
    return 100.0 * (total - avail) / total


def read_temp_c() -> float:
    return int(THERMAL_ZONE.read_text().strip()) / 1000.0


def sample_once(prev_ticks: tuple[int, int] | None) -> tuple[dict, tuple[int, int]]:
    idle, total = read_cpu_ticks()
    if prev_ticks is None:
        cpu_pct = 0.0
    else:
        d_idle = idle - prev_ticks[0]
        d_total = total - prev_ticks[1]
        cpu_pct = 100.0 * (1 - d_idle / d_total) if d_total > 0 else 0.0
    reading = {
        "cpu": round(cpu_pct, 3),
        "mem": round(read_mem_pct(), 3),
        "temp_c": round(read_temp_c(), 3),
    }
    return reading, (idle, total)


def run(duration_s: float, interval_s: float) -> list[dict]:
    print(f"Sampling at rest for {duration_s}s at {interval_s}s intervals.")
    print("Do not run stress-ng or anything else deliberately loading the Pi during this.")
    samples: list[dict] = []
    prev_ticks = None
    _, prev_ticks = sample_once(None)  # prime the CPU-tick delta
    t0 = time.time()
    n = 0
    while time.time() - t0 < duration_s:
        time.sleep(interval_s)
        n += 1
        reading, prev_ticks = sample_once(prev_ticks)
        reading["t"] = round(time.time() - t0, 2)
        samples.append(reading)
        if n % 20 == 0:
            print(f"  [{n}] {reading}")
    return samples


def analyze(samples: list[dict]) -> dict:
    """Real consecutive-pair deltas for cpu/mem, plus real temp deltas
    (temp isn't one of failure_detector's four FEATURES today, but it's
    the one new signal a real Pi 5 adds, so it's analyzed the same way for
    when it's wired in)."""
    deltas = {"cpu": [], "mem": [], "temp_c": []}
    for a, b in zip(samples, samples[1:]):
        for k in deltas:
            deltas[k].append(abs(a[k] - b[k]))

    def stats(vals: list[float]) -> dict:
        if not vals:
            return {}
        s = sorted(vals)
        p95 = s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]
        p99 = s[min(len(s) - 1, int(round(0.99 * (len(s) - 1))))]
        return {"max": s[-1], "p99": p99, "p95": p95, "mean": sum(s) / len(s)}

    return {k: stats(v) for k, v in deltas.items()}


def check_structural_gap(samples: list[dict], tolerance_s: float = 9.0) -> dict:
    """The real question: fed through the ACTUAL _within_tolerance() logic
    (not a reimplementation), does this real idle signal ever stay
    'unchanged' long enough that failure_detector.py's own last_seen/age
    mechanism would mark a genuinely healthy, still-reporting node FAILED?

    This mirrors twin/failure_detector.py's run() loop precisely: last_seen
    only advances when _within_tolerance() is False. Constructs synthetic
    'current'/'last_real' dicts using the FEATURES keys failure_detector.py
    actually checks (cpu, mem, conns, latency) -- conns/latency aren't
    produced by this at-rest script, so they're held exactly constant
    (matching a real Pi 5's likely idle behavior for a feature with no
    natural physical analog yet -- see EXPERIMENTAL_TWIN_REQUIREMENTS.md's
    open question on active_connections/latency_ms for a physical node).
    """
    last_real = None
    last_seen_t = None
    max_unbroken_unchanged_s = 0.0
    would_have_failed = False
    for s in samples:
        current = {"cpu": s["cpu"], "mem": s["mem"], "conns": 0, "latency": 0.0}
        if last_real is None or not _within_tolerance(current, last_real):
            last_real = current
            last_seen_t = s["t"]
        else:
            age = s["t"] - last_seen_t
            max_unbroken_unchanged_s = max(max_unbroken_unchanged_s, age)
            if age > tolerance_s:
                would_have_failed = True
    return {
        "max_unbroken_unchanged_s": round(max_unbroken_unchanged_s, 1),
        "tolerance_s": tolerance_s,
        "would_spuriously_fail_a_healthy_node": would_have_failed,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Real Pi idle sensor jitter, at rest")
    p.add_argument("--duration", type=float, default=1800.0,
                    help="Total measurement window in seconds (default 1800 = 30 min)")
    p.add_argument("--interval", type=float, default=3.0,
                    help="Seconds between reads (default 3.0, matches PUBLISH_INTERVAL_SEC)")
    args = p.parse_args()

    samples = run(args.duration, args.interval)
    jitter = analyze(samples)
    gap_check = check_structural_gap(samples)

    RESULTS_JSON.write_text(
        json.dumps(
            {
                "current_tolerance": UNCHANGED_TOLERANCE,
                "raw_samples": samples,
                "consecutive_pair_deltas": jitter,
                "structural_gap_check": gap_check,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print("\n--- Consecutive real-sample jitter at rest ---")
    for k, v in jitter.items():
        print(f"  {k}: {v}")
    print(f"\n--- Current UNCHANGED_TOLERANCE ---\n  {UNCHANGED_TOLERANCE}")
    print("\n--- Structural gap check (does a real stable signal ever get "
          "marked FAILED by the current code, independent of the tolerance "
          "width?) ---")
    for k, v in gap_check.items():
        print(f"  {k}: {v}")
    print(f"\nWrote {RESULTS_JSON}")
    print("Compare against the thresholds in notes/HARDWARE_MEASUREMENT_PLAN.md item (b).")


if __name__ == "__main__":
    main()
