# Digital Twin Adaptation Analysis — Reading the New Paper Against the Actual Code

**Written:** 2026-09-22, in direct response to Prof. Siddiqui's feedback after seeing the live demo.

**What she said, as relayed:** what's built is not 100% correct for the purpose of a digital twin; predicted data must not be used to test the digital twin; read Micelli & Montagna, "Human Digital Twin for Healthcare Applications: a White Label Digital Twin Implementation" (GoodIT '25), to understand what a digital twin actually is; the current build may need to be reproduced as a different model, though some of it may be reusable.

**Method used to write this document:** the paper (`63bb8e87-3748699.3749778.pdf`, all 9 pages) was read in full, fresh, this session — not recalled from a prior session's summary. Every code claim below was read live from the actual files in this repo this session (`git log`, `git diff`, and direct `cat` of `twin/publisher.py`, `twin/config.py`, `twin/failure_detector.py`, `twin/predictor.py`, `twin/ditto_client.py`, `twin/aggregator.py`, `notes/EXPERIMENTAL_TWIN_REQUIREMENTS.md`, `notes/PREDICTOR_VALIDATION.md`), not carried forward from memory of earlier sessions. Where a claim below matches something already written in this repo's own `notes/`, that is noted explicitly — it means the repo had already partially diagnosed this problem itself, before the professor's feedback, not that this document is presenting old news as new.

**This document does not decide the adaptation path.** It lays out, as rigorously as the evidence supports, what the paper requires, exactly where this repo's synthetic data comes from, an honest taxonomy classification, what's reusable versus what must change, and three candidate paths. The choice is Harrison's and the professor's, not this session's.

---

## 1. What the paper actually says a digital twin is

Three claims from the paper are load-bearing for this analysis. All three are quoted, not paraphrased, so nothing is lost in translation.

### 1.1 The three-layer architecture

The paper's Digital Twin Layer (§3.1.2) has three parts: the **Model set M** (representations of the physical thing), the **Storage set S**, and **Augmentations** — plus a **Shadowing Function** that connects the Digital Twin Layer to the Physical Interface.

### 1.2 The Shadowing Function — what actually updates the twin's state

> "The Shadowing Function. It is responsible for listening to the Physical Interface for any physical event 𝑒𝑃𝑇 and updating the state of the DT accordingly, via one or more 𝑠 ∈ 𝑆..."

The state of the twin is updated **only** in response to a physical event arriving from the Physical Interface — i.e., from the real world. There is no other channel described anywhere in the paper by which the twin's stored state changes.

### 1.3 Augmentations — where prediction lives, and what it operates on

> "Augmentations. This set encompasses any augmented capability the DT can have. This includes predictive models, reinforcement learning models **that are fed with the physical human actual data**, reasoning engines..."

Augmentations (including predictive models) are explicitly described as consuming real, already-observed physical data. They are a layer that sits *on top of* a shadowing process that is itself grounded in the real world — never a substitute for it, and never the thing being validated *as if* it were the real world.

### 1.4 The paper's own case study is real-world data, end to end

The worked example (§4) uses a **Samsung Galaxy Watch 5** for actual blood-pressure telemetry (via Samsung Health Connect → Firebase → MQTT), and a real Telegram chatbot ("AI 4 HyperTension") whose actual conversations are fed to an LLM (Gemini) to extract structured mood data — also published over MQTT into the same Shadowing Function. Every number that ever reaches the twin's state in the paper's own implementation originates from something that happened in the physical world: a wearable sensor reading, or a real conversation a real patient had. The paper explicitly flags, as *future* work still to be done, validating extracted data against certified medical devices (§5) — meaning even the paper's authors treat "is this real-world data trustworthy" as an open question for *real* data, not something they'd consider asking about synthetic data at all.

**Net reading:** a digital twin, per this paper, is defined by having a Shadowing Function whose only input is physical events from something real. Prediction is an optional add-on layer that consumes real data once it exists — it does not create the twin's ground truth, and it is never used as a stand-in for missing real data.

---

## 2. Where every data point in this repo actually comes from

This is not new analysis — this repo's own `notes/EXPERIMENTAL_TWIN_REQUIREMENTS.md` (written 2026-09-15, after an earlier professor redirect) already stated it plainly in §2.1: *"There is no device... the twin's telemetry source and the twin's ground truth are, today, the same function. There is no independent channel a value could diverge from."* What follows is that same fact, re-verified fresh this session against the live code rather than trusted from that document's word, plus the connection to the new paper's specific definitions.

Read live this session, `twin/publisher.py` lines 33–54 (`node_features()`), the **sole** function that ever produces a value for any of the 10 node Things:

```python
if node_id.endswith(":node_0"):
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
```

- **`node_0`:** a closed-form function of wall-clock elapsed time. No sensor, no `/proc`, no `psutil`, no hardware call of any kind — `elapsed` is `time.time() - t0` and nothing else.
- **`node_1`–`node_9`:** `random.uniform`/`random.randint` calls. Each draw is statistically independent of every prior draw and of elapsed time. There is no signal here to predict *from* in any meaningful sense — extrapolating a trend line through i.i.d. noise is fitting structure that provably does not exist in the generating process.

Then `patch_node()` (lines 57–66) PATCHes these fabricated numbers straight into Ditto over HTTP. Nothing between `node_features()` and Ditto touches anything physical. This was traced end to end, live, this session — not assumed from a prior read.

Two immediate consequences:

1. **There has never been a Physical Interface in this system**, in the paper's sense. `twin/publisher.py` *is* what the paper calls the Physical Interface's job — except instead of collecting and pre-processing real-time raw data from a real source, it invents the data itself.
2. **`twin/predictor.py` is not an Augmentation in the paper's sense**, because an Augmentation is fed real physical data and this predictor is fed the output of `random.uniform()` (for 9 of 10 nodes) or a deterministic formula (for 1 of 10). It cannot be "fed with the physical human/asset's actual data" when no such data exists anywhere in the pipeline.

### 2.1 What `PREDICTOR_VALIDATION.md` actually validated — and why the professor's instruction lands exactly here

`notes/PREDICTOR_VALIDATION.md` (read fresh this session, its stated methodology in Part 3a) measured the predictor's error by comparing its output against **the live, unmodified value `twin.publisher.node_features()` itself would have produced** at the same elapsed time — i.e., against the synthetic generating formula, not against any independent real measurement. That is the single available "ground truth" in a system with no physical counterpart, and the document says so honestly (§2.3 of `EXPERIMENTAL_TWIN_REQUIREMENTS.md`: *"ground truth is only recoverable for `node_0`... for `node_1`–`node_9`, there is no such number"*).

This is precisely the shape of thing the professor told Harrison not to do: using predicted data (or, more precisely here, validating a predictor *against* fabricated data and treating the result as if it said something about the digital twin) as a substitute for testing against something real. The repo's own predictor never writes its predictions into Ditto — the "no-PATCH invariant" is real and was re-confirmed live this session (the only Ditto call anywhere in `failure_detector.py` is a `GET`; `predictor.py` doesn't import `ditto_client` at all) — so the system is not *lying to Grafana*. But the validation exercise that was used to justify the predictor's correctness is still, structurally, testing fabricated output against fabricated ground truth. There was never a way for it to be anything else, because there was never a real signal in the loop to test against.

---

## 3. Honest taxonomy classification — Digital Model, not Digital Shadow, not Digital Twin

This repo's `notes/EXPERIMENTAL_TWIN_REQUIREMENTS.md` and `notes/HARDWARE_SELECTION_AUDIT.md` both already reference the Kritzinger-style three-way split (Digital Model / Digital Shadow / Digital Twin) by the presence and direction of automated data flow between physical and digital. Applying it honestly to what was just re-verified:

- **Digital Model:** a digital representation of something, with **no automated data flow in either direction** between the physical and digital sides — data enters the model by manual/offline means, or (as here) is generated synthetically.
- **Digital Shadow:** an automated, **one-way** flow from a real physical asset into the digital representation (state changes automatically when the physical thing changes; nothing flows back).
- **Digital Twin:** an automated, **bidirectional** flow — the digital side can also act on the physical side.

Given §2 above — zero of the 13 Ditto Things in this deployment have ever received a value that originated from anything physical, at any point in this project's history — this system does not clear the bar for a Digital Shadow, let alone a Digital Twin. It has automated data flow, but there is no physical asset on the other end of that flow for the data to be *shadowing*. By the taxonomy this repo already cites, **the honest classification is Digital Model**: a well-built simulation of a plausible cluster, running on real platform infrastructure, with no physical counterpart.

This is very likely the core of what the professor meant by "not 100% correct for the purpose of a digital twin" — not a claim about code quality, but a claim about what category of system this is. It is worth being direct about the fact that this conclusion is this session's interpretation of her feedback, reached by applying a taxonomy already sitting in this repo's own notes to what the code was just shown to actually do — not something confirmed with her directly. It should be said to her in those terms (a hypothesis being checked), not presented as a restatement of what she said.

---

## 4. What's reusable, and what has to change

### 4.1 Reusable essentially as-is

- **The Ditto Thing/Type model and the compositional hierarchy** (`hnguyen.clustertwin`, 1 cluster → 2 racks → 10 nodes, `NodeType`/`RackType`/`ClusterType` cardinality-linked via the Extended API). This is a direct, verified implementation of Ditto's attribute/feature split and type-graph model — nothing about the paper's feedback bears on Thing modeling, and the paper's own Model-set/Storage-set abstraction (§3.1.2) is a generalization of exactly this kind of structure, not a contradiction of it.
- **The outbound pipeline** — Ditto → Mosquitto → Telegraf → InfluxDB → Grafana, re-verified live in earlier sessions and structurally identical to the paper's own MQTT-broker-out pattern (§4.1.2's `MqttDigitalAdapter`). This is genuine, reusable competency with the platform, independent of where the inbound data comes from.
- **The Ditto HTTP client, aggregation logic (`aggregator.py`), and the general shape of `failure_detector.py`'s liveness/timeout logic** (poll → compare age against a tolerance → flag failed/cleared) — none of this is data-source-specific. A real sensor feeding the same Thing IDs through the same PATCH calls would exercise identical downstream code.
- **The now-fixed liveness/staleness bug** (`last_seen` advancing on every successful poll, independent of whether the value changed) is *more* relevant with real data than with synthetic data, not less — a real sensor at physical equilibrium is exactly the case this fix protects against, and it was proactively found and fixed before any real hardware existed to trigger it. This is a concrete piece of forward-looking, non-wasted work.

### 4.2 Must change

- **The data source itself, for at least one node.** This is the entire finding of §2–3: nothing currently feeds this system anything real. Some genuinely physical signal — however modest — has to enter the Physical Interface / publisher role for the system to become even a Digital Shadow.
- **The predictor's role and validation story, if it is kept at all.** Per the paper's own definition (§1.3 above), an Augmentation is legitimate only when it is fed real data. If real data exists for at least one node, the predictor could become a genuine Augmentation for that node specifically — but its validation would then need to be against held-out *real* samples from that node, never against the synthetic formula that currently stands in for ground truth. For any node that remains synthetic, the predictor should not be described as validated "digital twin" behavior — the honest framing on `notes/PREDICTOR_VALIDATION.md`'s own current evidence is "this piece of statistics behaves as expected on synthetic data," not "this predicts the twin's true state."
- **The project's own vocabulary in its docs.** `README.md`, `twin/README.md`, and `notes/EXPERIMENTAL_TWIN_REQUIREMENTS.md` currently describe this as a digital twin (with disclosed simplifications relative to the OpenTwins paper's Hono/Kafka-ML machinery). Given §3, calling it a Digital Model until real data enters the loop is the more defensible framing to bring into the next meeting with the professor, rather than continuing to call it a twin with caveats.

---

## 5. Candidate adaptation paths

Presented as options, not a decision. Each is evaluated against the same question: does it give the system an automated flow of state from something genuinely physical, satisfying the paper's Shadowing Function requirement for at least part of the deployment?

### Option A — Real telemetry from Harrison's own machine (`psutil`), zero cost, available immediately

Replace `node_0`'s (or one dedicated new node's) synthetic formula with real host metrics: actual CPU utilization, actual memory utilization, and a real-ish proxy for the other two features, read via `psutil` (confirmed importable in this session's environment; not currently in `requirements.txt`, a one-line add) or, on Windows, equivalents like `psutil.cpu_percent()` / `psutil.virtual_memory()`. The publish loop's shape (`patch_node()`, the PATCH-to-Ditto call, the interval loop) does not need to change at all — only `node_features()`'s branch for that one node.

- **What this achieves, precisely:** for that one node, the system would gain a genuine Shadowing-Function-equivalent (real host state → automated PATCH into Ditto), clearing the Digital Shadow bar for the first time in the project's history, at zero dollar cost and no hardware lead time.
- **What it does not achieve:** it is not a new physical *asset* distinct from the machine already running the code — a skeptical reading (worth naming rather than hiding) is that "the twin of the laptop it's running on" is a narrower, less externally-legible physical counterpart than a separate device like a Raspberry Pi. It also does not, by itself, restore any bidirectional/actuation story — still one-way, still a Shadow rather than a Twin, unless paired with something to act on (see Option C).
- **Effort:** hours, not days. No hardware ordering gate applies, since nothing is being purchased.

### Option B — Revive the Raspberry Pi 5 hardware plan, now with a clearer justification for *why*

The hardware/funding workstream (`notes/HARDWARE_SELECTION_AUDIT.md`, `notes/PURCHASE_CHECKLIST.md`, `notes/HARDWARE_MEASUREMENT_PLAN.md`) is not obsolete — if anything, this analysis gives it a sharper rationale than it had before: a Pi 5 reading its own real `/proc`/`/sys` sensors and (per the original proposal) taking a real fan-speed command would satisfy both halves of the paper's architecture at once — a genuine Shadowing Function (temperature → Ditto) and a genuine bidirectional loop if actuation is implemented (Ditto → fan command), which is what would let this system honestly claim Digital Twin status, not just Digital Shadow.

- **What this achieves:** the most complete alignment with the paper's architecture of any option here — real physical events in, and (if actuation is built) real actions back out, matching the paper's Digital Interface / action-invocation description (§3.2.1, "actions").
- **What it does not achieve immediately:** nothing until hardware is ordered and arrives — and per this project's own established, professor-approval-gated purchase process (`notes/PURCHASE_CHECKLIST.md`'s explicit "no order placed" state), that requires her sign-off first, likely on exactly the kind of write-up this document is a precursor to.
- **Effort:** the existing measurement plan and budget are already drafted; this is mostly a question of whether the professor now agrees the case for it is stronger, given what today's feedback implies about the software-only build's ceiling.

### Option C — Both, sequenced

Do Option A first (this week, no approval needed, no cost) to have *something* genuinely real in the loop to show next, then bring Option B to the professor as the follow-up ask, now backed by a working (if modest) real-data proof point rather than a purely hypothetical one. This does not preclude eventually narrowing scope back to a single real node if the professor prefers a smaller, more rigorous system over the current 13-Thing synthetic cluster — that is itself a fourth kind of choice (breadth vs. depth) worth raising with her rather than deciding here.

---

## 6. What this document deliberately does not do

- It does not pick Option A, B, or C. That is Harrison's call, informed by what's feasible before the next meeting, and ultimately the professor's call on what satisfies the assignment.
- It does not claim certainty about what the professor meant by "not 100% correct" beyond what's directly quoted from the paper and directly observed in the code. The Digital-Model classification in §3 is flagged as this session's inference, to be checked with her, not asserted as her verdict.
- It does not touch the hardware purchase gate. Per `notes/PURCHASE_CHECKLIST.md`, no order happens without her explicit approval, and nothing here changes that.
- It does not modify any code. This is analysis only, per the explicit scope of the task that produced it.
