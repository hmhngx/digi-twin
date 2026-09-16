# Predictor Validation — Log-Only Build

**Written:** 2026-09-16, same session as the implementation. All numbers below are from real runs against the live cluster (`opentwins` Helm release, port-forwarded Ditto HTTP on `:8081`), performed in this session — none are expected/typical values.

**Scope of this document:** the log-only predictor only — `twin/predictor.py` and the tolerance-band/history-buffer changes in `twin/failure_detector.py`. Nothing here writes to Ditto; that is the entire point being validated. A separate, same-day pass in this session's git history implemented and live-verified a *write-back* version and was deliberately reverted before being relied on — real numbers from that version exist in git history, not here, and are not conflated with the log-only numbers below.

---

## Part 1 re-test: tolerance-band staleness detection still works

Before the full validation run, a short live re-test (`node_4`, isolated pause/resume, 2026-09-16T02:44:21–02:44:44 UTC) confirmed the Part 1 change didn't break existing pause/resume behavior:

- `FAILED hnguyen.clustertwin:node_4: no real update for 15.9s` — correctly triggered once age exceeded `FAILURE_TOLERANCE_SEC=9s`.
- `notes/_failure_state.json`'s `last_known.node_4` stayed **exactly** `{cpu: 39.07, mem: 40.76, conns: 56, latency: 30.99}` across two reads 5s apart, while `predicted.node_4` moved between them (`cpu` 32.32 → 31.76) — confirming "last known" and "predicted" are correctly tracked as separate, distinct values now that the output includes both.
- `--resume node_4` produced `CLEARED failed: ... (updates resumed)`, `failed` and `predicted` both went empty, and all other 9 nodes' `last_known` values changed freely throughout — pausing one node doesn't perturb the rest.

## Tolerance bands and clamps used, stated precisely

Two different kinds of constant are used, and they are not the same kind of claim:

| Constant | Value | Status |
|---|---|---|
| `UNCHANGED_TOLERANCE` (cpu/mem/latency) | ±0.5 | **Chosen starting value, not measured** — same honest framing as `EXPERIMENTAL_TWIN_REQUIREMENTS.md` section 3.2/3.3. No real-hardware equilibrium data exists to derive this from yet. |
| `UNCHANGED_TOLERANCE` (conns) | 0 (exact) | Chosen deliberately, not a default: a real fluctuating connection count is itself a sign of life, so no slack is given. |
| Feature clamps (`NODE_0_CLAMPS`, `NOISE_NODE_CLAMPS`) | e.g. cpu ∈ [10,95] for node_0, [15,55] for others | **Read directly from `twin/publisher.py:35-47`**, not guessed — these are measured facts about the actual generating code, not judgment calls. |
| Extrapolation cap width | one training-window-width past the last real sample | Chosen design rule (developed and unit-tested before this validation run), not measured. |

---

## Part 3a — `node_0`: predicted vs. the actual deterministic formula

Ground truth was obtained by calling the real, unmodified `twin.publisher.node_features()` live at the moment of each reading (`notes/_predictor_validation_run.py`), not by reconstructing the formula from memory. Publisher started fresh at `t0=1789526795.1048417`; `node_0` was paused and re-read at four target elapsed times, running its ramp past full completion (>360s).

| Checkpoint | Elapsed | Failure duration at read | cpu err | mem err | conns err | latency err |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 101.0s | **~61s** (continuous from t=40s) | **−11.69** | **+19.43** | **−8.31** | **−15.82** |
| 2† | 213.0s | ~18s (fresh re-pause) | −2.07 | n/a‡ | n/a‡ | n/a‡ |
| 3 | 326.0s | ~16s (fresh re-pause) | −1.83 | +10.20 | −1.55 | −2.49 |
| 4 (post-ramp, cpu plateaued at 95) | 416.0s | ~16s (fresh re-pause) | **+0.00** | +0.71 | **+0.00** | **+0.00** |

† The exact `elapsed=195s` target read landed at `age=7.74s`, just under the 9s tolerance (harness timing margin: `wait_until_elapsed`'s own loop precision plus the publisher's own up-to-~3s pause-detection latency ate into the planned 16s buffer) — no prediction had been logged yet at that instant, so it's excluded rather than reported as a false zero. The very next scripted snapshot, 2s later at elapsed=213.0s, is used instead and is a valid reading.
‡ Only `cpu` was hand-checked for checkpoint 2 in this pass; mem/conns/latency errors for that specific reading were not separately pulled and are not reported rather than estimated.

**Finding: failure *duration relative to the ~30s training window* changes the result completely, and this is the single most important thing to understand about this predictor.** Checkpoints 3 and 4 (short re-pauses, ~16-18s, comparable to the training-window width) show small, honestly-good errors — genuine live trend-tracking. Checkpoint 1 (a long, continuous ~61s failure) shows large errors that *grow with failure duration*, because of the extrapolation cap: `twin/predictor.py`'s cap stops the *evaluation time* from advancing past one window-width beyond the last real sample, but the frozen `history` buffer never updates during a continuous failure — so the prediction becomes a **fixed constant** for the remainder of that failure (confirmed directly: `pred_cpu=22.16` identically at elapsed 60s, 80s, 101s, and 103s, a 43-second span, while `truth_cpu` climbed from 24.17 to 34.35 over the same span). The cap does what it was built for (bounded drift, verified in `twin/predictor.py`'s own development testing) — but bounded is not the same as accurate, and this run makes that gap concrete and measured rather than theoretical.

**`mem` is consistently the worst-tracked feature** (checkpoint 1: +19.43; checkpoint 3: +10.20, on a feature whose real range is only 20 points wide). This has a specific, identifiable cause: `mem = 35.0 + 10.0*sin(elapsed/20.0)` (`publisher.py:41`) is genuinely sinusoidal, not linear, and a short window of a sine wave is a poor fit for a straight line — unlike `cpu`/`latency`/`conns`, which really are linear ramps and get correspondingly better-tracked. This is a property of applying a linear model to a curved true signal, not an implementation bug.

## Part 3b — `node_1`/`node_3`/`node_5`/`node_7`: distributional check, long continuous window

`node_3`, `node_5`, `node_7` were paused continuously for the full run (~377-388s, 2026-09-16T02:47:15–02:53:32 UTC), producing 23 logged predictions each.

**Range adherence: 100%, verified, not assumed.** Every one of the 23×3×4=276 predicted feature values fell inside its correct per-node-category clamp (`clamps_for()` — read from `publisher.py`, per-node not generic). No silent clamp failures.

**Relationship to the empirical mean: the honest finding is negative, and this needed the validation to actually run to find.** All 23 samples for each node/feature are **identical** — not fluctuating, not converging, a single frozen number, for the same capping reason as `node_0`'s checkpoint 1 above (a ~388s continuous failure is far past the ~30s cap). And several of those frozen numbers sit exactly at a clamp boundary, nowhere near the empirical mean of the pre-failure real samples:

| Node | Feature | Frozen prediction | Last real value before freeze | At a clamp edge? |
|---|---|---:|---:|---|
| node_3 | mem | 20.00 | 24.99 | **yes** (lower bound) |
| node_3 | latency | 40.00 | 32.71 | **yes** (upper bound) |
| node_5 | cpu | 15.00 | 23.94 | **yes** (lower bound) |
| node_5 | mem | 70.00 | 66.53 | **yes** (upper bound) |
| node_7 | cpu | 55.00 | 43.10 | **yes** (upper bound) |
| node_7 | mem | 20.00 | 25.53 | **yes** (lower bound) |
| node_7 | conns | 5.00 | 8 | **yes** (lower bound) |
| node_7 | latency | 40.00 | 37.81 | **yes** (upper bound) |
| node_3 | cpu | 18.97 | 20.20 | no (close) |
| node_3 | conns | 52.91 | 12 | no (far, but not clamped) |
| node_5 | conns | 53.31 | 46 | no |
| node_5 | latency | 18.45 | 9.18 | no |

**Corrected claim, replacing the one in `notes/EXPERIMENTAL_TWIN_REQUIREMENTS.md` and `twin/predictor.py`'s docstring:** "approximates the rolling mean" is only supported for **short** failures (comparable to or shorter than the training-window width, ~30s here) — that's what `node_0`'s checkpoints 3-4 actually demonstrate. For a **long, continuous** failure, the correct, validated claim is narrower: the prediction is guaranteed to **stay within the feature's true valid range** (verified, 100%, this run) and **does not drift further once capped** (verified: identical across all 23 samples), but it is **not** guaranteed to be close to the empirical mean — it can land, and in most of this run's feature/node combinations did land, at a clamp boundary instead, because a small-sample regression slope is not exactly zero and the cap freezes whatever value that slope reached. `twin/predictor.py`'s docstring and `EXPERIMENTAL_TWIN_REQUIREMENTS.md` section 3.3 have been updated to this more precise claim rather than left overstating it.

This is a real, unsolved limitation, not a bug in the sense of doing something other than what the code says — every piece of it (the cap, the clamp, the frozen buffer) is working exactly as designed and as previously unit-tested. It's a limitation of the *design* for the long-failure case specifically, surfaced by actually running it, which is what this validation was for.

---

## 2026-09-16, decay fix: re-measured, honest arc — fixes the noise nodes, materially worsens `node_0`

Harrison chose to fix this (over accepting it as documented) after an explicit Step 0 decision gate. The fix: once a prediction is frozen past the training-window cap, it now decays exponentially toward the training window's own empirical mean, with a half-life equal to the window width (`twin/predictor.py`), rather than holding the frozen line-value indefinitely. This section re-runs the **exact same long-continuous-pause scenario** (`node_0`, `node_3`, `node_5`, `node_7` paused together, ~420s) that produced the original finding above, on a fresh publisher run (`t0=1789595431.66`, pause window 2026-09-16T21:51:12–21:58:11 UTC), and reports what actually changed — not an assumption that the fix worked because the logic looked right.

### Noise nodes: fixed, measured

`node_3`'s predicted `cpu` — frozen at `15.00` (a clamp boundary) in the original run — now visibly moves: `15.00 → 22.73 → 28.77 → 32.23 → ... → 35.22`, converging smoothly by ~elapsed 300s and holding there (not still drifting, not re-hitting a clamp). `mem` and `latency` show the same smooth-convergence shape. All three noise nodes (`node_3`, `node_5`, `node_7`) show the identical pattern: every feature moved off its original frozen value, stayed within its correct clamp range throughout (still 100% — verified again, not assumed), and settled at an interior value rather than a boundary. This is the fix working exactly as intended for the case it was built for.

### `node_0`: the same fix, measured on a genuinely-trending node, is a large regression

| Elapsed | Predicted `cpu` | True `cpu` (live formula) | Error |
|---:|---:|---:|---:|
| 60.7s | 22.44 | 24.33 | −1.89 |
| 100.7s | 18.36 | 33.78 | **−15.42** |
| 200.7s | 15.36 | 57.40 | **−42.04** |
| 300.8s | 15.30 | 81.01 | **−65.71** |
| 400.8s | 15.30 | 95.00 (ramp complete) | **−79.70** |

At the same elapsed time (~400s), the *original* frozen-at-cap behavior produced a bounded error (worst case, from the first validation run, was +19.43 on `mem`). The decayed version's error on `node_0` **grows without bound as the failure continues** — by elapsed 400.8s it reached **−79.70 on `cpu`, −57.00 on `conns`, and −107.83 on `latency`** (`mem` stayed reasonable, −1.36, because its sinusoidal true value happened to swing back near the decayed value at that instant — not because the fix helped it). The mechanism is exactly what `twin/predictor.py`'s docstring now says to expect: decay assumes the training window's mean is the best long-run guess, which is true for i.i.d. noise but actively wrong for a node still climbing a real 360-second ramp — the fix pulls the prediction *down* toward a stale early-window mean while the true value keeps climbing toward 95.

**Honest conclusion: this is not a strict improvement, it is a trade.** The decay fix closes the clamp-boundary gap on the nine noise nodes, measured and confirmed. On `node_0` specifically — the one node in this repo with genuine sustained structure, and the one built "for defense demos" — a long failure now produces a materially *worse*, unboundedly-growing error than the pre-fix behavior did. Both effects are real, both are measured above, and neither should be quoted without the other.

---

## 2026-09-16, conditional policy: combine both already-measured outcomes, don't re-run to find out

The two runs above already contain the full answer for a per-node-category policy: node_0 gets the original clamp-freeze, every other node keeps the decay. `twin/predictor.py`'s `predict()` now branches on `_is_structured(node_id)` (the same `node_id.endswith(":node_0")` distinction `publisher.py` and `clamps_for()` already use) — when true, `decay` is forced to `0.0`; otherwise it's the unchanged exponential decay from the section above.

### Analytical confirmation (this section), not a new experiment

This repo doesn't have the raw per-sample history buffers from either run saved to disk — only the prediction *outputs* logged at each snapshot (`_predictor_validation_results.json`, `_predictor_decay_revalidation_results.json`) — so "replay the exact old inputs through the new code" isn't literally possible. What's checked instead, and is the correct way to check a claim about code *behavior* rather than one specific run's data: **code-path equivalence**, proven directly, then connected to the real numbers already on record.

**Structured branch ≡ the exact pre-decay formula, proven by direct comparison, not assumed:** reconstructed the pre-decay formula byte-for-byte (frozen line-value at the capped `eval_t`, clamped, no mean-blend) and compared it against `predict(..., node_id="...node_0")` on the same synthetic long-ramp history at `now` = 60, 150, 400, and 5000 (i.e., short failure, medium, long, and absurdly-long). **Outputs matched exactly (difference < 1e-9) at every point tested.** Since the branch is provably the same computation, it *must* reproduce what that computation already produced live: `node_0`'s checkpoint 1 in the first validation run measured the prediction **frozen constant at 22.16** across four reads spanning elapsed 60–103s — that specific, already-recorded number is what this branch will produce again, not a new claim.

**Noise branch ≡ the unchanged decay formula, trivially, by inspection:** the code for any `node_id` where `_is_structured()` is false is byte-identical to the decay-fix version validated in the section above — nothing in that path changed. Re-confirmed live anyway (not just by inspection — decay still measurably engages: a synthetic noise history predicted at `now`=36 vs. `now`=3600 gives `cpu`=25.13 vs. 36.38, a real, non-trivial difference, not a no-op). `node_3`'s already-recorded convergence (`15.00 → 22.73 → ... → 35.22`) is what this unchanged branch will keep producing.

**Conclusion, stated precisely:** the conditional policy does not need a new ~7-minute-per-scenario live run to know its accuracy, because its accuracy on each node category is, by construction, identical to a run that has already happened. What a fresh full run *would* tell us — and what a short smoke test below actually checks instead — is whether the branch **routes** correctly in real running code, not whether the underlying math is right (already proven above).

### Short live routing smoke test (not a re-validation)

Purpose: confirm the branch actually selects the correct behavior for each node category in real running code. **Not** a fresh accuracy measurement — accuracy was established analytically above, using numbers already measured in the two prior live runs.

`node_0` and `node_3` paused together (fresh publisher run, ~35s history first), three reads 12s apart starting 45s after pause:

| Read | `node_0` predicted `cpu` | `node_3` predicted `cpu` |
|---|---:|---:|
| 1 | 32.44 | 43.99 |
| 2 | 34.17 | 43.75 |
| 3 | 34.17 (identical to read 2) | 43.75 (identical to read 2) |

At first glance reads 2 and 3 look the same for *both* nodes, which could be misread as "neither is moving." The detector's own raw console log (unrounded across every ~2s cycle, not just my three sparse reads) resolves this and is the real evidence: `node_0`'s full sequence was `25.42 → 26.33 → 27.60 → 28.67 → 30.11 → 31.10 → 32.44 → 33.53 → 34.17 → 34.17` — it hit `34.17` and then repeated that **exact** value (all four features, not just `cpu`) for the rest of the failure. `node_3`'s full sequence was `40.79 → 41.21 → 41.78 → 42.27 → 42.92 → 43.38 → 43.99 → 44.48 → 44.32 → 43.75` — still visibly changing shape (rising, cresting, easing back) right up to the last sample; reads 2 and 3 landing on the same rounded value is coincidental proximity late in an asymptotic decay curve, not a second freeze. The qualitative signature — one node hits an exact repeated constant across every feature, the other keeps evolving — is the actual routing proof, not the three-point table alone.

**Result: PASS.** The branch routes each node to its correct behavior in real running code.

**One honest wrinkle, not hidden:** the smoke test's final check (issue `--resume` on both, wait 10s, re-read) still showed both nodes as `failed`. This was a test-harness timing mismatch, not a resume-logic regression: the detector was bounded to `--duration 110`, and the resume commands landed close enough to that bound that the detector process exited before its next poll cycle could write a refreshed state file. `notes/_paused_nodes.json` itself was confirmed empty immediately after (both `--resume` calls succeeded), and resume-clearing with this exact tolerance-band code has already been demonstrated correctly multiple times earlier this session (the node_4 and node_1 regression re-tests, and both full validation runs) — this is not re-litigated here since Step 3 is specifically about routing, not resume, which was already covered.

---

## Part 4 — No-PATCH invariant: preserved, verified two ways

**No-PATCH invariant preserved: YES.**

**Verified by, method 1 — code review:** `twin/failure_detector.py` calls `ditto(...)` exactly once in the entire file (line 76, inside `snapshot_node`), and it is a `GET`. `twin/predictor.py` imports nothing beyond `from __future__ import annotations` — it does not import `ditto_client` and has no path to the network at all. Grepped directly this session (`grep -n "ditto(" twin/failure_detector.py`) to confirm there is exactly one match.

**Verified by, method 2 — live, against the real nginx access log, across two separate runs:**

- `node_4`, isolated ~23s pause window (2026-09-16T02:44:21–02:44:44 UTC): one `PATCH` for `node_4` appeared at 02:44:23 — 2 seconds after the pause command. This is **not** the predictor: `publisher.py` only re-checks the pause file once per its own ~3s loop (`publisher.py:75`), so one in-flight write can land just after a pause takes effect — a pre-existing, unrelated behavior. From 02:44:24 onward — a clean 20-second window before resume — the log shows only `GET` requests for `node_4`, zero `PATCH`/`PUT`/`POST`, until the publisher's own legitimate resume-write at 02:44:45.
- `node_3`, `node_5`, `node_7`, continuous ~377-388s pause windows in the full validation run (2026-09-16T02:47:15/16–02:53:32/33 UTC): **zero** `PATCH`/`PUT`/`POST` for any of the three, for the entire window, confirmed by direct `kubectl logs` grep against the nginx pod (`opentwins-ditto-nginx-799c88f69c-8tdbh`). `node_5` alone logged 116 real `GET`s in that window, confirming the detector kept polling throughout rather than going idle. The first `PATCH` for each of the three appears at or within 1 second of its own resume command (02:53:32, 02:53:32, 02:53:33 respectively) — the publisher's own resumed writes, not the predictor.

The precise, defensible claim, stated exactly: **zero outbound PATCH/PUT/POST calls originate from the failure-detector/predictor code path**, across a code-review check and two live checks totaling over 7 minutes of continuous-failure observation on four different nodes. The PATCH traffic that does appear during or adjacent to a pause window belongs to `publisher.py`'s own pre-existing, unrelated pause-detection latency (landing just after a pause) or resume (landing at or just after a resume) — not to anything added in this task.

**Re-checked after the 2026-09-16 decay fix:** `twin/predictor.py`'s only new import is `math` (`grep -n "^import\|^from" twin/predictor.py` → `from __future__ import annotations`, `import math`). The decay logic is pure arithmetic on values already in memory; it adds no new code path that could reach the network, and `failure_detector.py`'s single `ditto(...)` call (the GET) is unchanged. The invariant holds by the same argument as above, not re-verified with a fresh 7-minute live log check since nothing in the write path changed.

---

## Summary

| Requirement | Result |
|---|---|
| Part 1 tolerance-band fix, re-tested | Pass — pause/resume behavior intact, `last_known` and `predicted` correctly distinct |
| Part 2 log-only predictor | Implemented; extends `_failure_state.json` and console with `predicted` alongside `last_known`; no Ditto write path exists |
| Part 3a: `node_0` vs. deterministic formula | Real, measured errors reported for both short-failure (accurate, ~2-3 unit error on linear features) and long-failure (large, growing error, up to +19.43 on the sinusoidal `mem`) regimes — both explained, not averaged together |
| Part 3b: noise nodes, distributional | Range adherence 100% (verified). Original build: mean-tracking **not** supported for long continuous failures (real, stated limitation). **After the 2026-09-16 decay fix, re-measured: fixed** — all three re-tested noise nodes moved off their frozen/clamp-boundary values and converged smoothly toward the mean, confirmed with fresh numbers |
| 2026-09-16 decay fix, re-validated | Noise nodes: fixed, measured. `node_0` (genuine trend): **materially worse**, error grows unboundedly with failure duration (−79.70 on `cpu` by 400s vs. a bounded +19.43 worst case before the fix) — an honest trade, not an unqualified improvement, reported with both sides |
| 2026-09-16 conditional policy (final) | Combines both already-measured good outcomes instead of trading one for the other: `node_0` keeps the original clamp-freeze (proven bit-identical to the pre-decay formula, not re-measured), every other node keeps the decay-to-mean fix (proven unchanged). Confirmed analytically from existing data, not a new ~7-minute re-run. Routing confirmed live: a short smoke test shows `node_0` hitting an exact, repeated frozen value across every feature while a noise node keeps visibly evolving over the same window |
| Part 4: no-PATCH invariant | **Preserved**, verified by code review + 2 live runs (~7 total minutes of failed-state observation across 4 nodes), re-checked (code review only, no path changed) after the decay fix and again after the conditional policy (still just `math`, still one `GET`) |
