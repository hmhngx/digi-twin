# Experimental Digital Twin — Requirements: Data Sources and Parameters

**Written:** 2026-09-15, after the meeting with Prof. Siddiqui that day.
**Status:** Software-only. No physical hardware exists, none has been purchased, and the purchase decision itself is deferred until the work in this document is demonstrated.

## Scope pivot — read this before anything else below

Everything written in `notes/FALL2026_ACTION_PLAN.md`, `notes/FALL2026_RESEARCH_PROPOSAL_OPTIONS.md`, and `notes/HARDWARE_SELECTION_AUDIT.md` (2026-09-13 through 2026-09-15) assumed a Raspberry Pi 5 would shortly exist and that the semester's work would center on a cloud-computed fan controller competing against the Pi's own firmware. **That assumption did not survive today's meeting.** Prof. Siddiqui's actual direction, relayed 2026-09-15: learn to build a working digital twin with what already exists before any hardware purchase is even decided, specifically by (1) grounding the work in a real paper about building a digital twin for experimental purposes — data sources, parameters — and (2) working out how this twin handles prediction.

Two concrete consequences, stated plainly so they aren't silently carried forward:

1. **There is no physical experiment.** Every place the original task framing said "the physical experiment," this document says "the experimental digital twin" and means the software system already running against the OpenTwins/Ditto stack in this repo. Physical ground truth, physical disturbance inputs, and physical command/actuation data do not apply — not because they were forgotten, but because there is no physical device to source them from.
2. **The grounding paper changed.** The ACM sampled-data-delay paper (Huang, Jin & Wang, CSAE '18) was brought in specifically to justify a networked *controller* fighting real network delay — that problem doesn't exist without a physical actuator. The paper that actually answers both of the professor's directives is the one this whole repo is already an implementation of: **Robles, Martín & Díaz, "OpenTwins: An open-source framework for the development of next-gen compositional digital twins," *Computers in Industry* 152 (2023) 104007** — read in full, primary source, this session (`C:\Users\minhh\Downloads\openTwins.pdf`, 10 pages, text-extracted directly since no PDF page-renderer was available in this environment). Its §5 ("Evaluation") is a worked example of exactly "data sources and parameters for an experimental digital twin," and its §3.3 ("Prediction model integration with Kafka-ML") is the exact mechanism `twin/failure_detector.py`'s hold-last-value logic already stands in for. Every citation to "the paper" below means this one.

---

## 1. What this repo's twin model already decided (inventory, not proposal)

This is already built and already verified by prior sessions. Restating it here so the rest of this document has a fixed foundation to build on.

| Decision | Value | Source |
|---|---|---|
| Namespace | `hnguyen.clustertwin` | [`twin/config.py:22`](../twin/config.py#L22) |
| Composition | 1 cluster → 2 racks → 10 nodes (13 Things total) | [`twin/config.py:32-38`](../twin/config.py#L32-L38), [`README.md`](../README.md) |
| Attributes vs. features | Attributes = static (`hardware_model`, `rack_position`, `install_date`); Features = dynamic (`cpu_utilization`, `memory_utilization`, `active_connections`, `latency_ms`) | [`notes/twin_design_spec.md:38-47`](twin_design_spec.md#L38-L47) — this is a direct implementation of the paper's own Ditto Thing model: *"attributes correspond to the static part of the entity, whereas the features correspond to the dynamic part"* (paper §3.2) |
| Publish interval | `PUBLISH_INTERVAL_SEC = 3` (seconds) | [`twin/config.py:23`](../twin/config.py#L23) |
| Aggregate interval | `AGGREGATE_INTERVAL_SEC = 7` | [`twin/config.py:24`](../twin/config.py#L24) |
| Failure tolerance | `FAILURE_TOLERANCE_SEC = 9` | [`twin/config.py:25`](../twin/config.py#L25) |
| Thing schema | `NodeType`(card. 5) → `RackType`(card. 2) → `ClusterType`, enforced via the Ditto-Extended-API | [`notes/twin_design_spec.md:55-78`](twin_design_spec.md#L55-L78) |
| Inbound path | Direct HTTP PATCH, publisher → Ditto core API (`:8081`) | [`twin/publisher.py:57-66`](../twin/publisher.py#L57-L66) — a deliberate simplification of the paper's Hono ingestion (paper §3.1), disclosed as such throughout this repo's own docs |
| Outbound path | Ditto → Mosquitto (MQTT) → Telegraf → InfluxDB → Grafana | [`README.md`](../README.md) "Data path" section — this *is* the paper's real platform pattern (Kafka/broker → Telegraf-equivalent → InfluxDB → Grafana), not a simplification |
| Prediction | **Stale as of this row's original writing — corrected 2026-09-16.** As of the Part 2 work below, `failure_detector.py` no longer just holds the last value: on a tolerance-band-detected timeout it hands the node's frozen real-sample history to `twin/predictor.py`, which fits a per-feature least-squares line and extrapolates it (capped at one training-window-width past the last real sample). Past that cap, recovery is conditional on node category — `node_0` (structured/deterministic) stays frozen at the capped value; every noise node (`node_1`–`node_9`) decays toward the training window's empirical mean. The result is logged to `notes/_failure_state.json` and the console alongside the real last-known value; **nothing is written back to Ditto** (verified — see Part 4 of `notes/PREDICTOR_VALIDATION.md`). | [`twin/failure_detector.py:91-157`](../twin/failure_detector.py#L91-L157), [`twin/predictor.py`](../twin/predictor.py) (see `predict()` and `_is_structured()`), [`notes/PREDICTOR_VALIDATION.md`](PREDICTOR_VALIDATION.md) |

The one item with no existing repo analog at all is prediction — the paper's §3.3 has a whole subsystem for it (Kafka-ML + two glue services), and this repo has zero code addressing it. That gap is the subject of Part 2, and it's why the professor's second directive lands exactly there.

---

## 2. Real data sources an experimental digital twin needs

The paper's own §5.1 ("Experimental setup") is the template here: it names exact hardware, exact software versions, and a specific test design *before* reporting a single number. The four categories below are the same four the original task brief asked for, each one re-derived for a system with no physical device rather than silently answered as if one existed.

### 2.1 Telemetry from "the device itself"

There is no device. The closest analog is `twin/publisher.py`'s `node_features()` ([`:33-54`](../twin/publisher.py#L33-L54)), which is the *sole* source of every value this twin ever holds. This matters more than it sounds like it should, because of what it implies for the next subsection: **the twin's telemetry source and the twin's ground truth are, today, the same function.** There is no independent channel a value could diverge from.

Concretely, two different data-generating processes exist side by side, and they are not interchangeable for experimental purposes:

- **`node_0`** ([`publisher.py:35-42`](../twin/publisher.py#L35-L42)): fully deterministic given elapsed time — `cpu = 10.0 + 85.0 * min(1.0, elapsed/360.0)`, `latency` the same shape, `mem` a bounded sine wave, `conns` the same ramp cast to int. This is a real, reproducible signal with structure.
- **`node_1`…`node_9`** ([`publisher.py:44-47`](../twin/publisher.py#L44-L47)): `random.uniform`/`random.randint` calls, freshly drawn every cycle, with **no dependence on the previous value or on elapsed time at all.** Each draw is statistically independent of the last.

This distinction is load-bearing for Part 2 and is explained there rather than repeated twice.

### 2.2 Historical data for baseline/comparison

Two real sources exist, with different guarantees:

- **InfluxDB**, via the real Telegraf pipeline — but bounded by `flush_interval = "10s"` (verified live, [`notes/twin_design_spec.md:117`](twin_design_spec.md#L117)), meaning nothing written to Influx is visible sooner than ~9–11s after the originating PATCH regardless of how fast the publisher runs. Any baseline built by querying Influx inherits this floor.
- **Dated `notes/` files** — `scaling_comparison.md`, `fault_tolerance_results.md`, `VERIFICATION_REPORT.md`, etc. These are point-in-time snapshots, explicitly not live state (the root README says so directly), useful as a *comparison baseline for this project's own history* but not as ground truth for any single live run.

Neither of these is "historical sensor data" in the paper's sense (§4: the CEPSA refinery provided real historical data to train the freezing-point model). This repo has no equivalent training corpus, which is a real constraint on what Part 2 can be, addressed there.

### 2.3 Independent ground truth — the genuinely hard one

For a physical experiment, "ground truth" means an independent sensor you trust more than the one you're testing. For this software-only twin, that concept has to be rebuilt from scratch, because as noted in 2.1, the telemetry source *is* the ground truth generator — Ditto's stored feature value is defined to equal whatever `node_features()` last returned. There is no way for the twin to currently be "wrong."

That symmetry breaks the moment Part 2 introduces a predictor: a predictor's job is to *guess* a value without directly observing the generator, which finally creates a real gap between "what the twin believes" and "what's actually true" worth measuring. The methodology this repo already has for creating that gap on demand is `notes/_paused_nodes.json` — `failure_detector.py --pause node_X` ([`:125-129`](../twin/failure_detector.py#L125-L129)) already stops `publisher.py` from writing new values for a chosen node ([`publisher.py:76-79`](../twin/publisher.py#L76-L79)), which is exactly "simulate a sensor going silent" without touching any other node.

The catch, and the reason this needs to be said explicitly rather than assumed: **ground truth is only recoverable for `node_0`.** Because its formula is a deterministic function of elapsed time, "what would this node be reporting right now if it hadn't been paused" is a computable, comparable number. For `node_1`–`node_9`, there is no such number — each `random.uniform` draw the generator *would have* produced is independent of every draw before it, so "the value it would have reported" isn't a hidden fact waiting to be predicted, it's a fresh unknowable random number by construction. Any fidelity claim in this document or in Part 2 that doesn't separate these two cases is not a claim this repo can actually back up.

### 2.4 Controlled disturbance inputs

Two already exist and need no new code:

- **`node_0`'s own ramp** is itself a controlled, repeatable disturbance (10%→95% CPU over ~360s) — already described in this repo's own comments as built "for defense demos" ([`publisher.py:36`](../twin/publisher.py#L36)).
- **Pause/resume** (`_paused_nodes.json`) is a controlled disturbance to a *single node's data stream* — the direct software analog of "a sensor loses power" (paper §3.3's own framing).

`scripts/fault_tolerance_test.py` is a disturbance to the *pipeline* (kills a Ditto pod), not to a node's data — useful for Test-3-style fault tolerance work, but a different kind of experiment than Part 2's prediction question. Worth keeping distinct rather than conflating "the pipeline recovered" with "the prediction was accurate."

### 2.5 Command/confirmation data (bidirectional)

Not applicable. Confirmed multiple times independently in this repo's own audit trail (`HARDWARE_SELECTION_AUDIT.md` row 11, `FALL2026_RESEARCH_PROPOSAL_OPTIONS.md` cross-cutting point #1) that no inbound command path exists in any form, and today's meeting didn't add one to scope. **Resolved, 2026-09-16:** Part 2's predictor turned out to be log-only (see the final hand-off decision below) — it never writes anything back to Ditto at all, so the hypothetical this paragraph originally raised (would a write-back prediction count as a "command"?) is moot for the current build. Had write-back been kept, it would have been a twin *reporting its own internal state* honestly, not a command received from outside, and still would not have created a bidirectional control loop — but that's a note for if write-back is revisited later under its own explicit go-ahead, not a description of what runs today.

---

## 3. Parameters that must be explicitly decided

### 3.1 Sampling interval — tied to actually-measured delay, not a round number

`PUBLISH_INTERVAL_SEC = 3` is already set ([`config.py:23`](../twin/config.py#L23)). Whether 3s is *justified* depends on comparing it against real measured delay, which does exist — `notes/scaling_benchmark.py` measures exactly this, and I re-verified the raw numbers directly rather than trusting a prior summary:

Currently on disk in `notes/scaling_results.json` (verified by reading it directly this session — this is a **third**, previously-undocumented run, distinct from both dated runs in `scaling_comparison.md`):

| Test | n | Avg Ditto RTT | Note |
|---|---:|---:|---|
| sensor_count | 1 | 137.7 ms | |
| sensor_count | 10 | 1,162.5 ms | |
| client_count | 1 | 771.9 ms | |
| client_count | 20 | 2,744.0 ms | **only 2 of 20 samples recorded** — see caveat below |

Two prior dated runs (`scaling_comparison.md`) show a wider spread still: sensor-count n=1 ranges 137.7–615.5 ms across all three runs; client-count n=20 ranges 1,761–5,254 ms, with the 2026-07-28 run only completing 3/20 requests before HTTP 503s. `scaling_comparison.md` already states the right conclusion: **this variance is real and run-to-run, not a fixed ceiling** — don't average it into a single number and don't cite the best run.

**What this actually justifies:** `publisher.py` sends one PATCH per node every 3s, sequentially, with no concurrent clients hammering the same Thing (that access pattern only exists in the artificial `client_count` benchmark, which deliberately stress-tests a failure mode `publisher.py` never triggers in normal operation). The relevant comparison is therefore the `sensor_count` series, not `client_count` — and at up to 10 nodes, RTT stays under ~1.2s across every run on record, comfortably inside the 3s budget. **3s is justified for the current single-publisher access pattern**, not justified in general — if the access pattern ever changes (e.g., multiple publishers, or a real device competing with concurrent benchmark traffic), this would need to be re-measured against the `client_count` series instead, where RTT already approaches or exceeds a 3s budget past ~10–15 concurrent writers.

### 3.2 Failure/anomaly threshold — the tolerance-band fix, and why it hasn't misfired yet

`failure_detector.py` flags `FAILED` when a node's feature signature stops changing for longer than `FAILURE_TOLERANCE_SEC` ([`:79-95`](../twin/failure_detector.py#L79-L95)), using **exact equality** on the JSON-serialized `[cpu, mem, conns, latency]` tuple ([`:79-82`](../twin/failure_detector.py#L79-L82)).

This has not caused a false positive in this repo's history for a specific, checkable reason: `random.uniform`/`random.randint` draws colliding exactly, cycle over cycle, across four independent values, is a probability-zero event in practice. The bug is real but currently *dormant* — it only manifests once a data source can plausibly repeat an exact value, which describes a real sensor at physical equilibrium (the original concern this task was framed around) but does not describe anything this repo runs today.

**Fixed as part of Part 2, 2026-09-16** — this paragraph originally described the fix as still-needed; it has since landed, so the reasoning is restated here in the past tense rather than left to silently go stale. The reason was independent of hardware: once a predictor produces the "held" value instead of the raw last-seen snapshot, exact-equality staleness detection stops being the right question to ask at all — the detector needs a tolerance-band comparison to decide a node has gone quiet in the first place, *before* deciding whether to hand off to the predictor. The fix and the predictor were the same piece of work, not two separate ones. Current implementation: `twin/failure_detector.py`'s `_within_tolerance()`, gated by the `UNCHANGED_TOLERANCE` constants (see `notes/PREDICTOR_VALIDATION.md` for the exact values and the honest "chosen, not measured" caveat on them).

**A second, more fundamental bug in this same mechanism, found and fixed 2026-09-17:** the 2026-09-16 tolerance-band fix above correctly changed *what counts as "unchanged"* but left a separate problem untouched — the code used "the value hasn't changed" as a stand-in for "no update arrived," when those are genuinely different questions. `last_seen` (the timestamp `FAILURE_TOLERANCE_SEC` is measured against) only advanced when a value looked different from last time, so a node whose real value is genuinely stable for longer than 9 seconds would never be considered "seen" again and would be marked `FAILED` forever, despite being polled successfully the whole time. Found by code review while planning `notes/HARDWARE_MEASUREMENT_PLAN.md`'s real-sensor-jitter test — before any real hardware existed to observe it on — then confirmed live two ways: an immediate, repo's-own-synthetic-setup manifestation (all 10 nodes spuriously `FAILED` within the first ~32 seconds of a fresh run, from ordinary poll/publish cadence mismatch) and the originally-targeted one (`node_0`'s real plateau spuriously `FAILED` twice, exactly where a sine-wave analysis of `mem`'s formula predicted it would). Fixed by decoupling liveness (advances on every successful poll, unconditionally) from change-classification (still tolerance-gated, still governs what feeds `last_real`/the predictor's history buffer). Full real run details, timestamps, and the post-fix re-validation are in `notes/PREDICTOR_VALIDATION.md`.

### 3.3 Fidelity tolerance — what counts as "close enough"

Given section 2.3's finding, this has to be defined per node, not once globally:

- **For `node_0`:** compare the predictor's output against the deterministic formula's actual value at the same elapsed time. A concrete starting tolerance: **±5% of each feature's defined range** (e.g., ±4.25 percentage points for `cpu_utilization`, since its ramp spans 85 points) — chosen as a round, defensible starting number, not a value derived from any measurement, and stated as such rather than dressed up as verified. **Final policy, 2026-09-16 (conditional):** this tolerance holds for both short *and* long failures, because `node_0` — any node whose signal is a known deterministic formula rather than i.i.d. noise, per `twin/predictor.py`'s `_is_structured()` — keeps the original clamp-freeze behavior, not the decay. (A same-day intermediate pass tried decaying `node_0` too, to close the noise-node gap below; that measurably made things worse, −79.70 on `cpu` by 400s into a failure, and was replaced by the conditional branch rather than kept — see `notes/PREDICTOR_VALIDATION.md` for the full arc.)
- **For `node_1`–`node_9`:** there is no per-sample "correct" value to compare against (section 2.3). Range adherence is verified 100% across every run performed (original: 276 predictions; decay re-validation: 72 more). **Final policy, 2026-09-16 (conditional):** every non-structured node decays the frozen prediction toward the training window's empirical mean during a long continuous failure, closing the gap the original build had (predictions used to freeze at a fixed value, sometimes exactly at a clamp boundary — measured: 8 of 12 node/feature combinations did). Confirmed with real numbers in `notes/PREDICTOR_VALIDATION.md`, and this branch is untouched by the `node_0` fix above — the two node categories now each get the specific behavior already proven correct for them, not a single compromise setting for both.

---

## Hand-off to Part 2 — proposed design, not yet implemented

Step 0 narrowed the three original prediction options to one: **(a) reactive PID and (b) full MPC both require an actuator to control, and none exists or is being built** (hardware purchase is deferred, and no simulated-actuator work was put in scope by today's meeting either — Harrison's relay of the meeting was about the twin's own data/prediction handling, not a control loop). **(c) a lightweight statistical predictor replacing the hold-last-value fallback** is the only one of the three that fits both what was actually said and what section 2 above establishes is buildable without hardware. This isn't a guess filling a gap Step 0 left open — it's what's left once (a) and (b) are excluded by confirmed facts, and it's exactly what the paper's own §3.3 and this repo's own `twin_design_spec.md:172-174` ("replace hold-last-value with a small online predictor") already point at.

**Proposed method:** per-node, per-feature short-window linear regression (least-squares) over a rolling buffer of `failure_detector.py`'s own recent polls (it already polls every ~2s; buffering that history is nearly free, no new data source needed), extrapolated forward to the current time and clamped to each feature's known valid range. Rejected alternatives, and why:

- **Exponential moving average:** simpler, but structurally lags a ramp — it would always undershoot `node_0`'s rising CPU, which actively misrepresents "predict the next state" as "smooth the last state."
- **A trained ML model (true Kafka-ML analog):** would need a labeled historical corpus this repo doesn't have (section 2.2), and building the training infrastructure is disproportionate to "lightweight."

The useful property of linear regression here: on `node_0` it genuinely extrapolates the trend; on the nine noise nodes, a small window's fitted slope is small but not exactly zero (least-squares on i.i.d. data has no reason to land on precisely 0), so it approximates the rolling mean — a reasonable prediction for i.i.d. data, not a provably optimal one. One algorithm, two honestly-different behaviors, matching section 2.3's finding exactly.

**Decision, made 2026-09-16: log-only.** Compute and record the predicted value (`_failure_state.json` and console output, alongside the actual last-known value) and change nothing about what reaches Ditto or Grafana. This preserves the property this repo's own audit trail has independently verified and relied on multiple times (`twin/README.md`: *"Does **not** PATCH Ditto"*).

*(Correction: an earlier pass of this session implemented and live-verified the write-back alternative — PATCHing the prediction to the node's Thing, matching the paper's own `Kafka-ML-to-Eclipse-Ditto` service — before this was walked back to log-only. Write-back remains a real, separate option for a later task with its own explicit go-ahead, not something to silently re-introduce. See `notes/PREDICTOR_VALIDATION.md` for the log-only implementation's validation and the explicit no-PATCH-invariant check.)*

---

## Part 3 reconsideration — the deferred physical-entity requirements, revisited 2026-09-16

This document's own header deferred the hardware purchase decision until Parts 1-2 were demonstrated. Today's full audit pass (live pipeline re-confirmed post-restart, no-PATCH invariant re-checked two ways, this document's own stale inventory row fixed above) is that checkpoint, so the requirements list from `notes/HARDWARE_SELECTION_AUDIT.md` / `notes/FALL2026_ACTION_PLAN.md` / `notes/FALL2026_RESEARCH_PROPOSAL_OPTIONS.md` is reconsidered here in light of what the software-only phase actually taught, rather than assuming those three files' 2026-09-13/14/15 numbers and framing still hold untouched.

### Live pricing, re-verified today (not carried forward from the 2026-09-15 check)

Fetched directly from the live reseller pages this session, not a search summary:

| Item | 2026-09-15 figure | **2026-09-16/17 live figure** | Change |
|---|---:|---:|---|
| Raspberry Pi 5, 4GB board | $110.00 (PiShop + CanaKit) | **$110.00** — CanaKit "In Stock"; **PiShop now shows this exact SKU as "Out of stock"** (Notify Me only) | Price unchanged. **New finding: availability, not just price, is volatile** — the two resellers previously cited interchangeably no longer agree on stock. Order from CanaKit if buying now, or re-check PiShop before assuming it. |
| Official Active Cooler | $10.95–$13.50 | **$10.95** (PiShop, IN STOCK) | Unchanged |
| Official 27W USB-C supply | $12.95–$14.04 | **$11.95** (PiShop, IN STOCK) | **Down $1.00** |
| 32GB microSD (Pi OS 64-bit, the correct variant for Pi 5) | $19.95 | **$19.95** (PiShop, IN STOCK) | Unchanged |
| **Realistic total** (board from CanaKit + accessories from PiShop) | ~$154–158 | **≈ $152.85** ($110 + $10.95 + $11.95 + $19.95) | Slightly down, still comfortably under the $200 ceiling |

Conclusion: the volatility flagged in the earlier documents was real, but this round it cut in the buyer's favor (one price down, none up) — the risk just moved from "price" to "which specific reseller has it in stock this week." Re-check both dimensions immediately before ordering, not just price.

### The two previously-flagged discrepancies — checked, not assumed

1. **"$110 is a reseller markup over a $70 list price" framing.** Already corrected in `notes/FALL2026_RESEARCH_PROPOSAL_OPTIONS.md` (cross-cutting point #4) as of 2026-09-15: $110 is the Foundation's own current list price after two further increases past $70, not a markup. Re-reading that file fresh this session found no regression — the corrected framing is still there and consistent with today's re-verified $110. **Status: confirmed resolved, and still holding.**
2. **The InfluxDB crash-loop incident "not found" by an earlier grep-only pass.** `notes/HARDWARE_SELECTION_AUDIT.md`'s own correction (added 2026-09-15) says the incident is real and documented in two gitignored files. Verified fresh, this session, rather than trusting that claim on its own say-so: `notes/MEETING_SCRIPT_FINAL.md` and `notes/DEMO_DAY_OPERATOR_RUNBOOK.md` both exist on disk (confirmed via direct file listing), both are absent from `git ls-files` (confirmed not tracked), both are listed by exact filename in `.gitignore`, and both do contain the specific claimed incident detail (15 InfluxDB crash-loop restarts on 2026-08-13, ~23 on 2026-08-14, root cause investigated and not found) — grepped directly, not taken on faith. **Status: confirmed resolved.**

### Node classification carries forward: a real Pi 5 belongs on the structured branch

If and when a real Raspberry Pi 5 is added as a node, its CPU/memory/temperature readings must be treated as a **structured** signal in `twin/predictor.py`'s sense (`_is_structured()`), not routed through the noise-node decay branch. Reasoning: `_is_structured()`'s actual dividing line isn't "is this signal literally deterministic" — it's "does a training-window mean mean anything as a long-run estimate." A real Pi 5 under a sustained workload has genuine temporal structure (temperature climbs while load is applied and only reaches steady-state after a real thermal time constant, the same *shape* of problem as `node_0`'s CPU ramp, even though the exact curve won't replay a fixed formula) — decaying a stale prediction toward an early-window mean during a real thermal ramp would reproduce exactly the measured regression this repo already found and fixed for `node_0` (error growing unboundedly, up to −79.70 on `cpu` by 400s in the synthetic case). This is a design constraint to carry into whatever code change eventually wires up a real Pi 5, not a new mechanism — the existing `node_id.endswith(":node_0")` check would need to become (or be joined by) a check for the real device's node ID, nothing structurally new.

One honest caveat: this classification is about which *recovery behavior* is appropriate, not a claim that a real sensor's short-window fit will be as clean as `node_0`'s exact formula — real telemetry will have real per-sample jitter `node_0`'s synthetic ramp doesn't, which affects the *fidelity* claim (see the sampling-interval and tolerance-band points below) even though it doesn't change which branch is correct.

### Sampling interval — the existing justification does not automatically transfer, stated explicitly rather than assumed

Section 3.1 justified `PUBLISH_INTERVAL_SEC = 3` against `notes/scaling_results.json`'s measured Ditto RTT for this repo's own synthetic publisher (a single process on the same machine as the port-forward, HTTP over `localhost`). A real Pi 5 introduces at least two sources of delay that measurement never included: real Wi-Fi RTT/jitter between the device and the router, and OS scheduling jitter on a small ARM SoC under whatever else it's doing. **This does not transfer cleanly, and this document is not assuming it does.** Whether 3s remains a safe budget once a real network hop and a real OS scheduler are in the loop is an open, re-measurable question — not a re-derivation from existing numbers, an actual new measurement once hardware exists (e.g., timing real Pi-5-to-Ditto PATCH round trips the same way `scaling_benchmark.py` already times synthetic ones).

### Failure-detection tolerance band — real sensor noise is a genuinely open question, not resolved here

`UNCHANGED_TOLERANCE` (±0.5 on `cpu`/`mem`/`latency`) was explicitly logged everywhere it's defined as "chosen, not measured" against this repo's synthetic `random.uniform()` data, which has no measurement noise at all — it's either the same value or a fresh independent draw, nothing in between. A real Pi 5's CPU/temperature readings at physical equilibrium will have real, continuous measurement jitter (sensor quantization, brief scheduling-driven load spikes) that could plausibly exceed ±0.5 routinely, which would make the detector flag a perfectly healthy, stable Pi 5 as "still changing" — the opposite failure mode from the one this tolerance band was built to fix. **Flagged as open, not answered:** whether ±0.5 needs retuning against real hardware can only be determined once real equilibrium data exists to look at; this document does not guess a replacement number without that data.
