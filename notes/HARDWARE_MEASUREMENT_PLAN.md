> **Superseded on 2026-10-11.** This document predates the 2026-10-01 call and assumed a Raspberry Pi 5 (not yet bought) sending its PATCHes straight to Ditto through the laptop's port-forward.
> The board in use is now a borrowed Raspberry Pi 3 B+, the architecture is Pi -> laptop receiver -> Ditto, and the current plan is `notes/PI_TELEMETRY_SPEC_2026-10-11.md`.
> The text below is kept unchanged for history.

# Hardware Measurement Plan — the Two Open Carry-Forward Questions

**Written:** 2026-09-17, before any hardware exists, specifically so no design work is needed the moment it arrives. Both scripts referenced below are already written and committed (`notes/pi_rtt_measurement.py`, `notes/pi_idle_jitter_measurement.py`) — this document says how to run them and what result means what. Nothing here has been executed; there is no real Pi 5 yet.

This covers exactly the two items `notes/EXPERIMENTAL_TWIN_REQUIREMENTS.md`'s "Part 3 reconsideration" section flagged as genuinely open, not answered:

(a) whether the 3s sampling interval holds under real Wi-Fi/OS jitter instead of local synthetic RTT, and
(b) whether the ±0.5 tolerance band survives real sensor measurement noise instead of zero-noise synthetic data.

---

## (a) Sampling interval under real network conditions

**What to measure:** real wall-clock round-trip time of the exact `PATCH /api/2/things/{id}/features` call `publisher.py` makes in production, run from the Pi 5 itself over its real Wi-Fi link — not from the laptop, not over `localhost`. That distinction is the entire point: the existing `notes/scaling_results.json` numbers (137.7 ms–615.5 ms at n=1) measure the laptop talking to itself through a port-forward, with zero real network hop and zero real OS scheduling contention from anything Wi-Fi-related.

**Tool:** [`notes/pi_rtt_measurement.py`](pi_rtt_measurement.py), already written. Reuses `twin/ditto_client.py`'s exact `ditto()` PATCH helper and the exact `ditto_rtt_s` definition (`time.perf_counter()` immediately around the call) that `notes/scaling_benchmark.py` already uses, so the two result sets are directly comparable without unit conversion or re-derivation.

**How to run it, the moment the Pi is on the network and can reach the laptop's port-forward:**

```bash
# On the Pi, with this repo cloned and the same venv/requirements as the laptop:
export DITTO_HTTP_URL=http://<laptop-LAN-IP>:8081
python3 notes/pi_rtt_measurement.py --duration 1800 --interval 3
```

`--duration 1800` (30 minutes) is the minimum session length — long enough to plausibly span normal Wi-Fi variation (other devices joining/leaving, a neighbor's microwave, a background OS update on the Pi itself), not just a lucky quiet window. If the first run's `p95` is close to a threshold below, re-run for a full session-length (whatever a real experiment run is expected to last) before trusting the number.

**Concrete thresholds — not a judgment call:**

| Result | Meaning | Action |
|---|---|---|
| `p95_s` ≤ 1.5s **and** `max_s` ≤ 3.0s | The 3s interval has comfortable headroom (half the budget at the 95th percentile, and no single sample ever exceeded the full interval). | **3s holds. No change.** |
| `p95_s` > 1.5s **or** `max_s` > 3.0s | Either typical latency already eats half the budget, or at least one real PATCH took longer than the time until the next one is due — a real overlap risk. | **Interval needs to grow.** Set the new interval to `ceil(p95_s * 2)` seconds as a first candidate, then re-run this same script at that candidate interval and confirm its own `p95` again clears half of *that* new budget before adopting it. |
| `stdev_s` more than 3× the synthetic `sensor_count n=1` runs' spread (`notes/scaling_comparison.md` / `notes/scaling_results.json`) | Even if the mean looks fine, real Wi-Fi is qualitatively less predictable than the synthetic baseline — "jitter," not just "latency." | **Flag explicitly, even if the p95/max checks above pass.** A high-variance-but-low-average channel can still occasionally blow through a 3s budget in a way a single 30-minute average won't reliably catch; note this in the eventual writeup rather than silently averaging it away, the same discipline `scaling_comparison.md` already applies to the synthetic numbers. |

---

## (b) Tolerance band under real sensor noise

**What to measure:** real read-to-read jitter in CPU%, memory%, and CPU temperature, sampled at the Pi's actual resting state — no `stress-ng`, no deliberate load change. The question is specifically about *noise at equilibrium*, which is the exact condition the original tolerance-band fix (`twin/failure_detector.py`'s `_within_tolerance()`) was designed around but never tested against, because synthetic `random.uniform()` draws have no concept of "at rest" at all.

**Tool:** [`notes/pi_idle_jitter_measurement.py`](pi_idle_jitter_measurement.py), already written, Linux-only (reads `/proc/stat`, `/proc/meminfo`, `/sys/class/thermal/thermal_zone0/temp` — the exact three sources `notes/FALL2026_ACTION_PLAN.md` Phase 2 already named as the ones `node_features()` will read for a real Pi 5 node).

**How to run it, the moment the Pi is reachable:**

```bash
# On the Pi itself, genuinely idle -- nothing else deliberately loading it:
python3 notes/pi_idle_jitter_measurement.py --duration 1800 --interval 3
```

This does not just report raw jitter numbers — it feeds the captured sequence through the **real, imported** `twin.failure_detector._within_tolerance()` and `UNCHANGED_TOLERANCE`, not a reimplementation, so the answer is about the actual deployed code, not a lookalike calculation.

**Concrete thresholds:**

| Result | Meaning | Action |
|---|---|---|
| Consecutive-pair `p95` deltas for `cpu` and `mem` ≤ 0.5, and ≥ 95% of all consecutive real pairs stay within ±0.5 | Real idle jitter fits inside the existing band for at least 19 out of 20 real sample pairs. | **±0.5 holds. No change.** |
| More than 5% of consecutive real, no-load pairs exceed ±0.5 | The existing band is measurably too tight for real hardware. | **Widen it, using this exact run's own numbers**, not a guess: set the new tolerance to that run's observed `p99` consecutive-pair delta, rounded up to one decimal place — a measured replacement for a measured problem, consistent with how `UNCHANGED_TOLERANCE` was always framed as "chosen, not measured" pending exactly this data. |

**A third, structural result this script also checks, found by reasoning through the code while writing this plan — not yet observed, because no real hardware exists to observe it on:**

Reading `twin/failure_detector.py`'s actual loop closely: `last_seen[node_id]` is *only* ever updated inside the branch where `_within_tolerance()` returns `False` (i.e., "this looks like a genuinely new real value"). If a real signal stays within tolerance of the very first sample recorded for it *indefinitely* — which is exactly what "a Pi 5 sitting at true thermal/load equilibrium" means — `last_seen` never advances again, `age` grows without bound, and the node **will** be marked `FAILED` once `age` exceeds `FAILURE_TOLERANCE_SEC` (9s), even though it is healthy and genuinely still being polled successfully every cycle. This isn't about the tolerance band being the wrong *width* — no width fixes it, because the mechanism conflates "the value hasn't changed" with "no update arrived," and a sufficiently stable real device will eventually satisfy the first without ever violating the second. It's dormant on synthetic noise nodes for the same reason the original exact-equality bug was dormant: four independent `random.uniform()` draws landing within 0.5 of each other, repeatedly, is vanishingly unlikely by chance — which is exactly why a real Pi 5 (the first genuinely-capable-of-sustained-stability signal this repo will have) is what would actually expose it.

`pi_idle_jitter_measurement.py`'s `check_structural_gap()` tests this directly against the real code: it replays the captured idle sequence through `_within_tolerance()` exactly as `failure_detector.py`'s own loop would, and reports `would_spuriously_fail_a_healthy_node`.

| Result | Meaning | Action |
|---|---|---|
| `would_spuriously_fail_a_healthy_node: false` | Either real idle jitter is high enough to naturally reset `last_seen` often (crossing back out of tolerance before 9s elapses), or this reasoning doesn't hold up against real data. | No structural change needed — but re-check this specific field even if the width-only checks above pass, since a "wide enough band" and "resets often enough" aren't automatically the same thing. |
| `would_spuriously_fail_a_healthy_node: true` | Confirms the structural gap: a genuinely healthy, stable Pi 5 gets marked `FAILED` by design, regardless of tolerance width. | **This is a design decision, not a number to tune — bring it back to Harrison before changing anything.** The mechanical fix (sketched here, not implemented) would be tracking "a poll succeeded at all" separately from "the value changed enough to update `last_real`," and gating `FAILED` on the former — but that's a real change to `failure_detector.py`'s structure, which is exactly the kind of predictor/detector iteration this repo's own history has repeatedly said should wait for an explicit go-ahead rather than being folded silently into a measurement task. |

---

## Definition of done for this plan (met now, before hardware exists)

- Exact tool named for each question: yes, both scripts exist and are committed.
- Exact command to run the moment hardware is reachable: yes, both above.
- Concrete, numeric pass/fail thresholds, not vague judgment calls: yes, for both (a) and (b), plus the structural check for (b).
- No additional design work needed at execution time: yes — the only per-run input needed is the Pi's LAN IP situation (for (a)) and confirming the Pi is genuinely idle (for (b)), both operational, not design, decisions.
