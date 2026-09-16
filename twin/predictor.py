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


def _is_structured(node_id: str) -> bool:
    """True for a node whose signal comes from a known deterministic formula
    rather than independent random draws -- the same distinction
    publisher.py's own node_features() already makes (publisher.py:35 checks
    node_id.endswith(":node_0")). Reused for both the clamp range (below) and
    the post-cap recovery behavior (predict(), below): a structured signal's
    training-window mean is not a meaningful long-run estimate the way it is
    for i.i.d. noise, so it must not be decayed toward."""
    return node_id.endswith(":node_0")


def clamps_for(node_id: str) -> dict[str, tuple[float, float]]:
    return NODE_0_CLAMPS if _is_structured(node_id) else NOISE_NODE_CLAMPS


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
    node was genuinely reporting. Extrapolation distance is always capped at
    the width of the observed window (don't trust a fitted line further into
    the future than it was actually observed holding true).

    Past that cap, recovery behavior is CONDITIONAL on node category
    (`_is_structured()`, added 2026-09-16, reusing the exact same distinction
    publisher.py's own node_features() already makes):

    - Structured node (`node_0`, a deterministic formula, not noise): stays
      FROZEN at the capped line-value, exactly the original (pre-decay)
      behavior. Validated by direct code-path equivalence (decay forced to 0
      is algebraically the same computation as before decay existed) and by
      the real numbers already on record in notes/PREDICTOR_VALIDATION.md's
      first validation run: node_0's checkpoint 1 prediction was measured
      constant (22.16) across four reads spanning elapsed 60-103s, which is
      exactly what "frozen" looks like empirically. Bounded, defensible error
      under a long failure (worst case measured there: +19.43 on `mem`).
      Decaying this node's prediction toward its OWN training-window mean was
      tried and measured worse -- error grew unboundedly (-79.70 on `cpu` by
      400s) because the window's mean is a stale, low point on a still-rising
      ramp, not a meaningful long-run estimate. That's why this branch exists.

    - Any other node (node_1..node_9, i.i.d. random draws -- see
      EXPERIMENTAL_TWIN_REQUIREMENTS.md section 2.3): the frozen line-value
      DECAYS toward the training window's own empirical mean, half-life equal
      to the window width, unchanged from the 2026-09-16 decay fix. Validated
      by the second validation run: e.g. node_3's `cpu` moved smoothly off its
      old frozen clamp-boundary value (15.00) and converged to ~35.22, instead
      of sitting at the clamp forever.

    `node_id` also selects which feature-range clamp applies (node_0's
    deterministic ramp/oscillation has a different true range than the other
    nodes' random draws -- see clamps_for()).
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
    if _is_structured(node_id):
        decay = 0.0  # keep the original clamp-freeze -- see docstring above
    else:
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
