"""
Phase 7 — recursive aggregation.

Rack features from child nodes; cluster features from rack twins only
(never re-read all 10 nodes for the cluster level).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twin.config import (
    AGGREGATE_INTERVAL_SEC,
    CLUSTER_ID,
    HEALTHY_CPU_THRESHOLD,
    RACK_IDS,
    RACK_NODES,
)
from twin.ditto_client import ditto, enc


def get_feature(thing_id: str, feature: str):
    r = ditto("GET", f"/api/2/things/{enc(thing_id)}/features/{feature}/properties/value")
    if r.status_code != 200:
        return None
    return r.json()


def patch_features(thing_id: str, features: dict) -> int:
    r = ditto(
        "PATCH",
        f"/api/2/things/{enc(thing_id)}/features",
        json=features,
        content_type="application/merge-patch+json",
    )
    if r.status_code >= 400:
        raise RuntimeError(f"PATCH {thing_id} failed: {r.status_code} {r.text[:300]}")
    return r.status_code


def aggregate_rack(rack_id: str) -> dict:
    nodes = RACK_NODES[rack_id]
    cpus = []
    conns = []
    healthy = 0
    for nid in nodes:
        cpu = get_feature(nid, "cpu_utilization")
        conn = get_feature(nid, "active_connections")
        if cpu is None:
            continue
        cpus.append(float(cpu))
        if conn is not None:
            conns.append(float(conn))
        if float(cpu) < HEALTHY_CPU_THRESHOLD:
            healthy += 1
    avg_cpu = sum(cpus) / len(cpus) if cpus else 0.0
    total_conns = sum(conns) if conns else 0.0
    feats = {
        "avg_cpu_utilization": {"properties": {"value": round(avg_cpu, 2)}},
        "total_active_connections": {"properties": {"value": int(total_conns)}},
        "healthy_node_count": {"properties": {"value": healthy}},
    }
    status = patch_features(rack_id, feats)
    print(
        f"RACK {rack_id}: avg_cpu={avg_cpu:.2f} conns={int(total_conns)} "
        f"healthy={healthy} -> HTTP {status}"
    )
    return {
        "avg_cpu": avg_cpu,
        "conns": total_conns,
        "healthy": healthy,
    }


def aggregate_cluster_from_racks() -> None:
    """Cluster reads rack twins only — not the ten nodes."""
    rack_avgs = []
    rack_conns = []
    rack_healthy = []
    for rid in RACK_IDS:
        avg = get_feature(rid, "avg_cpu_utilization")
        conns = get_feature(rid, "total_active_connections")
        healthy = get_feature(rid, "healthy_node_count")
        if avg is None:
            raise RuntimeError(f"Missing rack aggregate on {rid}")
        rack_avgs.append(float(avg))
        rack_conns.append(float(conns or 0))
        rack_healthy.append(int(healthy or 0))
    cluster_avg = sum(rack_avgs) / len(rack_avgs)
    cluster_conns = sum(rack_conns)
    cluster_healthy = sum(rack_healthy)
    feats = {
        "avg_cpu_utilization": {"properties": {"value": round(cluster_avg, 2)}},
        "total_active_connections": {"properties": {"value": int(cluster_conns)}},
        "healthy_node_count": {"properties": {"value": cluster_healthy}},
    }
    status = patch_features(CLUSTER_ID, feats)
    print(
        f"CLUSTER {CLUSTER_ID}: avg_cpu={cluster_avg:.2f} "
        f"(from racks {rack_avgs}) conns={int(cluster_conns)} "
        f"healthy={cluster_healthy} -> HTTP {status}"
    )


def run(cycles: int | None = None, interval: float = AGGREGATE_INTERVAL_SEC) -> None:
    n = 0
    print(
        f"Aggregator started: interval={interval}s "
        f"healthy_cpu_threshold={HEALTHY_CPU_THRESHOLD}"
    )
    while cycles is None or n < cycles:
        n += 1
        for rid in RACK_IDS:
            aggregate_rack(rid)
        aggregate_cluster_from_racks()
        time.sleep(interval)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--cycles", type=int, default=None)
    p.add_argument("--interval", type=float, default=AGGREGATE_INTERVAL_SEC)
    args = p.parse_args()
    run(cycles=args.cycles, interval=args.interval)


if __name__ == "__main__":
    main()
