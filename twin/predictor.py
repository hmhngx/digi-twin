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

# (low, high) clamp per feature; None = no bound on that side. Two node
# categories, and two different KINDS of bound -- not just two ranges:
#
#   node_0 -- REAL host telemetry since 2026-09-22 (twin/publisher.py's
#   measure_host()). Clamped only to what is physically impossible, never to a
#   range we happen to have seen: a real machine can always do something it
#   hasn't done yet, so an observed min/max is not a bound. cpu and mem are
#   percentages -> [0, 100]. A connection count and a round-trip time can't be
#   negative but have no natural ceiling -> [0, None].
#   The clamps these replace (cpu [10,95], mem [25,45], conns [40,100],
#   latency [5,120]) were the min/max of node_0's old synthetic ramp formula --
#   facts about code this repo wrote, not about any machine. Measured live with
#   real data before this change (2026-09-22): the old mem clamp pinned
#   node_0's predicted memory at 45.00 while the laptop was really at
#   84.5-89.4 %, and the old latency floor pinned it at 5.00 vs a real 8-35 ms.
#
#   node_1..node_9 -- still independent random.uniform/randint draws
#   (publisher.py's node_features()), so their true range IS known exactly and
#   is read straight from that code: cpu [15, 55], mem [20, 70], conns [5, 80],
#   latency [2, 40].
HOST_NODE_CLAMPS: dict[str, tuple[float | None, float | None]] = {
    "cpu": (0.0, 100.0),
    "mem": (0.0, 100.0),
    "conns": (0.0, None),
    "latency": (0.0, None),
}
NOISE_NODE_CLAMPS: dict[str, tuple[float | None, float | None]] = {
    "cpu": (15.0, 55.0),
    "mem": (20.0, 70.0),
    "conns": (5.0, 80.0),
    "latency": (2.0, 40.0),
}


def _is_real_host(node_id: str) -> bool:
    """True for node_0, the one node fed from real host telemetry -- the same
    distinction twin/publisher.py's is_real_node() makes. Selects the clamp set
    only; the post-cap recovery policy in predict() is the same for every node."""
    return node_id.endswith(":node_0")


def clamps_for(node_id: str) -> dict[str, tuple[float | None, float | None]]:
    return HOST_NODE_CLAMPS if _is_real_host(node_id) else NOISE_NODE_CLAMPS


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

    Past that cap, EVERY node's frozen line-value decays toward the training
    window's own empirical mean, with a time constant of one window width
    (1 - e^-1, about 63 % of the way to the mean, one window past the cap).

    - node_1..node_9 (i.i.d. random draws -- EXPERIMENTAL_TWIN_REQUIREMENTS.md
      section 2.3): unchanged since the 2026-09-16 decay fix. Validated there:
      e.g. node_3's `cpu` moved smoothly off its old frozen clamp-boundary
      value (15.00) and converged to ~35.22 instead of sitting at the clamp.

    - node_0 (REAL host telemetry since 2026-09-22): now on the same decay.
      From 2026-09-16 to 2026-09-22 it was exempt -- FROZEN at the capped
      value -- because its signal was then a closed-form ramp: its future was
      analytically knowable, it kept climbing, and decaying toward an
      early-window mean was measured to make its error grow without bound
      (-79.70 on `cpu` by 400 s, notes/PREDICTOR_VALIDATION.md). That
      justification belonged to the formula, and the formula no longer
      exists. Real host telemetry has no closed form, and on this laptop it
      behaves as bursty and mean-reverting: CPU, connection count and latency
      jump when something starts and fall back when it stops, rather than
      following a sustained line. Freezing would lock in whatever slope ~10
      samples happened to show at the instant the node went silent, for as
      long as the silence lasts. Decaying limits how long a possibly-noise
      slope is trusted and falls back to the recent average level -- the
      least-bad long-horizon guess for a mean-reverting quantity. Measured,
      not only argued: in an offline backtest against real node_0 samples
      (notes/PREDICTOR_VALIDATION.md, 2026-09-22 section), decay beat freeze
      on all four features once past the cap and was identical before it.
      The same backtest also found that a plain window mean beats the linear
      extrapolation itself for cpu and latency on this data -- recorded
      there as an open finding, deliberately not acted on here.

    `node_id` still selects which clamp set applies (see clamps_for()): real
    physical bounds for node_0, the generator's exact range for the others.
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
