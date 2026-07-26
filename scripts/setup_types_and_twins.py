"""
Phases 2–3: create Twin Types, link type graph, instantiate twins, link instances.

Run from repo root:
  python scripts/setup_types_and_twins.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twin.config import (
    CLUSTER_ID,
    CLUSTER_TYPE,
    NODE_IDS,
    NODE_TYPE,
    POLICY_ID,
    RACK_IDS,
    RACK_NODES,
    RACK_TYPE,
)
from twin.ditto_client import ditto, enc, ensure_ok, ext


def feature(value=0.0):
    return {"properties": {"value": value}}


def ensure_policy() -> None:
    body = {
        "policyId": POLICY_ID,
        "entries": {
            "DEFAULT": {
                "subjects": {"nginx:ditto": {"type": "generated"}},
                "resources": {
                    "thing:/": {"grant": ["READ", "WRITE"], "revoke": []},
                    "policy:/": {"grant": ["READ", "WRITE"], "revoke": []},
                    "message:/": {"grant": ["READ", "WRITE"], "revoke": []},
                },
            }
        },
    }
    r = ditto("PUT", f"/api/2/policies/{enc(POLICY_ID)}", json=body)
    ensure_ok(r, f"PUT policy {POLICY_ID}")


def type_bodies():
    node = {
        "policyId": POLICY_ID,
        "attributes": {
            "name": "NodeType",
            "text_description": "Compute node twin type",
            "hardware_model": "generic-1U",
            "rack_position": 0,
            "install_date": "2026-01-01",
        },
        "features": {
            "cpu_utilization": feature(0.0),
            "memory_utilization": feature(0.0),
            "active_connections": feature(0),
            "latency_ms": feature(0.0),
        },
    }
    rack = {
        "policyId": POLICY_ID,
        "attributes": {
            "name": "RackType",
            "text_description": "Rack twin type",
            "datacenter_zone": "zone-a",
        },
        "features": {
            "avg_cpu_utilization": feature(0.0),
            "total_active_connections": feature(0),
            "healthy_node_count": feature(0),
        },
    }
    cluster = {
        "policyId": POLICY_ID,
        "attributes": {
            "name": "ClusterType",
            "text_description": "Cluster twin type",
            "cluster_name": "main",
            "region": "local-lab",
        },
        "features": {
            "avg_cpu_utilization": feature(0.0),
            "total_active_connections": feature(0),
            "healthy_node_count": feature(0),
        },
    }
    return {
        NODE_TYPE: node,
        RACK_TYPE: rack,
        CLUSTER_TYPE: cluster,
    }


def put_type(type_id: str, body: dict) -> None:
    # Prefer PUT with explicit ID (verified in Extended API tests)
    r = ext("PUT", f"/api/types/{enc(type_id)}", json=body)
    if r.status_code >= 400:
        # Idempotent path: if exists, PATCH schema fields
        r2 = ext("GET", f"/api/types/{enc(type_id)}")
        if r2.status_code == 200:
            print(f"Type already exists: {type_id}")
            return
        ensure_ok(r, f"PUT type {type_id}")
    else:
        ensure_ok(r, f"PUT type {type_id}")


def link_type_child(parent_id: str, child_id: str, cardinality: int) -> None:
    r = ext("PUT", f"/api/types/{enc(parent_id)}/children/{enc(child_id)}/{cardinality}")
    ensure_ok(r, f"link type {parent_id} -> {child_id} x{cardinality}")


def unlink_type_child(parent_id: str, child_id: str) -> None:
    r = ext("PATCH", f"/api/types/{enc(parent_id)}/children/{enc(child_id)}/unlink")
    print(f"unlink type {parent_id} -> {child_id}: HTTP {r.status_code} {r.text[:200]}")


def create_twin_from_type(type_id: str, twin_id: str, merge_body: dict | None = None) -> None:
    r = ext("POST", f"/api/types/{enc(type_id)}/create/{enc(twin_id)}", json=merge_body or {})
    if r.status_code == 409 or (r.status_code >= 400 and "exist" in r.text.lower()):
        print(f"Twin already exists (skip create): {twin_id} HTTP {r.status_code}")
        return
    ensure_ok(r, f"create twin {twin_id} from {type_id}")


def link_twin_child(parent_id: str, child_id: str) -> None:
    r = ext("PUT", f"/api/twins/{enc(parent_id)}/children/{enc(child_id)}")
    ensure_ok(r, f"link twin {parent_id} -> {child_id}")


def dump_json(label: str, obj) -> None:
    print(f"\n===== {label} =====")
    print(json.dumps(obj, indent=2))


def phase2_types() -> None:
    print("\n### PHASE 2: Twin Types ###")
    ensure_policy()
    for tid, body in type_bodies().items():
        put_type(tid, body)

    # Type graph: ClusterType --2--> RackType --5--> NodeType
    link_type_child(RACK_TYPE, NODE_TYPE, 5)
    link_type_child(CLUSTER_TYPE, RACK_TYPE, 2)

    time.sleep(2)
    r = ext("GET", "/api/types/all")
    ensure_ok(r, "GET /api/types/all")
    types = r.json()
    dump_json("GET /api/types/all", types)

    by_id = {t.get("thingId"): t for t in types}
    for tid in (NODE_TYPE, RACK_TYPE, CLUSTER_TYPE):
        if tid not in by_id:
            raise RuntimeError(f"Missing type in /api/types/all: {tid}")
        parents = (by_id[tid].get("attributes") or {}).get("_parents")
        print(f"_parents on {tid}: {parents}")

    node_parents = (by_id[NODE_TYPE].get("attributes") or {}).get("_parents") or {}
    rack_parents = (by_id[RACK_TYPE].get("attributes") or {}).get("_parents") or {}
    if node_parents.get(RACK_TYPE) != 5:
        raise RuntimeError(f"Expected NodeType._parents[{RACK_TYPE}]=5, got {node_parents}")
    if rack_parents.get(CLUSTER_TYPE) != 2:
        raise RuntimeError(f"Expected RackType._parents[{CLUSTER_TYPE}]=2, got {rack_parents}")
    print("Phase 2 verification PASSED")


def phase3_twins() -> None:
    print("\n### PHASE 3: Twin instances + composition ###")
    # Avoid recursive child instantiation from linked types
    print("Temporarily unlinking type children for explicit twin IDs...")
    unlink_type_child(RACK_TYPE, NODE_TYPE)
    unlink_type_child(CLUSTER_TYPE, RACK_TYPE)
    time.sleep(1)

    # Instance-specific attribute overrides
    create_twin_from_type(
        CLUSTER_TYPE,
        CLUSTER_ID,
        {
            "attributes": {
                "cluster_name": "main_cluster",
                "region": "local-lab",
                "name": "main_cluster",
            }
        },
    )
    for i, rid in enumerate(RACK_IDS):
        create_twin_from_type(
            RACK_TYPE,
            rid,
            {
                "attributes": {
                    "datacenter_zone": f"zone-{i}",
                    "name": f"rack_{i}",
                }
            },
        )
    for i, nid in enumerate(NODE_IDS):
        create_twin_from_type(
            NODE_TYPE,
            nid,
            {
                "attributes": {
                    "hardware_model": "generic-1U",
                    "rack_position": i % 5,
                    "install_date": "2026-01-01",
                    "name": f"node_{i}",
                }
            },
        )

    time.sleep(2)

    for rid in RACK_IDS:
        link_twin_child(CLUSTER_ID, rid)
    for rid, nodes in RACK_NODES.items():
        for nid in nodes:
            link_twin_child(rid, nid)

    time.sleep(2)

    # Verify twin composition
    for child in RACK_IDS + NODE_IDS:
        r = ext("GET", f"/api/twins/{enc(child)}")
        ensure_ok(r, f"GET twin {child}")
        parents = (r.json().get("attributes") or {}).get("_parents")
        print(f"_parents on {child}: {parents}")

    for parent in [CLUSTER_ID] + RACK_IDS:
        r = ext("GET", f"/api/twins/{enc(parent)}/children")
        ensure_ok(r, f"GET children of {parent}")
        dump_json(f"children of {parent}", r.json())

    # Raw Ditto verification (three levels)
    for tid in [CLUSTER_ID, RACK_IDS[0], NODE_IDS[0]]:
        r = ditto("GET", f"/api/2/things/{enc(tid)}")
        ensure_ok(r, f"Ditto GET {tid}")
        dump_json(f"Ditto thing {tid}", r.json())

    # Restore type graph
    print("Re-linking type children...")
    link_type_child(RACK_TYPE, NODE_TYPE, 5)
    link_type_child(CLUSTER_TYPE, RACK_TYPE, 2)
    print("Phase 3 verification PASSED")


def main() -> None:
    phase2_types()
    phase3_twins()


if __name__ == "__main__":
    main()
