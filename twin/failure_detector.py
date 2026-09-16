"""
Phase 6 — simplified sensor-failure detection.

Tracks last successful publish timestamp per node (by reading Ditto features
or by watching publisher heartbeats via a shared pause file). "Unchanged" is
now a tolerance-band comparison, not exact equality
(notes/EXPERIMENTAL_TWIN_REQUIREMENTS.md section 3.2): exact equality was
dormant against synthetic random.uniform() data (four independent values
colliding exactly, cycle over cycle, is a probability-zero event) but would
misfire on a real sensor sitting at physical equilibrium, where the same
value can legitimately repeat.

Simplification vs paper Section 3.3: on timeout we HOLD last-known feature
values unchanged. The paper predicts a plausible next value via Kafka-ML.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twin.config import FAILURE_TOLERANCE_SEC, NODE_IDS, PUBLISH_INTERVAL_SEC
from twin.ditto_client import ditto, enc

PAUSE_FILE = ROOT / "notes" / "_paused_nodes.json"
STATE_FILE = ROOT / "notes" / "_failure_state.json"

FEATURES = ("cpu", "mem", "conns", "latency")
# How much a feature may drift between polls and still count as "unchanged."
# conns is an integer connection count -- real fluctuation there is itself a
# sign of life, so no slack is given. The other three are continuous and get
# a small absolute band. Chosen starting values, not measured ones -- no real
# hardware exists yet to derive these from.
UNCHANGED_TOLERANCE = {"cpu": 0.5, "mem": 0.5, "conns": 0, "latency": 0.5}


def load_paused() -> set[str]:
    if not PAUSE_FILE.exists():
        return set()
    return set(json.loads(PAUSE_FILE.read_text(encoding="utf-8")))


def save_paused(paused: set[str]) -> None:
    PAUSE_FILE.write_text(json.dumps(sorted(paused), indent=2), encoding="utf-8")


def feature_value(thing: dict, name: str):
    try:
        return thing["features"][name]["properties"]["value"]
    except (KeyError, TypeError):
        return None


def snapshot_node(node_id: str) -> dict | None:
    r = ditto("GET", f"/api/2/things/{enc(node_id)}")
    if r.status_code != 200:
        print(f"WARN GET {node_id}: HTTP {r.status_code}")
        return None
    body = r.json()
    return {
        "cpu": feature_value(body, "cpu_utilization"),
        "mem": feature_value(body, "memory_utilization"),
        "conns": feature_value(body, "active_connections"),
        "latency": feature_value(body, "latency_ms"),
        "etag": r.headers.get("ETag") or r.headers.get("etag"),
        "raw": body,
    }


def _within_tolerance(a: dict | None, b: dict | None) -> bool:
    """True if every feature in `a` is within UNCHANGED_TOLERANCE of `b`."""
    if a is None or b is None:
        return False
    for f in FEATURES:
        av, bv = a.get(f), b.get(f)
        if av is None or bv is None:
            return False
        if abs(av - bv) > UNCHANGED_TOLERANCE[f]:
            return False
    return True


def run(duration: float | None = None) -> None:
    last_seen: dict[str, float] = {n: time.time() for n in NODE_IDS}
    last_real: dict[str, dict] = {}  # last snapshot confirmed to come from the publisher
    failed: set[str] = set()
    t0 = time.time()
    print(
        f"Failure detector started: interval~{PUBLISH_INTERVAL_SEC}s "
        f"tolerance={FAILURE_TOLERANCE_SEC}s"
    )
    print(f"Pause nodes by writing IDs to {PAUSE_FILE} (publisher honors this too).")

    while duration is None or (time.time() - t0) < duration:
        paused = load_paused()
        now = time.time()
        for node_id in NODE_IDS:
            snap = snapshot_node(node_id)
            if snap is None:
                continue
            current = {f: snap[f] for f in FEATURES}

            # If publisher paused this node, features should stop changing
            if node_id in paused:
                # do not refresh last_seen from polls while paused
                pass
            elif not _within_tolerance(current, last_real.get(node_id)):
                last_real[node_id] = current
                last_seen[node_id] = now
                if node_id in failed:
                    failed.discard(node_id)
                    print(f"CLEARED failed: {node_id} (updates resumed)")

            age = now - last_seen[node_id]
            if age > FAILURE_TOLERANCE_SEC and node_id not in failed:
                failed.add(node_id)
                held = last_real.get(node_id, current)
                print(
                    f"FAILED {node_id}: no real update for {age:.1f}s; "
                    f"holding last-known values "
                    f"cpu={held['cpu']} latency={held['latency']}"
                )

        STATE_FILE.write_text(
            json.dumps(
                {
                    "failed": sorted(failed),
                    "paused": sorted(paused),
                    "last_seen_age_s": {
                        n: round(now - last_seen[n], 2) for n in NODE_IDS
                    },
                    "last_known": {
                        n: {k: round(v, 2) for k, v in v_.items()}
                        for n, v_ in last_real.items()
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        time.sleep(2)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=float, default=None)
    p.add_argument("--pause", type=str, default=None, help="Add node id to pause file")
    p.add_argument("--resume", type=str, default=None, help="Remove node id from pause file")
    args = p.parse_args()
    if args.pause:
        paused = load_paused()
        paused.add(args.pause if ":" in args.pause else f"hnguyen.clustertwin:{args.pause}")
        save_paused(paused)
        print(f"Paused: {sorted(paused)}")
        return
    if args.resume:
        paused = load_paused()
        key = args.resume if ":" in args.resume else f"hnguyen.clustertwin:{args.resume}"
        paused.discard(key)
        save_paused(paused)
        print(f"Paused now: {sorted(paused)}")
        return
    run(duration=args.duration)


if __name__ == "__main__":
    main()
