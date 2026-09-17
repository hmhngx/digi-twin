# Fall 2026 Action Plan — Physical Twin Extension

**Prepared:** 2026-09-14
**Status:** Pre-funding. Nothing here has been purchased or built yet.
**Builds on:** `notes/HARDWARE_SELECTION_AUDIT.md` (fact-check of the original hardware PDF) and `notes/FALL2026_RESEARCH_PROPOSAL_OPTIONS.md` (the four-option menu, with a pricing correction made 2026-09-14 — see that file's Option A section for what changed and why).

---

## The decision

**Primary track: Option A — Raspberry Pi 5 thermal control loop.**
**Parallel track, starting immediately, zero cost: Option D — harden the existing software and simulate the control loop before real hardware arrives.**
**Fallback if funding or timeline gets tight: Option B — ESP32 rig (~$30–40, no approval-wait risk).**

This isn't a guess — it's what's left standing after re-verifying every load-bearing claim in the original hardware analysis against this repo's actual code and against live, current sources, and then re-checking that verification's own numbers a second time (a real pricing error was caught and fixed in that second pass — see below). Full evidence trail is in the two files linked above.

---

## Phase 0 — This week, before the funding meeting (no money spent)

1. **Bring `notes/FALL2026_RESEARCH_PROPOSAL_OPTIONS.md` and the funding artifact** (see companion deliverable) to the meeting — don't re-explain verbally what's already written down and sourced.
2. **Decide, before the meeting, how to present the $110 current price for the Pi 5 board** (see the correction note in the proposal file — $110 is the Foundation's own current price after two increases past the $70 figure some earlier materials may reference, not a reseller markup) — the honest number to ask for is $110, and it's worth deciding now whether to ask for that directly or ask for the older $70 figure and risk a second, smaller request later if the department balks at $110 without warning. This plan recommends asking for the real number once, not the stale one twice.
3. **Start the Option D work now** — it costs nothing, needs no approval, and directly de-risks the hardest unverified part of Option A (see Phase 3 below). Concretely, this week:
   - Read `twin/failure_detector.py:79-101` and change the `FAILED` trigger so a value that stays within a small tolerance band no longer counts as "unchanged" — this is a contained, few-line fix, not a rewrite, and it's needed regardless of which physical option is ultimately funded.
   - Sketch (on paper or in a design doc, not necessarily code yet) the "plain feature PATCH" command-delivery design described in Phase 3 — having this ready to explain in the meeting turns "there's one unverified technical risk" into "here's exactly how we're de-risking it," which is a stronger position to fund from.

---

## Phase 1 — Immediately after approval (target: ≤1 week)

**Order, in one batch, from a single reseller to simplify shipping/tracking** (this plan used PiShop.us and CanaKit.com prices interchangeably during verification; either is a legitimate authorized reseller — pick whichever has everything in stock at time of order, confirmed fresh, since stock status changes):

- Raspberry Pi 5, 4GB RAM (bare board)
- Official Active Cooler
- Official 27W USB-C power supply
- 32GB microSD card (an official-branded card was the one priced and verified here; a reputable generic A2/V30-class card would likely be cheaper and is worth a five-minute check before ordering, but wasn't verified this session — don't substitute an unverified card without confirming it meets the Pi 5's speed class recommendation)
- *Optional:* one external desk fan, if the budget stretches to it, to enable independent cooling-side disturbance testing (not just heating-side)

**Do not order before this list is confirmed against live prices one more time** — this plan's numbers are from 2026-09-14; if the meeting happens more than a week or two later, re-check prices rather than assuming they held, given the documented volatility.

---

## Phase 2 — Real telemetry, no actuation yet (target: 1–3 days of actual work once hardware arrives)

Goal: get the Pi 5 reporting *real* CPU/memory/temperature as one genuine node, with zero risk to anything already working.

1. Flash Raspberry Pi OS, confirm SSH access, confirm `/proc/stat`, `/proc/meminfo`, and `/sys/class/thermal/thermal_zone0/temp` are readable.
2. In `twin/publisher.py`, extend `node_features()` (currently lines 33-54) with a branch for the Pi 5's specific node ID that reads those three sources instead of calling `random.uniform(...)`, and adds one new feature, `cpu_temperature_c`.
3. Everything else in `publisher.py` — the loop, the pause-file check, the PATCH call — needs **no changes**. This is deliberately the lowest-risk phase: if anything goes wrong, the other nine synthetic nodes are completely unaffected, because each node's `node_features()` call is independent.
4. Add the new `cpu_temperature_c` (and, once Phase 3 exists, `fan_speed_pwm`) feature to the Ditto Thing schema via `scripts/setup_types_and_twins.py`'s existing, already-idempotent Extended API calls.
5. **Gate to pass before moving on:** the Pi 5's real temperature should be visible in Grafana, fluctuating with real load (e.g. running `stress-ng` on it by hand), for at least a few hours without the pipeline falling over.

---

## Phase 3 — Real actuation, the genuinely new part (target: the bulk of the semester's engineering time)

This is where the one real unverified assumption lives, and where Option D's advance work pays off.

1. **Command delivery — the specific, deliberate design choice:** rather than chasing Eclipse Ditto's "live message" machinery (whose exact HTTP mechanics could not be confirmed against a working page during verification — see `notes/HARDWARE_SELECTION_AUDIT.md` Part 2), have the cloud-side control logic write the desired fan speed as an ordinary Thing feature (e.g. `fan_speed_pwm_setpoint`), using the *exact same* `PATCH`/`GET` pattern every other part of this codebase already uses successfully. A new small script on the Pi 5 polls that feature on the same cadence as the publisher and writes it to the fan's PWM duty-cycle file. This is a simplification made specifically to avoid depending on the one mechanism this project couldn't independently verify.
2. **Sever the firmware's own fan control** so it stops fighting the new code for control of the same fan (a documented, if informally-sourced, community technique — verify it works on the specific OS image in use before relying on it for the real experiment).
3. **Write the actual controller** — start with something simple (a basic proportional controller) before attempting a full PID or anything more elaborate; nothing resembling a controller exists anywhere in this codebase today, so this is pure new work, not an adaptation of anything existing.
4. **Wrap the Pi-5-side script in a restart loop** (`while true; do <script>; sleep 2; done` or equivalent) from day one — this repo's own operational history shows every long-running script here has crashed unhandled on a transient network hiccup at least once; don't wait to discover that live during a demo.
5. **Gate to pass before moving on:** the controller holds a temperature setpoint under a repeatable synthetic load, and can be shown recovering after a deliberate disturbance (extra load, or the optional desk fan).

---

## Phase 4 — The actual experiment and writeup (target: final 2–3 weeks)

1. Run the comparison this whole project is meant to produce: the new cloud-based controller vs. the Pi 5's own stock firmware heuristic, on the same repeatable workload (e.g. alternating `stress-ng` bursts and idle periods).
2. Report the honest result — including if the cloud controller *doesn't* beat the firmware under some conditions (e.g. very fast disturbances, where network round-trip lag should be expected to hurt). A well-characterized negative or mixed result is a legitimate finding, not a failure of the project.
3. Update `notes/` with a dated, evidence-backed writeup in the same style as this repo's existing audit documents — the professor and any future student picking this up will read the notes, not just watch a demo.

---

## Fallback / decision gates

| If this happens... | ...then do this |
|---|---|
| Funding isn't approved at the $110 current-price level | Fall back to Option B (ESP32 rig, ~$30–40) rather than under-buying the Pi 5 track and getting stuck mid-semester waiting for a second approval |
| The command-delivery design in Phase 3 step 1 turns out not to work even in the simplified form | This is the point to loop back to the professor — it would mean the actuation half needs real troubleshooting time, which affects the semester timeline and is worth surfacing early, not near the deadline |
| Hardware is significantly delayed (shipping, stock) | Option D work continues in parallel regardless — the simulated control loop and the failure-detector fix don't depend on hardware being in hand |
| The Pi 5's real temperature turns out to be too stable/slow-changing to produce an interesting 3-second-resolution dataset | This was explicitly checked as plausible-but-not-a-blocker during verification (thermal time constants in the 100–300 second range against a 3-second poll) — if it's a problem in practice, shortening the polling interval is a config change, not a redesign |

---

## What "done" looks like this semester

A real Raspberry Pi 5, reporting real temperature and load data into the existing digital twin, whose fan is being controlled by a cloud-computed decision instead of its own firmware — with an honest, data-backed comparison of which one does a better job, presented alongside (not instead of) a clear account of what was simplified, what remains unverified, and why each of those calls was made. That last part — the discipline of saying what *isn't* proven yet — is itself consistent with the evidence-first standard the rest of this repo's audit trail already holds itself to, and is worth explicitly carrying into this phase rather than treating it as done once the hardware shows up.

---

*This plan intentionally does not repeat the full evidence behind each claim — see `notes/HARDWARE_SELECTION_AUDIT.md` and `notes/FALL2026_RESEARCH_PROPOSAL_OPTIONS.md` for sources. Where this plan states a fact, one of those two files has already cited it.*
