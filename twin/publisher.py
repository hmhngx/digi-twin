"""
Phase 4 — telemetry publisher.

Inbound path (simplified): direct Ditto HTTP PATCH of node features every ~3s.
Auth: ditto:ditto. No MQTT inbound.
Honors notes/_paused_nodes.json so the failure detector can pause one node.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twin.config import NODE_IDS, PUBLISH_INTERVAL_SEC
from twin.ditto_client import ditto, enc

PAUSE_FILE = ROOT / "notes" / "_paused_nodes.json"


def load_paused() -> set[str]:
    if not PAUSE_FILE.exists():
        return set()
    return set(json.loads(PAUSE_FILE.read_text(encoding="utf-8")))


def node_features(node_id: str, t0: float) -> dict:
    elapsed = time.time() - t0
    if node_id.endswith(":node_0"):
        # Deliberate multi-minute ramp for defense demos
        # cpu: 10% -> ~95% over ~6 minutes; latency: 5 -> ~120 ms
        progress = min(1.0, elapsed / 360.0)
        cpu = 10.0 + 85.0 * progress
        latency = 5.0 + 115.0 * progress
        mem = 35.0 + 10.0 * math.sin(elapsed / 20.0)
        conns = int(40 + 60 * progress)
    else:
        cpu = random.uniform(15.0, 55.0)
        mem = random.uniform(20.0, 70.0)
        conns = random.randint(5, 80)
        latency = random.uniform(2.0, 40.0)

    return {
        "cpu_utilization": {"properties": {"value": round(cpu, 2)}},
        "memory_utilization": {"properties": {"value": round(mem, 2)}},
        "active_connections": {"properties": {"value": conns}},
        "latency_ms": {"properties": {"value": round(latency, 2)}},
    }


def patch_node(node_id: str, features: dict) -> int:
    r = ditto(
        "PATCH",
        f"/api/2/things/{enc(node_id)}/features",
        json=features,
        content_type="application/merge-patch+json",
    )
    if r.status_code >= 400:
        raise RuntimeError(f"PATCH {node_id} failed: HTTP {r.status_code} {r.text[:400]}")
    return r.status_code


def run(cycles: int | None = None, interval: float = PUBLISH_INTERVAL_SEC) -> None:
    t0 = time.time()
    n = 0
    print(f"Publisher started: {len(NODE_IDS)} nodes, interval={interval}s")
    while cycles is None or n < cycles:
        n += 1
        paused = load_paused()
        for node_id in NODE_IDS:
            if node_id in paused:
                print(f"[{n}] SKIP paused {node_id}")
                continue
            feats = node_features(node_id, t0)
            status = patch_node(node_id, feats)
            cpu = feats["cpu_utilization"]["properties"]["value"]
            print(f"[{n}] PATCH {node_id} cpu={cpu} -> HTTP {status}")
        time.sleep(interval)


def main() -> None:
    p = argparse.ArgumentParser(description="Cluster twin telemetry publisher")
    p.add_argument("--cycles", type=int, default=None, help="Finite cycles (default: forever)")
    p.add_argument("--interval", type=float, default=PUBLISH_INTERVAL_SEC)
    args = p.parse_args()
    run(cycles=args.cycles, interval=args.interval)


if __name__ == "__main__":
    main()
