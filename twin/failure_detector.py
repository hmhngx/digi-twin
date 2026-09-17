"""
Phase 6 — sensor-failure detection with lightweight, log-only prediction.

Tracks last successful publish timestamp per node (by reading Ditto features
or by watching publisher heartbeats via a shared pause file). Liveness and
change are two different questions, and this module keeps them decoupled on
purpose (a real bug from conflating them, found 2026-09-17, is documented in
detail at the fix site in run() below and in notes/PREDICTOR_VALIDATION.md):
a node is "gone quiet" purely based on whether a poll of it succeeded within
FAILURE_TOLERANCE_SEC, full stop -- never on whether the value it reported
was different from last time. "Unchanged" is a separate, tolerance-band
comparison (not exact equality, notes/EXPERIMENTAL_TWIN_REQUIREMENTS.md
section 3.2 -- exact equality was dormant on synthetic random.uniform() data
but would misfire on a real sensor sitting at physical equilibrium) used only
to decide what counts as a fresh *real sample* worth recording for logging
and prediction, not whether the node is alive.

On timeout, each node's own rolling buffer of recent *real* samples (frozen
the instant it goes quiet) is handed to twin.predictor, which extrapolates a
per-feature linear trend. This is a lightweight, non-ML analog of the paper's
actual mechanism (OpenTwins, Robles et al. 2023, Section 3.3): "Kafka-ML will
predict the next state of the system to avoid a service interruption due to
the sensor failure." Unlike the paper, and unlike this repo's own prior
session in this conversation, this is LOG-ONLY: the prediction is recorded in
notes/_failure_state.json and printed to the console, alongside the actual
last-known real value, and nothing is written back to Ditto. See
notes/PREDICTOR_VALIDATION.md for the validation run and the explicit
no-PATCH-invariant check, and notes/EXPERIMENTAL_TWIN_REQUIREMENTS.md for why
a trained model isn't used here (no historical training corpus exists).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twin.config import FAILURE_TOLERANCE_SEC, NODE_IDS, PUBLISH_INTERVAL_SEC
from twin.ditto_client import ditto, enc
from twin.predictor import predict

PAUSE_FILE = ROOT / "notes" / "_paused_nodes.json"
STATE_FILE = ROOT / "notes" / "_failure_state.json"

FEATURES = ("cpu", "mem", "conns", "latency")
# How much a feature may drift between polls and still count as "unchanged."
# conns is an integer connection count -- real fluctuation there is itself a
# sign of life, so no slack is given. The other three are continuous and get
# a small absolute band, per EXPERIMENTAL_TWIN_REQUIREMENTS.md section 3.2.
# These are chosen starting values, not measured ones -- see PREDICTOR_VALIDATION.md.
UNCHANGED_TOLERANCE = {"cpu": 0.5, "mem": 0.5, "conns": 0, "latency": 0.5}
HISTORY_WINDOW = 10  # real samples kept per node -- gated by the ~3s publish
# cadence (PUBLISH_INTERVAL_SEC), not the ~2s poll loop below, since most polls
# just re-observe the still-current published value. ~30s of real history.


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
    last_real: dict[str, dict] = {}    # last snapshot confirmed to come from the publisher
    history: dict[str, deque] = {n: deque(maxlen=HISTORY_WINDOW) for n in NODE_IDS}
    failed: set[str] = set()
    predicted_now: dict[str, dict] = {}
    t0 = time.time()
    print(
        f"Failure detector started: interval~{PUBLISH_INTERVAL_SEC}s "
        f"tolerance={FAILURE_TOLERANCE_SEC}s (log-only prediction, no Ditto write-back)"
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

            if node_id in paused:
                pass  # do not refresh last_seen/history from polls while paused --
                # this is the deliberate simulated-silence path and must still age out.
            else:
                # Liveness: this poll succeeded just now, independent of whether the
                # value changed. This must NOT be gated by the tolerance check below --
                # a real signal that is genuinely stable (at equilibrium, plateaued,
                # under constant load) will legitimately stay within tolerance
                # forever, and conflating "unchanged" with "no update arrived" marks
                # a live, healthy node FAILED permanently. Found by code review during
                # hardware-measurement planning, 2026-09-17, before any real hardware
                # existed to observe it on; confirmed live both ways: node_0's own
                # plateau (publisher.py holds cpu/latency/conns exactly constant past
                # elapsed=360s) spuriously FAILED under the old code, twice, each time
                # mem's continued sine oscillation lingered near a peak or trough long
                # enough to also stay within tolerance for >9s -- see
                # notes/PREDICTOR_VALIDATION.md for the real run and timestamps.
                last_seen[node_id] = now
                if not _within_tolerance(current, last_real.get(node_id)):
                    # A genuinely new-looking real value -- still used to decide what
                    # counts as "the last known real sample" for logging/state and
                    # what feeds the predictor's history buffer (a classification /
                    # recording decision, not a liveness one -- see
                    # EXPERIMENTAL_TWIN_REQUIREMENTS.md section 3.2).
                    last_real[node_id] = current
                    if node_id in failed:
                        history[node_id].clear()  # don't stitch a fit across the failure gap
                        failed.discard(node_id)
                        predicted_now.pop(node_id, None)
                        print(f"CLEARED failed: {node_id} (updates resumed)")
                    history[node_id].append((now, dict(current)))

            age = now - last_seen[node_id]
            if age > FAILURE_TOLERANCE_SEC:
                if node_id not in failed:
                    failed.add(node_id)
                    print(
                        f"FAILED {node_id}: no real update for {age:.1f}s; "
                        f"predicting (log-only) from {len(history[node_id])} real samples"
                    )
                if len(history[node_id]) >= 2:
                    prediction = predict(list(history[node_id]), now, node_id)
                    predicted_now[node_id] = prediction
                    last = last_real.get(node_id, {})
                    print(
                        f"  {node_id} predicted={{{', '.join(f'{k}={v:.2f}' for k, v in prediction.items())}}} "
                        f"last_known={{{', '.join(f'{k}={v:.2f}' for k, v in last.items())}}}"
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
                    "predicted": {
                        n: {k: round(v, 2) for k, v in p.items()}
                        for n, p in predicted_now.items()
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
