"""
Hardware measurement plan, item (a) — real Pi-to-Ditto PATCH round-trip time.

Run this FROM the Raspberry Pi 5 itself, over its real Wi-Fi connection, once
it can reach the laptop's Ditto port-forward on the LAN (not localhost --
localhost from the Pi would only measure the Pi's own loopback, not the real
network hop this measurement exists to capture).

Point it at the laptop's LAN IP by setting DITTO_HTTP_URL before running, e.g.:

    export DITTO_HTTP_URL=http://192.168.1.50:8081
    python3 notes/pi_rtt_measurement.py --duration 1800

This reuses the exact same ditto() PATCH helper twin/publisher.py itself
calls in production, and the exact same ditto_rtt_s definition (send to
HTTP response) that notes/scaling_benchmark.py already uses for the
synthetic/local baseline in notes/scaling_results.json, so the two are
directly comparable -- this is not a new metric, it's the old metric run
over a new (real) network path.

See notes/HARDWARE_MEASUREMENT_PLAN.md for the concrete pass/fail thresholds.
Requires this repo cloned on the Pi with the same venv/requirements as the
laptop (twin/ditto_client.py, twin/config.py) -- no new dependency.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twin.config import DITTO_HTTP_URL, NODE_IDS
from twin.ditto_client import ditto, enc

RESULTS_JSON = ROOT / "notes" / "pi_rtt_results.json"


def one_patch(node_id: str, marker: float) -> float:
    """Return ditto_rtt_s: wall-clock time from PATCH send to HTTP response.

    Same definition as scaling_benchmark.py's patch_marker() -- kept
    deliberately identical so the two result sets are comparable without
    re-deriving anything.
    """
    body = {
        "cpu_utilization": {"properties": {"value": marker}},
        "memory_utilization": {"properties": {"value": 50.0}},
        "active_connections": {"properties": {"value": 1}},
        "latency_ms": {"properties": {"value": marker}},
    }
    t_send = time.perf_counter()
    r = ditto(
        "PATCH",
        f"/api/2/things/{enc(node_id)}/features",
        json=body,
        content_type="application/merge-patch+json",
    )
    rtt = time.perf_counter() - t_send
    if r.status_code >= 400:
        raise RuntimeError(f"PATCH failed: HTTP {r.status_code} {r.text[:200]}")
    return rtt


def run(duration_s: float, interval_s: float, node_id: str) -> list[float]:
    print(f"Target: {DITTO_HTTP_URL}")
    print(f"Node: {node_id}")
    print(f"Duration: {duration_s}s at {interval_s}s intervals "
          f"(~{int(duration_s / interval_s)} samples)")
    print("This measures real Wi-Fi + OS-scheduling RTT, not the local/synthetic "
          "baseline in notes/scaling_results.json -- run long enough to span "
          "normal network variation, not just a lucky quiet minute.")
    rtts: list[float] = []
    t0 = time.time()
    n = 0
    while time.time() - t0 < duration_s:
        n += 1
        marker = 1000.0 + (n % 1000) * 0.01
        try:
            rtt = one_patch(node_id, marker)
            rtts.append(rtt)
            if n % 20 == 0:
                print(f"  [{n}] last={rtt*1000:.1f}ms elapsed={time.time()-t0:.0f}s")
        except Exception as e:
            print(f"  [{n}] FAILED: {e}")
        time.sleep(max(0.0, interval_s - 0.05))
    return rtts


def summarize(rtts: list[float]) -> dict:
    s = sorted(rtts)

    def pct(p: float) -> float:
        if not s:
            return float("nan")
        idx = min(len(s) - 1, int(round(p * (len(s) - 1))))
        return s[idx]

    return {
        "n": len(s),
        "mean_s": statistics.mean(s) if s else float("nan"),
        "stdev_s": statistics.stdev(s) if len(s) > 1 else 0.0,
        "min_s": s[0] if s else float("nan"),
        "p50_s": pct(0.50),
        "p95_s": pct(0.95),
        "p99_s": pct(0.99),
        "max_s": s[-1] if s else float("nan"),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Real Pi-to-Ditto PATCH RTT over Wi-Fi")
    p.add_argument("--duration", type=float, default=1800.0,
                    help="Total measurement window in seconds (default 1800 = 30 min)")
    p.add_argument("--interval", type=float, default=3.0,
                    help="Seconds between PATCHes (default 3.0, matches PUBLISH_INTERVAL_SEC)")
    p.add_argument("--node", type=str, default=NODE_IDS[-1],
                    help="Thing ID to PATCH (default: last node, to avoid colliding with node_0's ramp demo)")
    args = p.parse_args()

    rtts = run(args.duration, args.interval, args.node)
    summary = summarize(rtts)
    RESULTS_JSON.write_text(
        json.dumps({"raw_rtts_s": rtts, "summary": summary}, indent=2),
        encoding="utf-8",
    )
    print("\n--- Summary ---")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"\nWrote {RESULTS_JSON}")
    print("Compare summary.p95_s and summary.max_s against the thresholds in "
          "notes/HARDWARE_MEASUREMENT_PLAN.md item (a).")


if __name__ == "__main__":
    main()
