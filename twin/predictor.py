"""
Phase 6 extension — lightweight, log-only prediction for the failure detector.

Replaces the pure hold-last-value fallback in twin/failure_detector.py with a
per-feature linear-trend extrapolation over a node's own recent *real* history,
in place of the paper's Kafka-ML model (OpenTwins, Robles et al. 2023,
Computers in Industry 152:104007, Section 3.3: "Kafka-ML will predict the next
state of the system to avoid a service interruption due to the sensor failure").

Deliberately not a trained ML model: this repo has no historical training
corpus to fit one against (see notes/EXPERIMENTAL_TWIN_REQUIREMENTS.md, section
2.2), so "lightweight statistical predictor" means ordinary least-squares
extrapolation over a short rolling window, not a Kafka-ML analog.

This module is pure math and knows nothing about Ditto or the poll loop —
failure_detector.py owns the history buffer (frozen the moment a node goes
quiet, so this always extrapolates real data, never a previous prediction)
and logs the result. Nothing in this module, or in the caller's use of it,
writes anything back to Ditto -- see notes/PREDICTOR_VALIDATION.md for the
explicit check.
"""
from __future__ import annotations

import math

# (low, high) clamp per feature, read directly from twin/publisher.py's
# node_features() rather than guessed. Two different generating processes
# exist there (publisher.py:35-47), so two different valid ranges:
#   node_0 (deterministic ramp/oscillation):
#     cpu = 10.0 + 85.0*progress            -> [10, 95]
#     latency = 5.0 + 115.0*progress        -> [5, 120]
#     mem = 35.0 + 10.0*sin(elapsed/20.0)   -> [25, 45]
#     conns = int(40 + 60*progress)         -> [40, 100]
#   node_1..node_9 (independent random.uniform/randint draws):
#     cpu -> [15, 55], mem -> [20, 70], conns -> [5, 80], latency -> [2, 40]
NODE_0_CLAMPS: dict[str, tuple[float, float]] = {
    "cpu": (10.0, 95.0),
    "mem": (25.0, 45.0),
    "conns": (40.0, 100.0),
    "latency": (5.0, 120.0),
}
NOISE_NODE_CLAMPS: dict[str, tuple[float, float]] = {
    "cpu": (15.0, 55.0),
    "mem": (20.0, 70.0),
    "conns": (5.0, 80.0),
    "latency": (2.0, 40.0),
}


def clamps_for(node_id: str) -> dict[str, tuple[float, float]]:
    return NODE_0_CLAMPS if node_id.endswith(":node_0") else NOISE_NODE_CLAMPS


def _fit_and_extrapolate(samples: list[tuple[float, float]], t: float) -> float:
    """Ordinary least-squares line through (timestamp, value) samples, evaluated at t.

    Falls back to the last value with 1 sample, and to the mean if every
    sample shares the same timestamp (degenerate, avoids a division by zero)."""
    n = len(samples)
    if n == 0:
        return 0.0
    if n == 1:
        return samples[0][1]
    mean_x = sum(x for x, _ in samples) / n
    mean_y = sum(y for _, y in samples) / n
    den = sum((x - mean_x) ** 2 for x, _ in samples)
    if den == 0:
        return mean_y
    num = sum((x - mean_x) * (y - mean_y) for x, y in samples)
    slope = num / den
    intercept = mean_y - slope * mean_x
    return slope * t + intercept


def predict(history: list[tuple[float, dict]], now: float, node_id: str) -> dict:
    """Predict each feature's current value from a node's frozen real history.

    `history` is the (timestamp, {feature: value}) samples collected while the
    node was genuinely reporting. On a pure-noise node (node_1..node_9 -- see
    EXPERIMENTAL_TWIN_REQUIREMENTS.md section 2.3), the fitted slope over a
    small window is small but not exactly zero (least-squares on i.i.d. data
    has no reason to land on precisely 0).

    Extrapolation distance is capped at the width of the observed window
    (don't trust a fitted line further into the future than it was actually
    observed holding true). Past that cap, the frozen line-value decays
    toward the training window's own empirical mean, with a half-life equal
    to the window width -- so a SHORT failure (comparable to or shorter than
    the training window) still gets genuine trend extrapolation (measured
    error ~1-2 units on node_0's linear features), while a LONG, continuous
    failure settles toward the mean instead of sitting frozen at whatever
    value the line happened to reach at the cap.

    This decay was added 2026-09-16 after validation found the previous
    freeze-at-the-cap behavior could land exactly at a clamp boundary on a
    long failure, measurably far from the mean (see
    notes/PREDICTOR_VALIDATION.md, "2026-09-15/16" section for the original
    finding and "2026-09-16, decay fix" for the re-measured result). Re-run
    live on the same long-continuous-pause scenario: decay demonstrably
    closes the gap on the noise nodes it targets (all three re-tested nodes
    moved off their frozen/clamp values and converged toward the mean). On
    `node_0` -- the one node with a genuine sustained trend, not noise -- the
    SAME fix is a measured regression, not a hypothetical one: error grows
    unboundedly with failure duration as decay pulls the prediction toward a
    stale early-window mean while the true ramp keeps climbing, reaching
    -79.70 on `cpu` (vs. a bounded +19.43 worst case before this fix) by 400s
    into a continuous failure. This is a real trade this codebase has chosen
    to accept for now, not an unqualified improvement -- see
    PREDICTOR_VALIDATION.md before assuming decay is strictly better.
    Both effects are measured and reported, not just the favorable one.

    `node_id` selects which feature-range clamp applies (node_0's deterministic
    ramp/oscillation has a different true range than the other nodes' random
    draws -- see clamps_for()).
    """
    if not history:
        return {}
    clamps = clamps_for(node_id)
    features = history[-1][1].keys()
    last_t = history[-1][0]
    first_t = history[0][0]
    window = max(last_t - first_t, 1.0)
    max_t = last_t + window
    eval_t = min(now, max_t)
    excess = max(0.0, now - max_t)  # how far past the cap point `now` is
    decay = 1.0 - math.exp(-excess / window)  # 0 at the cap, -> 1 as excess grows
    predicted = {}
    for feature in features:
        samples = [(ts, vals[feature]) for ts, vals in history if feature in vals]
        frozen_value = _fit_and_extrapolate(samples, eval_t)
        mean_value = sum(y for _, y in samples) / len(samples)
        value = frozen_value + decay * (mean_value - frozen_value)
        lo, hi = clamps.get(feature, (None, None))
        if lo is not None:
            value = max(lo, value)
        if hi is not None:
            value = min(hi, value)
        predicted[feature] = value
    return predicted
