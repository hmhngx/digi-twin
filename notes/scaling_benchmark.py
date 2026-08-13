"""
Phase 9 — scaling benchmark with SEPARATED metrics.

Metric (a) ditto_rtt_s: time from PATCH send until Ditto HTTP response
  (paper-comparable; not affected by Telegraf flush_interval).

Metric (b) e2e_influx_s: time from PATCH send until point visible in InfluxDB
  (includes Telegraf flush_interval batching floor, typically ~10s).

Do not treat (b) as the platform's Ditto scalability ceiling.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from influxdb_client import InfluxDBClient

from twin.config import INFLUX_BUCKET, INFLUX_ORG, INFLUX_TOKEN, INFLUX_URL, NODE_IDS
from twin.ditto_client import ditto, enc

RESULTS_JSON = ROOT / "notes" / "scaling_results.json"
RESULTS_PNG = ROOT / "notes" / "scaling_results.png"
RESULTS_SVG = ROOT / "notes" / "scaling_results.svg"


def patch_marker(node_id: str, marker: float) -> tuple[float, float]:
    """Return (t_send, ditto_rtt_s)."""
    t_send = time.perf_counter()
    body = {
        "cpu_utilization": {"properties": {"value": marker}},
        "memory_utilization": {"properties": {"value": 50.0}},
        "active_connections": {"properties": {"value": 1}},
        "latency_ms": {"properties": {"value": marker}},
    }
    r = ditto(
        "PATCH",
        f"/api/2/things/{enc(node_id)}/features",
        json=body,
        content_type="application/merge-patch+json",
    )
    ditto_rtt = time.perf_counter() - t_send
    if r.status_code >= 400:
        raise RuntimeError(f"PATCH failed {node_id}: {r.status_code} {r.text[:200]}")
    return t_send, ditto_rtt


def wait_influx(thing_id: str, marker: float, t_send: float, timeout: float = 45.0) -> float:
    if not INFLUX_TOKEN:
        raise RuntimeError("INFLUX_TOKEN missing")
    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    api = client.query_api()
    deadline = time.time() + timeout
    lo, hi = marker - 0.05, marker + 0.05
    query = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -3m)
  |> filter(fn: (r) => r._measurement == "mqtt_consumer")
  |> filter(fn: (r) => r._field == "value_cpu_utilization_properties_value")
  |> filter(fn: (r) => r["thingId"] == "{thing_id}")
  |> filter(fn: (r) => r._value >= {lo} and r._value <= {hi})
  |> last()
'''
    while time.time() < deadline:
        tables = api.query(query, org=INFLUX_ORG)
        for table in tables:
            if table.records:
                client.close()
                return time.perf_counter() - t_send
        time.sleep(0.25)
    client.close()
    raise TimeoutError(f"No Influx point for {thing_id} marker={marker}")


def one_update(node_id: str, marker: float) -> dict:
    t_send, ditto_rtt = patch_marker(node_id, marker)
    e2e = wait_influx(node_id, marker, t_send)
    return {"ditto_rtt_s": ditto_rtt, "e2e_influx_s": e2e}


def _summarize(samples: list[dict]) -> dict:
    ditto = [s["ditto_rtt_s"] for s in samples]
    e2e = [s["e2e_influx_s"] for s in samples]
    return {
        "avg_ditto_rtt_s": sum(ditto) / len(ditto),
        "avg_e2e_influx_s": sum(e2e) / len(e2e),
        # Keep legacy key as E2E for backward compatibility, clearly not Ditto RTT
        "avg_latency_s": sum(e2e) / len(e2e),
        "ditto_rtts": ditto,
        "e2e_latencies": e2e,
        "latencies": e2e,
    }


def sensor_count_test(counts=(1, 2, 4, 6, 8, 10)) -> list[dict]:
    results = []
    for n in counts:
        nodes = NODE_IDS[:n]
        marker = 1000.0 + n + (time.time() % 100) / 100.0
        print(f"Sensor-count test n={n} marker={marker:.4f}")
        t0 = time.perf_counter()
        samples: list[dict] = []
        with ThreadPoolExecutor(max_workers=n) as pool:
            futs = {
                pool.submit(one_update, nid, marker + i * 0.001): nid
                for i, nid in enumerate(nodes)
            }
            for fut in as_completed(futs):
                sample = fut.result()
                samples.append(sample)
                print(
                    f"  {futs[fut]} ditto_rtt={sample['ditto_rtt_s']*1000:.1f}ms "
                    f"e2e_influx={sample['e2e_influx_s']:.3f}s"
                )
        wall = time.perf_counter() - t0
        summary = _summarize(samples)
        results.append(
            {
                "test": "sensor_count",
                "n": n,
                "wall_s": wall,
                **summary,
            }
        )
        print(
            f"  avg_ditto_rtt={summary['avg_ditto_rtt_s']*1000:.1f}ms "
            f"avg_e2e={summary['avg_e2e_influx_s']:.3f}s wall={wall:.3f}s"
        )
    return results


def client_count_test(counts=(1, 5, 10, 15, 20)) -> list[dict]:
    results = []
    target = NODE_IDS[0]
    for n in counts:
        base = 2000.0 + n + (time.time() % 50)
        print(f"Client-count test clients={n} target={target}")
        samples: list[dict] = []
        lock = threading.Lock()

        def worker(i: int):
            marker = base + i * 0.01
            sample = one_update(target, marker)
            with lock:
                samples.append(sample)

        t0 = time.perf_counter()
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.perf_counter() - t0
        summary = _summarize(samples) if samples else {
            "avg_ditto_rtt_s": float("nan"),
            "avg_e2e_influx_s": float("nan"),
            "avg_latency_s": float("nan"),
            "ditto_rtts": [],
            "e2e_latencies": [],
            "latencies": [],
        }
        results.append(
            {
                "test": "client_count",
                "n": n,
                "wall_s": wall,
                **summary,
            }
        )
        print(
            f"  avg_ditto_rtt={summary['avg_ditto_rtt_s']*1000:.1f}ms "
            f"avg_e2e={summary['avg_e2e_influx_s']:.3f}s wall={wall:.3f}s"
        )
    return results


def plot_svg(results: list[dict]) -> None:
    sensor = [r for r in results if r["test"] == "sensor_count"]
    client = [r for r in results if r["test"] == "client_count"]

    def series_polyline(xs, ys, x0, y0, w, h, color):
        if not xs:
            return ""
        max_x = max(xs) or 1
        max_y = max(ys + [0.001]) or 1
        pts = []
        for x, y in zip(xs, ys):
            px = x0 + (x / max_x) * w
            py = y0 + h - (y / max_y) * h
            pts.append(f"{px:.1f},{py:.1f}")
        return (
            f'<polyline fill="none" stroke="{color}" stroke-width="2" '
            f'points="{" ".join(pts)}" />'
            + "".join(
                f'<circle cx="{p.split(",")[0]}" cy="{p.split(",")[1]}" r="3" fill="{color}" />'
                for p in pts
            )
        )

    # Two rows: (a) Ditto RTT ms, (b) E2E Influx s
    sx = [r["n"] for r in sensor]
    sy_a = [r["avg_ditto_rtt_s"] * 1000 for r in sensor]
    sy_b = [r["avg_e2e_influx_s"] for r in sensor]
    cx = [r["n"] for r in client]
    cy_a = [r["avg_ditto_rtt_s"] * 1000 for r in client]
    cy_b = [r["avg_e2e_influx_s"] for r in client]
    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="840">
  <rect width="100%" height="100%" fill="#fff"/>
  <text x="50" y="28" font-size="15" font-family="Segoe UI, sans-serif">(a) Ditto PATCH RTT (ms) — paper-comparable</text>
  <text x="550" y="28" font-size="15" font-family="Segoe UI, sans-serif">(a) Client-count Ditto RTT (ms)</text>
  <rect x="50" y="40" width="400" height="280" fill="none" stroke="#ccc"/>
  <rect x="550" y="40" width="400" height="280" fill="none" stroke="#ccc"/>
  {series_polyline(sx, sy_a, 50, 40, 400, 280, "#1f77b4")}
  {series_polyline(cx, cy_a, 550, 40, 400, 280, "#ff7f0e")}
  <text x="50" y="350" font-size="12" font-family="Segoe UI, sans-serif">y = avg Ditto HTTP RTT (ms)</text>

  <text x="50" y="400" font-size="15" font-family="Segoe UI, sans-serif">(b) E2E to Influx (s) — includes Telegraf flush_interval</text>
  <text x="550" y="400" font-size="15" font-family="Segoe UI, sans-serif">(b) Client-count E2E Influx (s)</text>
  <rect x="50" y="420" width="400" height="280" fill="none" stroke="#ccc"/>
  <rect x="550" y="420" width="400" height="280" fill="none" stroke="#ccc"/>
  {series_polyline(sx, sy_b, 50, 420, 400, 280, "#2ca02c")}
  {series_polyline(cx, cy_b, 550, 420, 400, 280, "#d62728")}
  <text x="50" y="730" font-size="12" font-family="Segoe UI, sans-serif">y = avg PATCH→Influx visibility (s); flush_interval typically dominates</text>
</svg>
"""
    RESULTS_SVG.write_text(svg, encoding="utf-8")
    print(f"Wrote {RESULTS_SVG}")

    try:
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(2, 2, figsize=(10, 8))
        axes[0, 0].plot(sx, sy_a, marker="o")
        axes[0, 0].set_title("(a) Sensor-count Ditto RTT")
        axes[0, 0].set_ylabel("Avg RTT (ms)")
        axes[0, 0].grid(True, alpha=0.3)
        axes[0, 1].plot(cx, cy_a, marker="o", color="C1")
        axes[0, 1].set_title("(a) Client-count Ditto RTT")
        axes[0, 1].set_ylabel("Avg RTT (ms)")
        axes[0, 1].axhline(1000.0, color="red", linestyle="--", alpha=0.5, label="1s paper")
        axes[0, 1].legend()
        axes[0, 1].grid(True, alpha=0.3)
        axes[1, 0].plot(sx, sy_b, marker="o", color="C2")
        axes[1, 0].set_title("(b) Sensor-count E2E Influx")
        axes[1, 0].set_xlabel("Concurrent nodes")
        axes[1, 0].set_ylabel("Avg latency (s)")
        axes[1, 0].grid(True, alpha=0.3)
        axes[1, 1].plot(cx, cy_b, marker="o", color="C3")
        axes[1, 1].set_title("(b) Client-count E2E Influx")
        axes[1, 1].set_xlabel("Concurrent clients")
        axes[1, 1].set_ylabel("Avg latency (s)")
        axes[1, 1].grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(RESULTS_PNG, dpi=140)
        print(f"Wrote {RESULTS_PNG}")
    except Exception as e:
        print(f"matplotlib unavailable ({e}); using SVG as chart artifact")
        RESULTS_PNG.write_bytes(RESULTS_SVG.read_bytes())
        print(f"Also wrote chart bytes to {RESULTS_PNG} (SVG content; matplotlib blocked)")


def main() -> None:
    print("Running dual-metric scaling benchmarks...")
    print("  (a) ditto_rtt_s = PATCH -> Ditto HTTP response")
    print("  (b) e2e_influx_s = PATCH -> visible in Influx (Telegraf flush-bounded)")
    results = sensor_count_test() + client_count_test()
    RESULTS_JSON.write_text(json.dumps(results, indent=2), encoding="utf-8")
    plot_svg(results)
    print(f"Wrote {RESULTS_JSON}")


if __name__ == "__main__":
    main()
