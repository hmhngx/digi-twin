# Purpose and Direction Audit

**Written:** 2026-09-23, at HEAD `dfe5be3`.

**The question this answers, verbatim:** *"What is my digital twin be used for, what purpose, and how are we going to do that — if not, keep fixing the app."*

**Scope:** purpose and direction only, not code quality. No code was changed to write this.

**Method:** every document cited below was read in full this session. Every code claim comes from reading or grepping the current files at `dfe5be3` this session. The Micelli & Montagna paper was re-extracted from the PDF this session (`C:\Users\minhh\Downloads\3748699.3749778.pdf`, 9 pages), and so was the OpenTwins paper (`C:\Users\minhh\Downloads\openTwins.pdf`). Where no evidence was found, this document says so rather than filling the gap.

---

## Verdict

**No.** This project cannot currently state a specific real-world purpose for its digital twin — a named person or system that uses the twin's state, and a decision or action that depends on it. So it also cannot state a concrete plan to deliver one.

Every purpose written down in this repo falls into one of two groups:
- **An academic purpose for the builder:** learn what a digital twin is, show that learning to Prof. Siddiqui, answer her specific directives, and earn funding for hardware. This is real and well documented, but it is a purpose for Harrison, not a use of the twin.
- **A single concrete research purpose:** a cloud-computed fan controller for a Raspberry Pi 5, competing against the Pi's own firmware (written 2026-09-13/14). It was superseded two days later, when the professor deferred hardware on 2026-09-15. Nothing in the repo shows it has been revived since.

**Today, nothing reads this twin's state and does anything differently because of it.** The only output a human sees is a CPU chart. The only humans documented as looking at it are Harrison and the professor, during demos.

**Caveats, stated after the answer, not instead of it:**

1. **The academic purpose is legitimate and was largely met.** `notes/PROFESSOR_SUFFICIENCY_AUDIT.md` (2026-08-11) judged the build "sufficient with caveats" to show genuine understanding. `notes/FINAL_GATE_SUMMARY.md` (2026-08-13) says it met the professor's build mandate at that time. It just doesn't answer "what is the twin used for".
2. **The Pi 5 fan-control purpose is the one specific use ever written down.** It is *deferred*, not cancelled (`notes/EXPERIMENTAL_TWIN_REQUIREMENTS.md`: "the purchase decision itself is deferred"). Reviving it is the professor's decision, behind the purchase gate in `notes/PURCHASE_CHECKLIST.md`.
3. **The real-data gap has narrowed but the purpose gap has not moved.** Since 2026-09-22, `node_0` carries real telemetry from the laptop running the publisher (commit `6aca4ca`). §2.4 below shows these are two different problems, not one problem under two names.
4. **The professor's own words are known only as relayed.** Her 2026-09-22 feedback was that the build is "not 100% correct for the purpose of a digital twin" (`notes/DIGITAL_TWIN_ADAPTATION_ANALYSIS.md`, line 5). This audit cannot tell what she meant beyond that relay. The draft asking her (`notes/PROFESSOR_UPDATE_2026-09-22_DRAFT.md`) is unsent; no evidence was found in this repo that it has been sent.

---

## 1. What the project's purpose has been said to be, over time

Only what each document says about *why the system exists or what it is for* — not what it does technically.

| Date | Source | What it says the work is for | Status now |
|---|---|---|---|
| 2026-07-26 | Commit `04570a3` message | "Complete cluster twin PoC on OpenTwins with verified composition and pipelines." A build and verification milestone. No use is stated. | Historical |
| 2026-08-06 | `notes/PROFESSOR_DEMO_PLAYBOOK.md`, header | "Audience: academic / professor walkthrough." | Historical |
| 2026-08-11 | `notes/PROFESSOR_SUFFICIENCY_AUDIT.md`, "Question" line | "Is this proof-of-concept sufficient to demonstrate genuine understanding of digital twins and of OpenTwins specifically to an academic reviewer (Prof. Siddiqui)?" | Met, per its own verdict "Sufficient with caveats" |
| 2026-08-13 | `notes/FINAL_GATE_SUMMARY.md`, "Professor's actual mandate" | "general digital-twin literacy, a working compositional twin built from this repo, and a joint decision on the Fall research topic — **not** a full reproduction of the 2023 paper." | First two parts met by 08-13. The third — a Fall research *topic* — was never jointly decided on record. The 09-14 plan's "decision" was Harrison's pre-meeting proposal, and the 09-15 meeting replaced it with learning directives rather than a topic (see those rows below). |
| 2026-08-13 | `notes/DEMO_REQUEST_EMAIL.md` (commit `27a74a8`) | "demonstrate the digital twin proof-of-concept I built over the summer … and discuss how it connects to the Fall research direction"; "what this taught me about Ditto's compositionality model." | Historical |
| ~2026-08-14 | `notes/MEETING_SCRIPT_FINAL.md` (gitignored; its own text dates itself by "repo `git log`, 2026-08-14") | Closing line: *"What I don't have today is a Fall research topic to hand you."* On domain: the compute cluster was chosen "as a different domain that preserves the same compositional pattern". The domain was picked to mirror the paper's structure, not to serve a use. | Historical; the most explicit on-record statement that no purpose beyond demonstrating understanding existed at that point |
| 2026-08-25 | `README.md` (then-current) and `notes/twin_design_spec.md` line 3 | "Academic proof-of-concept digital twin." The README says the repo **is** "lab evidence of compositional twins" and is **not** "a production-scale cluster twin." | Still the framing of both files today |
| 2026-09-13 | `notes/FALL2026_RESEARCH_PROPOSAL_OPTIONS.md` | A menu of four options for a "Fall 2026 cyber-physical extension". Option A's research question: *"Can a control algorithm running in the cloud … keep a computer cooler and quieter than the fan-speed rules built into the computer's own firmware?"* Option A is the recommended default. | **Superseded 2026-09-15** (see that row) |
| 2026-09-13 | `notes/HARDWARE_SELECTION_AUDIT.md` | Fact-checks an external PDF recommending a Pi 5 as the physical "Thing" for that Fall extension. It concludes the recommendation is "sound, with caveats". | Its purchase recommendation is "still pre-decision" (its own 2026-09-16 status note) |
| 2026-09-14 | `notes/FALL2026_ACTION_PLAN.md` | "Primary track: Option A." Phase 4: "Run the comparison this whole project is meant to produce: the new cloud-based controller vs. the Pi 5's own stock firmware heuristic." "What done looks like": a Pi 5 "whose fan is being controlled by a cloud-computed decision instead of its own firmware". **This is the only concrete real-world use of the twin's state ever written down in this repo.** | **Superseded 2026-09-15** |
| 2026-09-15 | `notes/EXPERIMENTAL_TWIN_REQUIREMENTS.md`, "Scope pivot" | The 09-13/14 cloud fan-controller framing "did not survive today's meeting." The professor's direction, as relayed: "learn to build a working digital twin with what already exists before any hardware purchase is even decided, specifically by (1) grounding the work in a real paper about building a digital twin for experimental purposes — data sources, parameters — and (2) working out how this twin handles prediction." "There is no physical experiment." | These are learning directives, not a use for the twin. The document never states what the twin is for. |
| 2026-09-17 | `notes/PROFESSOR_PRESENTATION_SCRIPT.md` (untracked; it cites commit `ca175ee` of 2026-09-17) | "this is the meeting the hardware decision is gated on." The demo's purpose was to earn funding approval for the Pi 5. | Overtaken by the professor's 2026-09-22 feedback. Its `node_0` "ramp" framing is now stale. |
| 2026-09-17 | `notes/PURCHASE_CHECKLIST.md`, `notes/PROFESSOR_UPDATE_DRAFT.md` (commit `551e87c`) | Funding is gated on a successful demo. The old draft asked for that demo. | The old draft is marked superseded (2026-09-22). The checklist still says no order has been placed. |
| 2026-09-22 | `notes/DIGITAL_TWIN_ADAPTATION_ANALYSIS.md` (untracked) | The professor's feedback, as relayed: "not 100% correct for the purpose of a digital twin"; "predicted data must not be used to test the digital twin"; read Micelli & Montagna; "the current build may need to be reproduced as a different model". The analysis classifies the system as a Digital Model and lists Options A/B/C. It says explicitly that it "does not decide the adaptation path." | Current diagnosis. It states no purpose. |
| 2026-09-22 | Commits `6aca4ca`–`dfe5be3`; `notes/PROFESSOR_UPDATE_2026-09-22_DRAFT.md` | Option A landed: `node_0` is real host telemetry. The draft asks the professor "whether this is the right direction", whether to "narrow the system down to fewer parts that are all real", and "how important is the two-way part". | Unsent; no answer on record. The draft asks about *direction* and *scope*, but does not ask directly what the twin should be *used for*. *(Update, 2026-09-25: also stale -- same correction as the item 4 update note below. The draft now asks the purpose question directly; it remains unsent.)* |

**The shape of the timeline.**
1. The purpose began as demonstrating understanding (July–August).
2. It briefly became one concrete research use: cloud fan control of a real Pi 5 (09-13/14).
3. The professor deferred that on 09-15. It was replaced by learning directives with no stated use (09-15).
4. Those produced a demo whose goal was hardware funding (09-17).
5. That demo drew feedback questioning whether the system is a digital twin at all (09-22).
6. The response to that feedback so far is a data-source change plus an unanswered question to the professor (09-22).

At no point after 2026-09-15 does any document state a real-world use for the twin's state.

---

## 2. Who or what actually uses this twin's state

### 2.1 The benchmark: what Micelli & Montagna actually demonstrate (re-verified against the PDF)

The paper states a concrete purpose up front, and names the problem it answers:
- Abstract: "As a proof of concept, we present an application in the context of hypertensive patient care"
- Introduction: its goal is "interoperable, scalable, and clinically useful HDTs"
- §4 names the problem: chronic disease management suffers "fragmented care, delayed diagnoses and, especially, poor long-term outcomes", partly from "limited access to specialised care"
- §4 names the use: "a full-stack system … that enables remote monitoring, data collection and analysis of different information related to the management of hypertensive people"

Its case study uses real data sources:
- a Samsung Galaxy Watch 5 for blood pressure (§4.1.1);
- the AI 4 HyperTension Telegram chatbot for mood (§4.1.1).

Two of the task prompt's own phrasings are stronger than the PDF supports, so they are corrected here rather than repeated:
- **FHIR interoperability is a stated design goal, not a demonstrated consumer.** The paper says FHIR "can be exploited to formalise the model and interoperate with other healthcare services" (§3.2). No EHR or clinical decision-support system consuming the twin's state is shown. The **only downstream consumer the paper actually demonstrates** is "a web application that subscribes to the MqttDigitalAdapter … to asynchronously update a real-time chart for the patient's measures" (§4.1.3, Figure 7).
- **Whose data it is was not found in the extracted text.** The web application charts "the patient's measures" (§4.1.3), but no statement was found of who actually wore the watch, and no evidence was found of a clinician acting on the output. Validation against certified medical devices and "a large population sample" are listed as future work (§5).

So the paper's demonstrated consumer is *also* a person looking at a chart. The real difference from this repo is not automation, since the paper doesn't demonstrate any. It is that the paper names:
- a **domain and a user** — hypertensive patients and their care;
- a **physical entity the data really comes from**;
- a **standard aimed at named future consumers** — healthcare services, via FHIR/LOINC.

Measured by that bar, this repo has a physical source for one node since 2026-09-22 — and no named domain use, no user, and no intended consumer.

### 2.2 Every reader of the twin's state in this repo, traced from the current code

Found by grepping `twin/*.py` and `scripts/*.py` at `dfe5be3` for every Ditto `GET`, every Influx query, and every read of the detector's state file. Also grepped for anything that alerts, notifies, or actuates.

| Reader | What it reads | What it does with it | Acts on anything outside the twin? |
|---|---|---|---|
| `twin/aggregator.py` (`get_feature()`, lines 27–31) | Each node's `cpu_utilization` and `active_connections`, then each rack's aggregates | Writes rack and cluster rollups (`avg_cpu_utilization`, `total_active_connections`, `healthy_node_count`) back into Ditto | **No.** It is the twin computing derived state from itself. |
| `twin/failure_detector.py` (`snapshot_node()`, line 80) | Every node's four features | Prints `FAILED`/`CLEARED` and predictions to its console, and writes `notes/_failure_state.json` | **No.** No committed file reads `_failure_state.json` (`git grep` finds it only in `failure_detector.py` itself), and nothing is notified. |
| Grafana, `grafana/dashboard.json` | InfluxDB fields `value_cpu_utilization_properties_value` and `value_avg_cpu_utilization_properties_value` only (4 panels) | Draws charts and a table | **No.** The dashboard JSON contains zero alert definitions. Whether the live Grafana instance has alert rules configured outside this JSON was **not checked** (it needs a login). No evidence was found in this repo of any. |
| `scripts/setup_types_and_twins.py` (line 246), `scripts/phase5_mqtt_influx.py query` (line 117) | Things and Influx points | One-shot setup and verification output for the operator | **No** |

**State that is shown to no one.** Of the seven kinds of state this twin maintains, the dashboard displays two: node `cpu_utilization` and rack/cluster `avg_cpu_utilization`. The other five — `memory_utilization`, `active_connections`, `latency_ms`, `total_active_connections`, `healthy_node_count` — each appear **0 times** in `grafana/dashboard.json`. Their only code readers are the twin's own internal loops:
- `healthy_node_count` and `total_active_connections`: read only by the aggregator, to roll racks up into the cluster;
- `active_connections`: read by the aggregator (summed per rack) and by the failure detector;
- `memory_utilization` and `latency_ms`: read only by the failure detector's change and prediction logic.

**Anything that acts.** The only "command" anywhere in committed code is `scripts/phase5_mqtt_influx.py` line 196, which opens Ditto's own MQTT connection — infrastructure, not a decision based on twin state. No alerting, notification, webhook, setpoint, or actuation code was found.

**The honest answer: nothing acts on this twin's state. A person can look at a CPU chart.** The only people documented as doing so are the demo audience (`PROFESSOR_DEMO_PLAYBOOK.md`: "Audience: academic / professor walkthrough") and Harrison as operator (`README.md`, "Windows operator run").

### 2.3 Does failure detection and prediction serve a purpose of its own, or is it orphaned?

**The OpenTwins paper's reason for prediction**, confirmed in `openTwins.pdf` this session: when a sensor misses its period, "Kafka-ML will predict the next state of the system to avoid a service interruption due to the sensor failure until the sensor is available again." That reason only holds if the prediction *reaches* something that would otherwise be interrupted.

**This repo's own stated reasons, from its notes:**
- **Coverage of the paper's architecture.** `FINAL_GATE_SUMMARY.md` row 10 lists "Sensor failure detection" as a paper-derived requirement to build *and* explain, under the professor's literacy mandate. `twin_design_spec.md` calls it "a simplified stand-in" (out-of-scope item 3).
- **The professor's second directive.** `EXPERIMENTAL_TWIN_REQUIREMENTS.md`'s scope pivot relays it as "working out how this twin handles prediction."
- **Buildability.** The same document's "Hand-off to Part 2" section picks the statistical predictor because it "is the only one of the three that fits both what was actually said and what section 2 … establishes is buildable without hardware."

**What happens to the prediction:** by an explicit 2026-09-16 decision it is "log-only". Per `twin_design_spec.md`, it "never reaches Ditto or Grafana"; per `EXPERIMENTAL_TWIN_REQUIREMENTS.md`, "Decision, made 2026-09-16: log-only". It goes to a console and to a file no committed code reads (§2.2).

**Verdict: orphaned.** No document in this repo names a consumer who needs a predicted value during a dropout, and the design deliberately keeps the prediction away from every place a consumer could see it. So the paper's rationale — preventing a service interruption — has no service to protect here. The subsystem was built to answer a learning directive and to mirror the paper's architecture, which is a legitimate *learning* reason but not a use.

The same holds for the liveness detection itself: `FAILED`/`CLEARED` goes to a console, and nothing and no one is notified. The repo already half-says this. `PREDICTOR_VALIDATION.md`'s 2026-09-22 section records "whether prediction stays in this project at all is an open question after the professor's feedback."

### 2.4 Are "no purpose" and "no real data" the same blocker, or two?

Worked out from the evidence rather than assumed: **two different problems, with a clear order between them.**

**Evidence they are separable.** On 2026-09-22 the real-data gap was partly closed: `node_0` became a Digital Shadow node (commit `6aca4ca`; `twin_design_spec.md`, "Where node feature values come from"). The purpose gap did not move at all. Everything in §2.2 was traced *after* that change, and nothing consumes `node_0`'s real values any differently than it consumed the old synthetic ramp. Real data turned out to be necessary but not sufficient.

**Evidence that purpose comes first.** Without a purpose, there is no principled way to choose *which* real data to collect, and the current real node shows it:
- **Its asset** is the laptop running the code, chosen because it costs nothing and takes hours (`DIGITAL_TWIN_ADAPTATION_ANALYSIS.md` §5 Option A: "zero cost, available immediately"; "Effort: hours, not days"). The same section names the weakness itself: "'the twin of the laptop it's running on' is a narrower, less externally-legible physical counterpart."
- **Its features** were chosen to fill the four slots the synthetic `NodeType` already had. `latency_ms` became "TCP connect RTT to `1.1.1.1:443`" and `active_connections` became "ESTABLISHED TCP count" because those slots existed, not because anyone needs them (`twin/publisher.py`, `measure_host()` docstring). `HARDWARE_SELECTION_AUDIT.md` (question 3) had already flagged that these two features "have no natural physical analog once a node is a real device."

By contrast, the one time a purpose existed — the Pi 5 fan controller, 09-13/14 — it dictated the data (temperature, fan PWM) *and* named the missing half (an actuation path and a controller). `FALL2026_ACTION_PLAN.md` Phase 2–3 specify both.

**Conclusion.** The real-data gap is real, but it sits *downstream* of the purpose gap. A purpose decides what physical thing to twin, which data matters, and who consumes it. Real data without a purpose — today's `node_0` — produces a working shadow that serves nothing. Neither problem is an engineering problem at its root: the real-data gap is blocked on choosing an asset, and the purpose gap is blocked on choosing a use.

---

## 3. If yes

Does not apply. The verdict is no.

---

## 4. What has to become true before writing more code is the right next move

Every item is a missing decision or a missing real-world hook. None of them can be fixed by writing code.

1. **A decision on what the twin is a twin *of*.** The physical asset, and why that one. On record today:
   - this laptop (what `node_0` measures now, chosen for cost, not purpose);
   - a Raspberry Pi 5 (deferred 2026-09-15);
   - whatever "the current build may need to be reproduced as a different model" (the professor, as relayed 2026-09-22) points to.

   *Decided by:* Harrison and Prof. Siddiqui.
2. **A named consumer of the twin's state.** A person in a specific role, or a specific system, and the question they need the twin to answer. *Today:* none beyond the demo audience (§2.2).
3. **A decision or action that depends on that state.** Without one, even a fully real shadow serves nothing (§2.4), and the "twin" level of the taxonomy — something acting back on the physical side — cannot be reached. *The only such action ever written down:* a fan-speed decision (`FALL2026_ACTION_PLAN.md`), currently deferred.
4. **The professor's read on the 2026-09-22 diagnosis.** She needs to confirm or correct the "Digital Model" interpretation (`DIGITAL_TWIN_ADAPTATION_ANALYSIS.md` §3 flags it as an inference, not her verdict), and say what "not 100% correct for the purpose of a digital twin" meant. *Pending:* the draft is unsent. As written, it asks about direction and scope but **never asks directly what the twin should be used for** — the question this audit finds unanswered. *(Update, 2026-09-25: this specific gap was closed — the draft now asks this directly; see the current text of `PROFESSOR_UPDATE_2026-09-22_DRAFT.md`. The draft is still unsent, which is the part of item 4 that remains open.)*
5. **Whether the two-way part is required for this project to count.** That is, whether a digital *shadow* is an acceptable end state, or a *twin* that acts on something physical is expected. This decides whether the Pi 5 purchase question reopens. *Pending:* asked in the unsent draft.
6. **A breadth-vs-depth scope decision.** Keep the 13-Thing cluster (one real node, nine synthetic), or narrow to fewer parts that are all real? Raised in `DIGITAL_TWIN_ADAPTATION_ANALYSIS.md` §5 Option C and in the draft. *Pending.*
7. **Whether prediction stays in scope, and if so, whose question it answers.** It is currently orphaned (§2.3). Its future depends on items 2 and 3: a prediction is only useful to a named consumer during a dropout that matters to them.

---

## 5. What this means for right now

**Stop extending or fixing the app until items 1–4 in §4 are answered.** Every code change since 2026-09-15 made a system with no stated use more correct:
- the tolerance band, the liveness fix, and the predictor policy;
- `node_0`'s real telemetry and the new predictor clamps.

That work was honest and is reusable (`DIGITAL_TWIN_ADAPTATION_ANALYSIS.md` §4.1 lists what carries over). But more of it now — for example the open finding that a plain window mean beats the linear trend, or the publisher's sequential-PATCH cadence — would be polishing capabilities that nothing and nobody consumes.

**The next step is a conversation, not code:**
- Revise and send the professor note so it asks the purpose question directly: *what should this twin be used for, by whom, and must it act on something physical?*
- Or bring her a concrete candidate purpose Harrison has chosen himself, with a named user and a named decision, for her to accept or redirect.

Keeping the existing stack runnable for that conversation is maintenance, not development. Beyond that, writing more code before the purpose is set risks a third round of building something the professor then says isn't what a digital twin is for.

---

## Noted along the way, not touched (outside this task's scope)

- `README.md` line 169 says the Ditto→MQTT connection "does not survive cluster restart." On 2026-09-22 it did survive a `minikube start` recovery (observed during the previous task).
- `README.md` line 457 links `notes/EMAIL_TO_PROF_RESCHEDULE.md`, which does not exist.
- `notes/PROFESSOR_PRESENTATION_SCRIPT.md` (untracked) still describes `node_0` as a ramp, and still says a real Pi 5 belongs on a freeze branch that no longer exists.
- `notes/HARDWARE_SELECTION_AUDIT.md` row 11 calls the repo "a unidirectional digital shadow." By `DIGITAL_TWIN_ADAPTATION_ANALYSIS.md` §3's own finding, that was an overclaim when written, because no node carried physical data then.
- `notes/DIGITAL_TWIN_ADAPTATION_ANALYSIS.md` and `notes/PROFESSOR_PRESENTATION_SCRIPT.md` are untracked. The unsent professor draft points the professor at the first of them.
