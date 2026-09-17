# Fall 2026 Physical-Twin Research Options — Proposal Menu

**Prepared:** 2026-09-13
**Purpose:** A menu of feasible research directions to bring to Prof. Siddiqui for the Fall 2026 cyber-physical extension of this repo's cluster-twin PoC. Not a single forced recommendation — four real options, each explained plainly, each with its numbers checked.

**How to read the confidence tags used throughout:**
- **[VERIFIED LIVE]** — checked against an official/primary source this session (2026-09-13), with the source named.
- **[FROM CODE AUDIT]** — established by directly reading this repo's own source files earlier this session (see `notes/HARDWARE_SELECTION_AUDIT.md` for the line-by-line trail).
- **[ESTIMATE]** — a reasonable engineering estimate, explicitly *not* pinned to a checked source. Treated as approximate, not a quote.

This document builds on `notes/HARDWARE_SELECTION_AUDIT.md`, which fact-checked an earlier AI-written hardware recommendation against this repo's actual code and against live external sources, and found real errors in it (a mischaracterized API, a self-contradicting timing figure, several bad citations). Everything below has been re-derived independently rather than carried over from that flawed document.

---

## Before the options: what's true for *any* choice you make

These apply no matter which idea gets picked, because they're facts about the existing repo and its environment, not about any one piece of hardware.

**1. The current twin only pushes data outward — nothing pulls a command back in.** [FROM CODE AUDIT] `twin/publisher.py`, `twin/aggregator.py`, and `twin/failure_detector.py` only ever *send* data to Ditto. None of them contains any code that reads a command and acts on it. Whichever idea you pick, "receiving a command from the cloud and doing something physical with it" is a feature that has to be built from nothing — it doesn't exist today in any form.

**2. All three existing scripts crash (don't recover) if Ditto becomes briefly unreachable.** [FROM CODE AUDIT] Confirmed by this repo's own `notes/FINAL_READINESS_AUDIT.md`: all three scripts throw an unhandled exception and exit if the connection drops, rather than retrying. Whatever new script talks to your physical device will inherit this same fragile pattern unless you deliberately wrap it in a restart loop (the repo's own operational notes show this was a real, recurring problem even for the pure-software version — port-forwards died silently and repeatedly during actual work sessions).

**3. Running something unattended for weeks has already broken this exact dashboard once — and that is only one of two separate, documented InfluxDB incidents, not the whole story.** [FROM CODE AUDIT, corrected 2026-09-15] `notes/FINAL_READINESS_AUDIT.md` §4 documents a real bug: after about 17 days of continuous operation, the Grafana dashboard silently stopped working because InfluxDB was choking on too many unique tag values (a random per-message ID stored as a tag instead of a field). It was found and fixed. **Separately, and unresolved:** `notes/MEETING_SCRIPT_FINAL.md` §5 and `notes/DEMO_DAY_OPERATOR_RUNBOOK.md` document a second incident — the InfluxDB pod crash-looping on a cold cluster restart, 15 times on 2026-08-13 and ~23 times on 2026-08-14. This was actively investigated (resource limits, OOM kills, disk pressure, and whether it was the *same* tag-cardinality issue were all checked) and the root cause was never found. An earlier pass of this repo's own hardware audit searched for exactly this and found nothing — not because it doesn't exist, but because both source files are excluded from git by exact filename in `.gitignore` (likely because they also hold live credentials), so they're invisible to any repo-wide search and were only found this session by reading the notes directory directly. A physical device running unattended for a semester should be planned around *both* incidents, not just the one with a clean resolution.

**4. Memory prices are currently volatile industry-wide, and this is already showing up in real prices, not just a future risk — though the mechanism is simpler than an earlier pass of this document described.** [VERIFIED LIVE, 2026-09-15] The Raspberry Pi Foundation has raised its own official price for the Pi 5 4GB board **three times** in five months, each announced directly on raspberrypi.com: $60→$70 (Dec 1, 2025), $70→$85 (Feb 2, 2026), $85→$110 (Apr 1, 2026) — all attributed to the same LPDDR4 memory shortage. **$110 is the Foundation's own current list price, confirmed independently three ways: two in-stock resellers charging it directly, arithmetic from the three announcements, and a price-tracking forum thread showing no change since Apr 12, 2026 — reconfirmed fresh again on 2026-09-15, still $110.** An earlier pass of this document described $110 as a reseller "street price" markup over a still-current $70 "list price" — that framing was wrong; there is no markup, only a list price that moved twice more after $70 was checked.

**The Pi Zero 2 W is a genuinely different case, not "the same shortage" — checked directly rather than assumed, after the Pi 5 mistake above made that assumption look risky.** Raspberry Pi's own supply-chain update states plainly: *"We're currently planning to keep Zero 2W at $15, though we have pretty thin margins at that price point"* (raspberrypi.com, "Supply chain update — it's good news!," fetched directly 2026-09-15). That's a deliberate decision to hold this board's price despite cost pressure, not evidence it was hit by the same LPDDR4 dynamic as the Pi 5 — and PiShop's $17.25 (reconfirmed fresh, still current) is best explained as ordinary reseller margin on a thin-margin board, not a shortage echo. Every dollar figure in this document uses the current, verified price, not a stale or wrongly-explained one.

---

## Option A — Raspberry Pi 5 thermal control loop *(the strongest option)*

### The one-sentence pitch
Swap the fake, randomly-generated CPU/memory numbers on one "node" for real numbers read off an actual mini-computer, then teach the cloud twin to fight that computer's own overheating by controlling its fan — and see if a cloud-computed controller can out-perform the fan's built-in factory logic.

### The research question
*Can a control algorithm running in the cloud (getting sensor data over a network, thinking, and sending a command back over the same network) keep a computer cooler and quieter than the fan-speed rules built into the computer's own firmware — despite the extra delay of round-tripping every decision through the cloud?*

This is a real, well-studied class of question in cyber-physical systems (the tension between fast-but-dumb local control and smart-but-laggy remote control), and it's a fair fight: the Pi 5's firmware fan curve is a genuinely competent baseline to try to beat, not a strawman.

### Theoretical grounding — Huang, Jin & Wang, CSAE 2018, read in full for this write-up

This research question has a real, citable theoretical anchor: Cao Huang, Huiyu Jin, and Ying Wang, "Stability for Sampled-Data Systems with Delay and Applications for Networked Control Systems," *CSAE '18* (ACM, DOI 10.1145/3207677.3278102). The paper was read in full — abstract through references — for this section, not summarized secondhand.

**What Theorem 1 actually proves, precisely.** For a nonlinear plant under state-feedback control `u_T(x)`, if **(A1)** the control law is Lipschitz continuous (‖u_T(x) − u_T(y)‖ ≤ L₀‖x−y‖, for sampling period T below some threshold) and **(A2)** the *undelayed* Euler-approximation of the resulting closed loop is globally exponentially stable, **then** the *delayed* system is also globally exponentially stable, for T below a derived threshold T\*. The delayed control signal is modeled with delay τ_k assumed strictly less than the sampling period (τ_k ∈ (0,T)) — but this is a standing assumption of the model itself, set up in the paragraph just before the theorem, **not one of the theorem's two labeled conditions (A1)/(A2).**

**The bounded-dropout piece is a separate move in Section 4, not part of Theorem 1 itself — worth being precise about rather than blurring the two together.** After proving Theorem 1, the paper's Section 4 ("Applications in NCS") separately assumes the actuator receives at least one update every *l* sampling periods, and argues that as long as *l·T < T\** (the same threshold from Theorem 1's own proof), a bounded run of dropouts can be treated as one effectively-lengthened delay and the theorem can still be invoked. Theorem 1 proves delay-tolerance directly; dropout-tolerance is a subsequent, less formally developed extension of it in the same paper, not a second theorem.

**Validation in the paper: simulation only, one abstract plant, no hardware.** A single second-order nonlinear example (ẋ₁=x₂, ẋ₂=−x₁+sin(x₂)+u), T=0.1s, delay uniform on [0.01, 0.03]s, 50% dropout probability, l=4, run in MATLAB (Fig. 2–3 of the paper). Confirmed directly from the PDF — there is no hardware validation of any kind.

**Venue, stated honestly.** ACM-published, peer-reviewed, DOI-registered — real, checkable content. But CSAE (Computer Science and Application Engineering) is a broad-scope regional conference (Hohhot, China, Oct 2018), not a flagship control-systems venue. It's cited here for the precision of its actual theorem and proof, not for outsized authority.

**Does real measured data already exist in this repo to check against Theorem 1's conditions? Partially — and the two halves (delay, dropout) are in very different states.**

- *Delay: yes, real per-sample data exists.* `notes/scaling_benchmark.py` measures `ditto_rtt_s` — wall-clock time from an HTTP PATCH send to Ditto's HTTP response. This is the closest real analog this project has to the paper's sensor-to-controller network delay, though it's a full round trip including Ditto's own actor-model processing, not a pure one-way network delay — that distinction is worth keeping, not blurring. The **raw, per-sample values currently on disk in `notes/scaling_results.json`** (not just the hand-summarized averages in `notes/scaling_comparison.md`) show individual RTTs ranging from ~140ms at 1 concurrent node up to a 1.27–2.54s spread across 15 concurrent clients on a single Thing, and 2.65–2.83s at 20 clients — though only **2 of the 20 attempted samples are recorded** in that highest-concurrency bucket in this specific file (see the dropout note below for why that number shouldn't be over-interpreted). Compared against this repo's actual production sampling interval (`PUBLISH_INTERVAL_SEC = 3s`, `twin/config.py`), low-concurrency delay sits comfortably under the sampling period; high-concurrency delay approaches — and for the missing samples, plausibly exceeds — it. That's a real, informal echo of exactly the condition (τ_k < T) Theorem 1's model assumes.

- *Dropout: no formal measurement exists.* `scaling_benchmark.py` never computes or stores a dropout count or rate — a failed PATCH raises an exception rather than being tallied. The closest available signal is qualitative and inconsistent across runs: this repo's own narrative documents record 3-of-20 completing at 20 concurrent clients on one dated run (2026-07-28, per `notes/VERIFICATION_REPORT.md` §7) and 20-of-20 on another (2026-08-12, per `notes/scaling_comparison.md`), while the raw JSON currently on disk shows only 2-of-20 — three different counts under nominally comparable conditions, which is itself informative about how variable this behavior is on this local setup, but none of it is the same phenomenon as the paper's network packet dropout (a communication-channel loss model with a stated per-transmission probability). This is HTTP-level request failure under server-side contention, not network loss. **Treating it as directly comparable to the paper's dropout model would overstate what was actually measured. If a dropout-rate figure genuinely comparable to the paper's is wanted, that data collection does not exist yet — it would be new semester work, not something already on record.**

**Will this project formally verify Theorem 1's Lipschitz/GES assumptions against a real dynamic model of the Pi 5's thermal/fan plant? No — stated plainly rather than implied otherwise.** Doing so would mean deriving and validating an actual differential-equation model of the Pi 5's thermal mass and fan response, establishing real Lipschitz constants for it, and proving the undelayed Euler approximation is globally exponentially stable — a substantial control-theory research effort on top of everything else already scoped this semester (real telemetry, command delivery, a first controller, the comparison experiment itself). **This project is scoped as empirically motivated by Theorem 1, not a formal test of it.** The paper's result is the theoretical reason to expect a networked controller *can* remain stable despite realistic delay — which is why the cloud-controller-vs-firmware comparison is worth attempting at all — but the semester's actual controller is planned to start as a simple proportional controller (`notes/FALL2026_ACTION_PLAN.md` Phase 3), tuned empirically against observed behavior, not derived from a proven-Lipschitz plant model. Any framing stronger than "motivated by" would not be honest about what's actually planned.

**This project's own reliability work sits exactly in the region the paper's theorem explicitly excludes — a real, citable connection, not a stretch.** The paper states its boundary directly: *"this assumption excludes long-time continuous data packet dropout, which means the communication network and the total NCS fail"* (Section 4, quoted verbatim). This repository's own audited behavior, today, is precisely that excluded case: `notes/FINAL_READINESS_AUDIT.md` confirms all three existing scripts (`publisher.py`, `aggregator.py`, `failure_detector.py`) crash with an unhandled exception — not graceful degradation, a hard failure — the moment Ditto becomes unreachable, with no retry. In the paper's own vocabulary, that is a "total NCS fail" event, exactly the case Theorem 1's stability guarantee was never meant to cover. The reliability work already planned for this semester (wrapping the Pi-5-side process in a restart loop, `FALL2026_ACTION_PLAN.md` Phase 3) isn't an attempt to extend Theorem 1 into that excluded region — it's ordinary engineering hardening — but it is honestly describable as sitting right at the boundary the paper draws around its own guarantee, and saying so is worth doing because it shows the paper was actually read and understood, not cited for its title.

### How it physically works, in plain terms
The Pi 5 is a computer, and computers get hot under load, and a hot chip can damage itself, so every computer has some way of watching its own temperature and reacting — usually a fan. Right now, that reaction happens *entirely inside* the Pi 5's own low-level firmware: it watches its temperature sensor and steps the fan speed up in fixed stages as it gets hotter (roughly: fan kicks on around 50°C, ramps up through the 60s, hits full speed by 75°C) [VERIFIED LIVE — raspberrypi.com/news/heating-and-cooling-raspberry-pi-5, cross-checked against a correction on that same page].

The experiment replaces that closed local loop with a longer one: the Pi 5 reports its real temperature up to your digital twin every few seconds; your twin (not the Pi 5's firmware) decides what fan speed makes sense; that decision gets sent back down to the Pi 5, which then has to have been told to stop listening to its own firmware and just do what it's told.

### How it maps onto your existing repo
This is the part that makes the Pi 5 attractive over the alternatives below: your existing `Cluster → Rack → Node` structure doesn't need to change conceptually — the Pi 5 just **becomes** one real `node_*` instead of a simulated one.

- `twin/publisher.py`'s `node_features()` function [FROM CODE AUDIT, lines 33-54] currently fabricates numbers with `random.uniform(...)`. For the Pi 5's node ID specifically, this becomes: read `/proc/stat` for CPU%, `/proc/meminfo` for memory%, and a new line reading `/sys/class/thermal/thermal_zone0/temp` for the real temperature. Everything else in that file — the publish loop, the pause-file mechanism, the retry-free PATCH call — stays exactly as it is.
- `twin/aggregator.py` needs **no changes at all** to include this node in rack/cluster averages — it already treats every node in `NODE_IDS` identically.
- The only genuinely new code is the "listen for a command" half, which (see cross-cutting point #1 above) doesn't exist in any form today.

### What it actually costs (re-checked twice — see correction note below)

| Item | Verified street price | Source |
|---|---|---|
| Raspberry Pi 5, 4GB RAM (bare board) | **$110.00** | [VERIFIED LIVE, 2026-09-14] Two independent authorized resellers agree exactly: PiShop.us ("In Stock") and CanaKit.com ("In Stock") |
| Official Active Cooler (heatsink + PWM fan) | **$10.95–$13.50** | [VERIFIED LIVE, 2026-09-14] PiShop.us $10.95 / Adafruit $13.50 |
| Official 27W USB-C power supply | **$12.95–$14.04** | [VERIFIED LIVE, 2026-09-14] PiShop.us $12.95 / Adafruit $14.04 |
| Official 32GB microSD card (blank) | **$19.95** | [VERIFIED LIVE, 2026-09-14] PiShop.us |
| **Total** | **≈ $154–158** | |

**Two corrections on top of a correction — this is worth reading in full, because it's exactly the kind of mistake this whole exercise exists to catch.** An earlier pass of this document reported the Active Cooler at $22.99 and a total of ~$115, both sourced from a search-engine summary rather than a direct page fetch. Loading two live reseller pages found that $22.99 was itself wrong (the real price is $10.95–$13.50) — bringing the total to ~$154–178, which the *next* pass explained as "$110 street price vs. a $70 list price," implying a reseller markup driven by scarcity.

**That explanation was also wrong, and re-checking it on 2026-09-15 found why: the Raspberry Pi Foundation raised its own official price for the 4GB board three separate times between December 2025 and April 2026 — $70 → $85 → $110 — each one a real, dated announcement on raspberrypi.com, not a reseller markup.** $70 was never stale or wrong when it was first checked; it was simply superseded twice more before the next check happened. **$110 is the Foundation's own current price today**, confirmed three independent ways (two resellers, the arithmetic across all three announcements, and a price-tracking forum thread with no change recorded since April 12, 2026). Budget and ask for $110 because it is the current official price, not because it's an inflated street price above a lower "real" one.

**Re-verified live again 2026-09-16/17, as part of the Part 1-2 demonstration checkpoint (see `notes/EXPERIMENTAL_TWIN_REQUIREMENTS.md`'s "Part 3 reconsideration" section for the full pass):** the board is still exactly $110.00 at CanaKit — but **PiShop, one of the two resellers this document has cited interchangeably throughout, now shows this exact SKU as "Out of stock"** (Notify Me only), the first time stock availability rather than price has been the volatile variable. The Active Cooler ($10.95) and 32GB card ($19.95) are unchanged at PiShop; the 27W supply dropped a dollar, to $11.95. Total if buying the board at CanaKit and the rest at PiShop: **≈$152.85**, still comfortably under the $200 ceiling. Re-check both price *and* stock immediately before actually ordering — this file's numbers are a snapshot, not a standing guarantee, same caveat as always.

Add an optional external desk fan (~$15–20 [ESTIMATE, not verified this session]) if you want an independent way to disturb the Pi 5's temperature from outside (blowing cool air at it) rather than only being able to raise its temperature (via workload) — this gives you a cleaner experiment because you can test both "make it hotter" and "make it cooler" disturbances.

**Total realistic budget: ~$154–178** (higher end includes the optional desk fan). This now sits at or slightly above the $150 "preferred" line from the constraints this whole exercise is built on, though still comfortably under the $200 hard ceiling — a materially different picture from this document's first draft, and the honest one.

### What's genuinely new vs. reused (be honest about this with your professor)
**Reused almost as-is:** the entire outbound pipeline (Ditto → Mosquitto → Telegraf → InfluxDB → Grafana), the aggregation logic, the publish loop, the Thing/Type composition tooling.
**Modified, not rewritten:** `node_features()` (swap fake numbers for real file reads); the failure detector's trigger condition (see below).
**Built from nothing:** the inbound command path (doesn't exist today in any form); the actual control algorithm (a PID controller or similar) that decides fan speed from temperature — nothing resembling a controller exists anywhere in this codebase today; a supervisor/retry wrapper so the physical device's script doesn't silently die on the first network hiccup (see cross-cutting point #2).

**One real design question this raises that your existing schema doesn't answer:** the PDF that inspired this all never addressed what happens to `active_connections` and `latency_ms` — two of the four features every node currently reports — once a node is a real computer instead of a random-number generator. A bare Pi 5 doesn't naturally have those. Worth deciding upfront: drop them for this node, fake them alongside the real temperature data, or replace them with something more physically meaningful.

**Also worth deciding upfront, and worth asking the professor directly:** the exact mechanism Eclipse Ditto (the platform this is built on) uses to send a live command to one specific device could not be fully pinned down this session — the general capability is real and documented, but the precise web address/format wasn't confirmed against a page this session could actually load. The safer, lower-risk path (and the one this document recommends) is to skip that machinery entirely and just have the cloud side write the "desired fan speed" as an ordinary piece of Thing data, which the Pi 5 then checks on its own regular polling cycle — reusing the exact same simple mechanism (`PATCH`/`GET` over plain HTTP) that already works reliably everywhere else in this codebase, instead of a fancier "live message" feature that hasn't been proven to work here.

### Failure detector: a required small fix
[FROM CODE AUDIT] The failure detector currently flags a node as "failed" if its reported values stop changing for too long. A real Pi 5 sitting at a stable temperature (which is completely normal — that's what "reached equilibrium" looks like) will trigger constant false alarms under the current logic. This needs a small, well-scoped fix: allow "stayed within a small tolerance band" to count as "still healthy," rather than requiring the number to be bit-for-bit identical to flag it as stuck. This is a contained change to one comparison in one file, not a rewrite.

### Risks, stated plainly
- The single biggest unknown is the exact command-delivery mechanism (above) — budget time to prototype this early, not late.
- Current memory-price volatility (cross-cutting point #4) means the ~$115 figure could drift up before you order.
- This repo's own history shows unattended long-running processes have broken before (the dashboard bug, the port-forward deaths) — a semester-long physical deployment should assume it will need occasional unattended-babysitting via a restart loop, not "set it and forget it."

### Rough timeline
No specific hours-spent figure exists in this repo's own records to build an estimate from [could not confirm from files]. What *is* on record: the existing pure-software PoC took about one month of iterative, audited work (2026-07-26 to 2026-08-25, per `git log`) to reach its current polished state, and much of that month went to fixing infrastructure problems, not building new features. Given the command-and-control half of this idea is being built from zero, budgeting **several weeks of focused part-time work**, not days, is the realistic planning assumption — noticeably longer than an earlier draft's "four hours to first telemetry" estimate, which only covered the easy (read-only) half of the work.

---

## Option B — ESP32 + resistive heater thermal benchtop rig

### The one-sentence pitch
Build a small, standalone thermal experiment from cheap parts — a heating element, a temperature sensor, a fan, and a $10 microcontroller — that's easier and faster to build than Option A, but doesn't reuse your existing Cluster/Rack/Node code at all.

### The research question
*How does network delay between a sensor/actuator and its cloud-based controller affect the stability of a closed-loop temperature-control system — and at what delay does the control loop start oscillating instead of settling?*

This is actually a cleaner, more classic control-theory experiment than Option A, because you can deliberately dial in extra network delay and watch exactly when things go unstable — a well-defined, textbook-style research question.

### How it physically works, in plain terms
A small resistor gets warm when current flows through it (the same principle as a toaster or a space heater, just much smaller and lower-power). A digital temperature sensor sits near it. A tiny $10 computer (the ESP32) reads the sensor and can turn the heater on and off very quickly (many times per second) to control the average amount of heat being produced — the same trick a dimmer switch uses for a lightbulb. A small fan provides cooling. Your cloud twin watches the temperature and tells the ESP32 how much to heat.

### Verified technical specs (checked against the actual datasheet, not the earlier document's questionable citations)
- **DS18B20 temperature sensor:** [VERIFIED LIVE — Analog Devices official datasheet, analog.com] accuracy ±0.5°C, and — this part is a real limitation, confirmed directly from the manufacturer's own numbers, not a third-party blog — at its most precise setting it physically takes **750 milliseconds** to complete one reading. That's a hard floor on how fast this sensor can report new data, regardless of how fast your code polls it.
- **ESP32 board:** [ESTIMATE, cross-checked across several current retail listings] roughly **$8–12** for a standard development board.

### What it actually costs
| Item | Price | Source |
|---|---|---|
| ESP32 dev board | ~$10 | [ESTIMATE] |
| DS18B20 sensor | ~$3–5 | [ESTIMATE — commodity part, not individually verified against one retailer] |
| Small heating resistor + small DC fan + MOSFET driver + wiring/breadboard/enclosure | ~$15–25 | [ESTIMATE] |
| **Total** | **≈ $30–40** | |

This is genuinely the cheapest option by a wide margin — real headroom under even the PDF's stress-tested "$50 emergency budget" scenario.

### What's genuinely new vs. reused
**Nothing from `twin/publisher.py`, `twin/aggregator.py`, or the existing Ditto Thing schema carries over in any meaningful way.** A heating resistor's temperature has no natural relationship to "CPU utilization" or "memory usage" — building this means designing a new, parallel Thing type from scratch, not extending the existing Cluster/Rack/Node one. This isn't a flaw in the idea, it's just an honest description of the tradeoff: faster and cheaper to build, in exchange for **not** demonstrating you can extend the existing compositional architecture — which, if part of the point of this research is showing continuity with the software already built, is a real cost worth weighing.

### One safety point worth clarifying before building anything
Heating elements can be controlled two different ways: switching a low-voltage DC resistor on and off very fast (safe, simple, what's assumed above), or "phase-angle" dimming of an AC mains circuit (the technique used in household lamp dimmers — this is mains-voltage work). The two are easy to conflate in casual descriptions. Whatever gets built here should explicitly use the low-voltage DC approach — this is both the safer and the far more common approach for a small ESP32 rig, and it's the one consistent with the "no mains-voltage modifications" safety rule any version of this project should follow regardless of which hardware option gets picked.

### Risks, stated plainly
- The 750ms sensor floor (verified above) means you cannot poll faster than roughly once per second no matter what — this caps how "high-frequency" the control loop can meaningfully be, which matters if the research question is specifically about fast dynamics.
- An exposed heating element on a desk is a real, if minor, burn/fire consideration that needs a simple enclosure — not a blocker, just something to plan for, not skip.
- Zero code reuse from the existing repo means this is closer to "a second, separate project" than "an extension" — worth being upfront about that framing with your professor.

### Rough timeline
Faster to first result than Option A — no existing codebase to integrate with means no risk of breaking something that already works, but also no head start from it. **[ESTIMATE, not derived from any documented figure]:** a working local (non-cloud) version could plausibly happen in days; wiring it into the actual Ditto twin realistically adds one to two weeks on top, similar in kind to the "new code" portion of Option A even though the physical build itself is faster.

---

## Option C — Raspberry Pi Zero 2 W micro-cluster (3 nodes)

### The one-sentence pitch
Build three tiny, cheap real computers and wire them up to literally *be* your Cluster/Rack/Node hierarchy in physical form — the most structurally honest match to your existing architecture, but the weakest on the "meaningful physical actuation" requirement.

### The research question
*Can a cloud-based scheduler use aggregated thermal data from a real, physically distributed cluster to move workload away from the hottest machine and toward cooler ones, and does this measurably reduce the cluster's peak temperature compared to ignoring temperature entirely?*

### How it physically works, in plain terms
Three small, real, independent computers (each about the size of a stick of gum) each run their own copy of your publisher script, each reporting real CPU and temperature data. Your cloud twin looks at all three at once and can decide to send more or less simulated work to each one, based on which is running hottest.

### Verified technical specs
[VERIFIED LIVE — raspberrypi.com official product page, fetched directly] Official price: **$15** per board. Specs: quad-core processor, 512MB RAM. Directly confirmed by reading the manufacturer's own product page: **there is no PWM fan header, no mention of any official active-cooling accessory, and no built-in thermal-management story of any kind for this board** — unlike the Pi 5, which has a dedicated 4-pin fan header and an official cooler product built for it. This independently confirms what the earlier document claimed about this board, but now from a first-party source rather than taking it on faith.

### What it actually costs — and a genuinely useful, slightly counter-intuitive finding
| Item | Price (×3) | Source |
|---|---|---|
| Pi Zero 2 W boards | 3 × $17.25 = **$51.75** | [VERIFIED LIVE, reconfirmed 2026-09-15] PiShop.us, "In Stock" — unlike the Pi 5, the $15 Foundation price here is a deliberately-held figure ("planning to keep Zero 2W at $15, though we have pretty thin margins," raspberrypi.com), not one superseded by further official increases. The $17.25 street price is ordinary reseller margin on a thin-margin board, not the same LPDDR4 shortage dynamic as the Pi 5. |
| microSD cards | 3 × $19.95 = **$59.85** | [VERIFIED LIVE, using the same official card checked in Option A] A smaller/generic card would likely be cheaper here, but that wasn't verified this session — treat this as the safe upper bound, not the floor |
| Power supplies (lower power than Pi 5 — standard 5V, not the 27W unit) | 3 × ~$8–12 = **~$24–36** | [ESTIMATE, not verified this session] |
| Custom fan + MOSFET wiring per node (since there's no off-the-shelf cooler for this board) | 3 × ~$5–10 = **~$15–30** | [ESTIMATE, not verified this session] |
| **Total** | **≈ $151–178** | |

**This is the finding worth pointing out to your professor, and it got stronger after re-checking real prices instead of announced ones:** three $15-and-change boards sound like the "budget option," but once every peripheral (SD card, power supply, custom cooling) is multiplied by three, the total lands in **the same $150–180 band as the single Pi 5 in Option A** — right down to overlapping ranges. "Cheap per unit" and "cheap overall" are not the same thing once a design has three of everything, and this held up even after the underlying prices were corrected upward across the board.

### What's genuinely new vs. reused
This is the *best* structural fit to the existing schema — literally three physical Node-shaped objects under two Rack-shaped groupings — but the weakest on actual bidirectional physical control, because (as verified above) there's no standard way to actuate anything on this board. Building real actuation means soldering your own fan-driver circuit onto each of three separate boards by hand, which is real, error-prone physical work, not a software change.

### Risks, stated plainly
- Highest hands-on-hardware risk of the three physical options: hand-soldering three separate custom circuits has a real chance of a wiring mistake damaging a board.
- Coordinating three independent Wi-Fi devices talking to one Minikube cluster on a Windows laptop adds real networking complexity on top of the port-forward fragility this repo has already struggled with once (cross-cutting point #2) — now times three.
- Without solving the actuation problem, this option risks landing as "three real thermometers" rather than a genuine bidirectional twin — worth being honest that this is the hardest of the three to get all the way to the closed-loop bar your project is aiming for.

### Rough timeline
**[ESTIMATE, not derived from any documented figure]:** getting three boards reporting real telemetry is comparably fast to Option A's read-only half (days, since it's the same kind of file-reading code, just ×3). Getting real, working, hand-wired actuation on all three reliably is likely the *slowest* path of the three physical options, specifically because of the hand-soldering and per-unit wiring risk — plan for this to be the long pole if this option is chosen.

---

## Option D — Harden and extend the *existing* software twin (no new hardware at all)

### The one-sentence pitch
Instead of (or alongside) buying anything, spend real research effort fixing and extending what's already built — this repo has documented, real weaknesses that are themselves legitimate things to study and fix, with zero hardware-acquisition risk and zero budget.

### The research question
*Several, any of which is a real, scoped research task on its own:*
- Can the failure detector be redesigned around statistical variance/tolerance instead of exact-value-equality, and does that meaningfully reduce false positives without missing real failures?
- Can the pipeline be made resilient to the exact kind of outages already observed (Ditto disconnects, port-forward deaths) — i.e., turn a documented weakness into a documented, tested fix?
- Can a *simulated* bidirectional control loop (a fake "actuator" whose synthetic physical state responds to synthetic commands) be built and proven correct entirely in software, as a rehearsal for real hardware — de-risking Option A's hardest, least-verified part (the command-delivery path) before spending any money?

### Why this belongs on the menu
This isn't a consolation-prize option. Every single repo-specific weakness found during this session's audit — the unhandled crashes, the naive failure detector, the dashboard's cardinality bug, the never-built command-and-control path — is *itself* a legitimate, scoped piece of systems research, and fixing any of them produces something real and demonstrable. It's also the only option with **no cost, no shipping time, and no hardware-acquisition risk at all**, which matters given the memory-price volatility flagged above.

### What's genuinely new vs. reused
Entirely additive to what exists. No new dependencies, no new physical setup, no procurement wait. The tradeoff, stated plainly: this doesn't get you a physical Thing, and if the mandate specifically requires crossing into real cyber-physical territory this semester (worth confirming directly, since it's the professor's call), this option alone wouldn't satisfy that on its own — though it pairs naturally *with* any of A/B/C as the de-risking step that happens first.

### Risks, stated plainly
- Doesn't, by itself, produce a physical Thing — if that's a hard requirement for the semester, this needs to be paired with A, B, or C, not stand alone.
- Because there's no hardware to demo, it may read as "less impressive" in a live meeting than something you can physically point at and watch react — worth considering how it'll be presented, not just how it'll be built.

### Rough timeline
**[ESTIMATE]:** each of the three research questions above is independently scoped at roughly a week or two of focused work, and could be sequenced (do the simulated control loop first, as direct prep for Option A) or run in parallel with hardware procurement so no time is lost waiting for a box to ship.

---

## Side-by-side comparison

| | **A: Pi 5** | **B: ESP32 rig** | **C: Pi Zero cluster** | **D: Software-only** |
|---|---|---|---|---|
| Verified cost | ~$154–178 | ~$30–40 | ~$151–178 | $0 |
| Reuses existing code? | Mostly | Almost none | Structurally, yes; functionally, little | Entirely |
| Matches Cluster/Rack/Node shape? | As one real node | No — different domain | As 3 real nodes (best fit) | N/A |
| Real bidirectional actuation? | Yes, well-supported (PWM fan) | Yes, well-supported (heater) | Weak — no standard actuation path | N/A (simulated only) |
| Biggest open risk | Exact command-delivery mechanism unverified | Zero architectural reuse | Hand-soldered per-node wiring | Doesn't produce physical hardware |
| Hardware acquisition risk | Low, but current memory prices are volatile | Very low | Low | None |

---

## What I'd actually say to the professor

All four are genuinely feasible under the stated constraints (budget, safety, semester timeline, no proprietary lock-in) — this isn't a "pick the obvious winner" situation, it's a real tradeoff to discuss:

- **A (Pi 5)** is the best balance of code reuse and genuine bidirectional control, and is this document's default recommendation — but go in aware that the command-delivery mechanism needs to be prototyped early, since it's the one piece that couldn't be fully verified this session.
- **B (ESP32)** is the fastest and cheapest path to *a* working result, at the honest cost of not extending your existing architecture at all — a reasonable pick if time or budget risk (see the memory-price note) turns out to matter more than continuity with the existing codebase.
- **C (Pi Zero cluster)** looks the most architecturally elegant on paper but carries the most hands-on hardware risk and the weakest actual actuation story — worth discussing honestly rather than defaulting to it just because "three nodes" sounds like a better match.
- **D (software-only)** is a legitimate real option on its own, and a smart de-risking first step no matter which physical option (if any) gets chosen next.

---

*Every dollar figure, spec, and technical claim above is tagged with where it came from. Nothing here was carried forward from the original PDF without being re-checked — see `notes/HARDWARE_SELECTION_AUDIT.md` for the full record of what that document got wrong and how each correction was verified.*
