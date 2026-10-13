# Pi telemetry sync: dry run (2026-10-12) and live run (2026-10-13)

Written 2026-10-13. Raw logs: `notes/_pi_runs/receiver_20261012T222821Z.jsonl` (dry run) and `notes/_pi_runs/receiver_20261013T044037Z.jsonl` (live run); git-ignored, not modified. Specification: `notes/PI_TELEMETRY_SPEC_2026-10-11.md`. Analyzer: `scripts/analyze_pi_run.py`.

Source tags used for every number:

- **[analyzer]** printed by `scripts/analyze_pi_run.py` on the raw log.
- **[recomputed]** recomputed from the raw log on 2026-10-13 by a throwaway script that imports the analyzer's own functions (loader, probe pairing, percentile). Percentiles are nearest-rank, as in the analyzer. The script is not kept in the repo.
- **[user-reported, not on disk]** told to me by the person who ran the experiment; no file in the repo or in the run logs backs it.

## 1. Scope

Two runs of the Pi → laptop receiver → Ditto sync: a dry run (receiver started with `--dry-run`, so nothing was sent to Ditto) and a live run (receiver patching the real Ditto Thing `pi_0`). The wider study is about the fidelity of the digital twin itself. This note covers only what the sync's logs can show: delivery, sequence health, timing, and clock behaviour. **Nothing here measures accuracy against an independent reference.**

## 2. Setup

| Item | Value |
|---|---|
| Sender | Raspberry Pi 3 Model B Plus Rev 1.3, 64-bit Raspberry Pi OS (Debian 13), Python 3.13.5, console-only boot (`multi-user.target`); `pi/pi_sender.py`, standard library only. Board facts from the 2026-10-11 capture (spec §2) |
| Receiver | `twin/pi_receiver.py` on the laptop (Windows) |
| Network | Pi and laptop both on a phone hotspot over Wi-Fi (variable latency) [user-reported, not on disk] |
| Receiver → Ditto | through a `kubectl port-forward` to Minikube, so the Ditto acknowledgement time includes that hop [user-reported, not on disk] |
| Dry run | start record `dry_run: true`; first sample received 2026-10-12 22:29:54Z (18:29 local), last 22:49:27Z (18:49 local) [recomputed] |
| Live run | start record `dry_run: false`; first sample received 2026-10-13 04:41:51Z (00:41 local), last 05:24:30Z (01:24 local) [recomputed] |
| Sender settings | Defaults, as far as the logs show: 3 s sample interval (CPU stamps 3000 ms apart), a burst of 5 probes at start, then one about every 60 s (probe times). The 5 s request timeout is the default and is not recorded in the logs |

Local times are UTC−4 (the offset that matches the user-stated local times for both runs).

## 3. Results

### 3.1 Delivery and clock

| | Dry run | Live run |
|---|---|---|
| Samples logged / sessions [analyzer] | 384 / 1 | 854 / 1 |
| Sequence numbers [recomputed] | 0–389, 61–66 absent | 0–853, contiguous |
| Duration [analyzer] | 1172.949 s | 2558.81 s (42.6 min) |
| applied / duplicate / stale / stale_session [analyzer] | 384 / 0 / 0 / 0 | 854 / 0 / 0 / 0 |
| missed (sequence gaps) [analyzer] | 6 | 0 |
| Ditto acknowledgements [analyzer] | not applicable (dry run) | ok 854, failed 0 |
| Messages with `ntp_synchronized` = true [analyzer] | 384 of 384 | 854 of 854 |
| Longest gap between messages / silence episodes over 9 s [analyzer] | 26.619 s / 1 | 3.928 s / 0 |
| Messages carrying all four measurements [analyzer] | 0.9974 | 0.9988 |
| Clock probes valid [analyzer] | 23 | 47 |
| Probe round trip `delay`, min / p50 / p95 / max [analyzer] | 6.0 / 7.0 / 9.0 / 10.0 ms | 5.0 / 8.0 / 14.0 / 19.0 ms |
| Offset (receiver clock minus Pi clock), min to max [analyzer] | +11.0 to +50.5 ms | −74.0 to −9.5 ms |
| Drift, indicative [analyzer] | +35.3 ppm | +28.3 ppm |

The first message of a session never carries CPU (no baseline), which is why the complete-bundle fraction is 853/854 in the live run. The dry run's 0.9974 is 383/384.

Which clock causes the 28–35 ppm drift is not known (§6).

### 3.2 Live run: latency

All in ms [analyzer] unless marked.

| Stage | n | min | p50 | p95 | p99 | max |
|---|---|---|---|---|---|---|
| Age at send (Pi clock only) | 3415 | 0.0 | 1.0 | 2.0 | 331.0 | 349.0 |
| "Network, offset-corrected" (see §4) | 854 | 3.5 | 15.5 | 28.0 | 96.5 | 1022.5 |
| Receiver → Ditto acknowledgement | 854 | 82.0 | 110.0 | 137.0 | 198.0 | 251.0 |
| **End to end, as the analyzer prints it** | 3415 | 95.5 | 128.0 | **401.5** | **467.5** | 1141.5 |
| End to end, excluding the 42 artifact messages [recomputed] | 3248 | 95.5 | 127.0 | 168.5 | 231.5 | 1141.5 |

**The analyzer's end-to-end p95 and p99 (401.5 and 467.5 ms) are inflated by a sender defect (§7.1), not by the Pi's sensors or the network.** In 42 of 854 messages (4.9 %) the sender ran `timedatectl` between reading the measurements and stamping `sent_at`, so those messages were sent 258–349 ms after their measurements were read; the other 812 were sent 1–2 ms after. Method [recomputed]: an artifact message is one whose largest `sent_at − measured_at` exceeds 100 ms (none lies between 2 and 258 ms); all 167 of its measurement values (41 messages × 4, plus 3 for the first message, which has no CPU) are dropped from the end-to-end list, leaving 3248. Offsets are paired with samples by the analyzer's own rule (minimum-delay probe within ±300 s of the sample, `pick_offset`). With the nearest probe in time instead, the same exclusion gives p50 126.0 / p95 167.5 / p99 230.0 / max 1141.5 ms, and the unexcluded figures are p95 400.5 / p99 468.5. The artifact messages are spaced 60–63 s apart (25 gaps of 63 s, 16 of 60 s; 20 or 21 cycles), and the first message is one of them.

The p95 sits on the edge of this artifact: 4.9 % of messages are artifacts, just under the 5 % tail. The per-feature end-to-end p95 shows how sensitive it is (analyzer): memory 402.5, temperature 401.5, frequency 401.5, but CPU 316.0, because CPU is absent from the first message and so has 41 artifact values instead of 42.

The "network" figure is not affected: `sent_at` is stamped after the check, and the 42 artifact messages have the same network median (15.5 ms) as the rest [recomputed].

Network tail [recomputed, analyzer's offset rule]: 8 messages over 100 ms (seq 214: 170.0, 215: 102.0, 331: 137.5, 519: 133.5, 620: 119.0, 640: 130.0, 823: 1022.5, 840: 103.5). With nearest-probe offsets it is 7 (seq 215 drops below 100). They form 7 clusters (seq 214 and 215 are one) starting 641.9, 992.9, 1556.9, 1859.9, 1920.2, 2469.8 and 2519.9 s after the first message; intervals 351, 564, 303, 60, 550 and 50 s. Not periodic.

### 3.3 Dry run: latency

Age at send (analyzer): n=1535, p50 1.0, p95 2.0, p99 335.0, max 342.0 ms. **The dry run has the same sender artifact**: 19 of 384 messages, 248–342 ms, spaced 60 s (8 times), 63 s (9 times) and 123 s (once, across the outage) [recomputed]. Receiver → Ditto and end-to-end are not computable in a dry run.

"Network, offset-corrected" [analyzer, whole run]: n=384, min 8.0, p50 14.5, p95 31.5, p99 1633.5, max 3393.5 ms. Split at the outage [recomputed], two offset-pairing rules:

| Part | n | Rule | min | p50 | p95 | p99 | max | over 100 ms |
|---|---|---|---|---|---|---|---|---|
| seq 0–60 | 61 | analyzer (min-delay probe within ±300 s) | 8.0 | 16.0 | 20.0 | 24.0 | 24.0 | 0 |
| seq 0–60 | 61 | nearest probe | 6.5 | 14.5 | 18.0 | 23.0 | 23.0 | 0 |
| seq 67–389 | 323 | analyzer | 9.5 | 14.5 | 35.5 | 1633.5 | 3393.5 | 9 |
| seq 67–389 | 323 | nearest probe | 9.5 | 13.5 | 32.5 | 1633.5 | 3393.5 | 9 |

Over-100 ms messages after the outage (analyzer rule): seq 124 (1024.5), 166 (3393.5), 167 (1633.5), 168 (1042.5), 189 (1016.5), 267 (1078.5), 268 (2317.5), 367 (3131.5), 368 (1053.5). All nine are after the gap. Five clusters start 378, 507, 574, 808 and 1110 s after the first message; intervals 128, 67, 234 and 302 s.

### 3.4 Pi values and timestamp regularity (live run) [recomputed]

- Temperature 41.318–45.084 °C, 8 distinct values, all in steps of 0.538 °C.
- CPU frequency 600000 kHz in 806 messages, 1400000 in 47, 900000 in 1. Memory 19.44–20.22 %. `cpu_utilization` (853 values) median 0.08 %, p95 1.93 %, max 3.18 %. The Pi was idle.
- CPU `measured_at` stamps are exactly 3000 ms apart in all 852 consecutive pairs; the averaging window is 3000 ms in all 853 (min = max) [analyzer].
- Memory, temperature and frequency stamps are 3000 ± 1 ms apart (memory 3000 ×766, 3001 ×44, 2999 ×43; frequency 3000 ×764, 3001 ×45, 2999 ×44; temperature also 3002 ×14 and 2998 ×13). The deviations sum to +1, +1 and +2 ms over 853 intervals, so they are jitter around a fixed grid, not a different rate.
- This difference is expected from the code: `Sampler._cpu` stamps right after the `/proc/stat` read, the first read of the cycle; `Sampler._instant` stamps after its own file read and parse, which come later in the cycle. Within a message the memory stamp is 1 ms after the CPU stamp in 807 of the 853 messages that have a CPU value and 0 ms in 46; those 46 are exactly the ones whose frequency reading is 1400000 kHz. At 1400000 the later stamps come 1 ms sooner than at 600000 (frequency stamp minus CPU stamp: 1 ms in 46 of 46, 2 ms in 806 of 806; the one 900000 reading shows 1 ms). This is consistent with the Pi executing the reads faster at the higher clock; the governor was not read, so that is not confirmed.

## 4. What each quantity measures

- **Age at send** = `sent_at − measured_at`, Pi clock only. Per measurement. For CPU, `measured_at` is the end of its 3 s averaging window, so the information is on average 1.5 s older than this age shows (spec §5).
- **"Network, offset-corrected"** = `received_at − sent_at − offset`. `sent_at` is stamped on the Pi immediately before `HttpLink.post()`, which opens the TCP connection and sends the request, so **this figure includes TCP connection setup**. `received_at` is stamped on the laptop after the receiver has accepted the connection, read the request line and headers, checked authorization and read the body. So it also includes the receiver's per-connection startup, and the offset error (below). It is not a one-way network latency. Evidence from the dry run [recomputed]: seq 166 shows 3393.5 ms, and the sender's own CPU stamps for seq 166 and 167 are 3400 ms apart, so that cycle, which consists of the sample and the send, took about 3.4 s on the Pi, not just the part after the receiver stamped it.
- **Receiver → Ditto acknowledgement** = `patched_at − received_at`, receiver clock only: validation, deduplication, building the PATCH, the PATCH through the port-forward to Ditto, and Ditto's HTTP reply. It is the time to an HTTP acknowledgement, not the time until the value is visible downstream.
- **End to end** = `patched_at − (measured_at + offset)` per measurement = age at send + network + acknowledgement. It stops at Ditto's reply. Ditto → Mosquitto → Telegraf → InfluxDB is not measured.
- **Probe round trip (`delay`)** = `(t4 − t1) − (t3 − t2)`. The offset estimate has an error bound of `delay/2` plus about ±1 ms quantization (spec §4.1); here that is 2.5–9.5 ms for the live probes and 3–5 ms for the dry-run probes.

## 5. Events

### 5.1 Dry run: outage, seq 61–66

On disk [recomputed]: seq 60 was received at 22:32:54.313Z; seq 67 at 22:33:20.932Z; seq 61–66 do not appear in the log (6 messages missed, a 26.619 s gap, one silence episode). The sender consumed those six sequence numbers (a failed send still consumes its number). The CPU averaging window of seq 67 is 5007 ms, i.e. the preceding cycle lasted about 5 s, which fits the sender's 5 s request timeout.

User-reported, not on disk: the Pi's NetworkManager logged "link timed out" (a supplicant timeout) at 22:33:18Z and re-associated at 22:33:20Z; the SSH session to the Pi dropped at about the same time; the old SSH session was closed at 22:49:27Z, the same second the receiver log ends (the log's last sample is at 22:49:27.5Z); the sender's own failure lines were lost with that session.

Observations: messages resumed in the same second the Pi's Wi-Fi re-associated. The log shows no messages from 22:32:54.3Z, about 24 s before the logged time of the link timeout.

**The cause of the link timeout is UNKNOWN.** A 5-minute periodic cause (a background Wi-Fi scan) was hypothesised from the dry-run stall times. The live run did not support it: its messages over 100 ms are not periodic (§3.2), and the dry-run stall clusters themselves are 128, 67, 234 and 302 s apart (§3.3). The hypothesis is dropped, not confirmed.

### 5.2 Live run

No outage, no lost message, 0 silence episodes (§3.1). User-reported, not on disk: the sender printed "done: 854 sent ok, 0 failed" and no "send failed" line; `vcgencmd get_throttled` read `throttled=0x0` at about the start and again after the run. The first matches the log (854 messages applied, sequence 0–853 contiguous). `0x0` means none of the documented under-voltage or throttling bits, including the "has occurred" ones, was set at those two reads (spec §11).

## 6. Verified and not verified

Verified by these two runs:

- Delivery: live 854 of 854 messages received and applied, no gap; the dry run lost exactly the six messages of the outage.
- Ordering: sequence numbers arrived in order; no duplicate and no stale message occurred, so the deduplication and stale rules were not triggered by these runs (they are covered by the offline tests only).
- The Ditto path: 854 of 854 PATCHes to `pi_0` were acknowledged by Ditto with `ditto: ok` (HTTP-level acknowledgement; the run logs contain no read-back of the Thing's features).
- The first live smoke test, 5 samples, was read back from Ditto by the operator [user-reported, not on disk]: a GET of `pi_0` returned 200 with values equal to the sender's last sample and `link.last_seq` = 4.
- The numbers in §3, under the definitions in §4.

**Not verified:**

- Accuracy against an independent reference. No reference logger exists; nothing here says the values are right.
- Arrival of `pi_0` events in InfluxDB. The Ditto-to-Mosquitto connection must be re-created after the cluster restart; not checked.
- Which clock (laptop or Pi) causes the 28–35 ppm drift.
- The cause of the dry-run outage.
- Power and battery: not readable in software on this board.
- Behaviour under CPU load. The Pi was idle (`cpu_utilization` median 0.08 %).
- Temperature resolution is 0.538 °C per step, which limits any temperature comparison.
- Whether the sender fix in §7.1 has the expected effect on the Pi: it has only been run against fake clocks.

## 7. Defects and proposals

### 7.1 NTP-ordering artifact: age at send fixed in this change, not yet re-run on the Pi; CPU and frequency side effect still open

`Sender.cycle()` called `NtpProbe.get()` between sampling and stamping `sent_at`. That kept `timedatectl` out of the network figure but aged the measurements by its run time on every cycle where the 60 s refresh fell due (§3.2: 42 of 854 live messages, 258–349 ms; §3.3: 19 of 384 dry-run messages, 248–342 ms). The check now runs once before the first cycle and after each cycle's send and any due probe; `cycle()` only reads the cached value. Details and tests: spec §4.3, `NtpPlacementTests` in `tests/test_pi_sender.py`.

**Related observation, live run [recomputed].** Here "followed by" means the **next message**: for an artifact message with sequence number n, message n+1. The `timedatectl` run happens in the cycle of message n, after its measurements were read, so message n+1 holds the first measurements taken after it, and its CPU averaging window is the 3 s that contains the run. All 42 artifact messages have a next message.

- **Frequency.** All 42 next messages carry `cpu_frequency_khz` = 1400000; they make up 42 of the 47 readings of 1400000 in the run.
- **CPU utilisation.** Of the 853 values (the first message has none), 45 are above 1.0 %. Forty-two of the 45 are in a next message (seq 1, which follows the artifact at seq 0, is one of the 42), and 3 are elsewhere (seq 27: 1.93, seq 106: 1.35, seq 706: 1.43 %). The 42 next-message values have min 2.34, median 2.85, max 3.18 %. All other values (n = 811) have median 0.08, p95 0.50, p99 0.67, max 1.93 %. The two groups do not overlap (1.93 < 2.34).
- **The clock probe does not explain it.** A probe is taken after a send once a minute too, and its work falls in the window of the message that carries it. The probe-carrying messages and the next messages coincide in only 5 messages (seq 21, 41, 61, 641, 661). The 38 probe-carrying messages that are not next messages (seq 0 is one and has no CPU value; the other 37 do) show CPU min 0.0, median 0.17, max 0.67 %, and 1400000 kHz in 1 of the 38. The 37 next messages that carry no probe show CPU min 2.34, median 2.85, max 3.18 % and 1400000 kHz in 37 of 37.

So the once-a-minute `timedatectl` run is visible in the Pi's CPU and frequency series. The run's own CPU use was not isolated by an experiment (for example a run with the check disabled), and the governor was not read.

**Open defect: this change does not remove it.** The CPU averaging windows are contiguous 3000 ms intervals between consecutive `/proc/stat` reads (§3.4 for the live run; `Sampler._cpu` keeps each good read as the next baseline). The `timedatectl` run still happens once a minute and follows message n's read in the old order and in the new one (now after the send and inside the idle time), so it falls into the window of message n+1 in both. This is derived from the code; it has not been measured on the Pi after the fix, and neither has the size of the effect after the fix. The CPU and frequency series are therefore expected to keep a bump every 60–63 s that comes from the measurement itself: 42 of 853 CPU values (4.9 %) at 2.34–3.18 % against a median of 0.08 % (live run, old order). Options, all **unverified** and none implemented:

1. Read the kernel's clock-sync state directly with `adjtimex` instead of running `timedatectl`, after validating on the Pi that it agrees with `timedatectl`. Whether the standard library offers a usable call on Python 3.9 to 3.13 has not been checked.
2. Refresh every 300 s instead of 60 s. That would cut the number of affected messages by a factor of five and leave the flag up to 300 s old.
3. Check only at the start and the end of a run. That leaves the flag unknown for the part in between.

### 7.2 Probe lost when a send fails: by design, reported, not changed

Pending clock probes are discarded after every send attempt, successful or not, and ride only on the next telemetry message (`Sender.cycle`). In the dry run the receiver served 24 probes (5 at start, then one a minute) but the log carries 23 of them, so 23 of 24 are valid. The missing one was served at 22:32:54.329Z, 16 ms after seq 60 was received. It was pending when the send of seq 61 failed and was discarded with that attempt [recomputed]. In the live run all 47 probes served were carried (47 of 47).

### 7.3 Analyzer label "network, offset-corrected": proposal only

The label suggests a network latency. It measures send-to-receipt including TCP connection setup and the receiver's per-connection startup (§4). Proposal: rename it, for example "send to receipt, offset-corrected (includes TCP connect)". The analyzer is not changed.

### 7.4 Analyzer offset pairing: proposal only

The analyzer pairs each sample with the minimum-delay probe within ±300 s. With drift of 28–35 ppm (about 1.7–2.1 ms per minute) and probes a minute apart, a probe up to 5 minutes away can add up to about 10 ms of offset error beyond the `delay/2` bound. The two rules give different network percentiles in the live run [recomputed]: min 3.5 vs 9.0 ms, p95 28.0 vs 20.0 ms, p50 15.5 vs 15.0 ms (analyzer rule vs nearest probe). Proposal: pair each sample with the nearest probe, or interpolate between its two neighbours. No independent reference exists, so which rule is closer to the truth has not been tested.

## 8. Next steps

1. Re-run a measurement run with the fixed sender on the Pi and check that age at send is 1–2 ms on every message, and measure the size of the bump on the message after each `timedatectl` run (the live run before the fix: 1400000 kHz in 42 of 42, CPU 2.34–3.18 %).
2. Add a controlled CPU load and an independent reference reading, so accuracy can be measured.
3. Check that `pi_0` events reach InfluxDB (re-create the Ditto-to-Mosquitto connection first).
4. Ask Prof. Siddiqui about power measurement.
