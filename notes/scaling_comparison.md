# Scaling benchmark notes (Phase 9) — dual metrics

Two independent runs exist: **2026-07-28** (prior audit) and **2026-08-12** (this
session, re-verified fresh — `notes/scaling_results.json` currently holds this run's
raw data, since the script overwrites that file on every run). Both are kept below
so run-to-run variance is visible rather than silently overwritten. Metrics are
**separated**; do not conflate them.

## Metric definitions

| Metric | What it measures | Dominated by |
|--------|------------------|--------------|
| **(a) `ditto_rtt_s`** | Time from HTTP PATCH send until Ditto HTTP response | Ditto / port-forward / local k8s |
| **(b) `e2e_influx_s`** | Time from PATCH send until point visible in InfluxDB | **Telegraf `flush_interval = "10s"`** batching floor + MQTT path |

Paper Test 1 / Test 2 report Ditto/Kafka pipeline latency. Only metric **(a)** is meaningful for that comparison. Metric **(b)** is visibility latency for Grafana/Influx, not a Ditto scalability ceiling.

Verified live ConfigMap: `flush_interval = "10s"`. Most of (b) ≈ waiting for the next Telegraf flush, not Ditto work.

## 2026-08-12 re-run (this session, fresh)

Ran against the live cluster with `publisher.py` / `aggregator.py` / `failure_detector.py`
concurrently running (i.e. under realistic background load, not an idle cluster).

### (a) Ditto PATCH RTT

| Sensor count | Avg Ditto RTT (ms) | | Concurrent clients | Avg Ditto RTT (ms) | Notes |
|-------------:|--------------------:|-|--------------------:|--------------------:|-------|
| 1 | 201.1 | | 1 | 390.5 | |
| 2 | 638.9 | | 5 | 458.7 | |
| 4 | 881.2 | | 10 | 1161.8 | |
| 6 | 450.8 | | 15 | 1387.6 | |
| 8 | 1002.6 | | 20 | 1761.1 | **all 20/20 samples completed, no HTTP 503s this run** |
| 10 | 498.0 | | | | |

### (b) E2E PATCH → Influx visibility

| Sensor count | Avg E2E (s) | | Concurrent clients | Avg E2E (s) |
|-------------:|------------:|-|--------------------:|------------:|
| 1 | 3.67 | | 1 | 9.64 |
| 2 | 9.95 | | 5 | 9.97 |
| 4 | 10.01 | | 10 | 9.94 |
| 6 | 10.10 | | 15 | 10.13 |
| 8 | 9.77 | | 20 | 9.98 |
| 10 | 10.02 | | | |

Notable difference from the 2026-07-28 run: at 20 concurrent clients this run completed
**all 20/20** PATCHes with no HTTP 503s (avg RTT 1761 ms, elevated but not failing),
whereas the 2026-07-28 run only got 3/20 through before 503 `ask.error` timeouts. Same
qualitative trend (RTT rises with concurrency, non-monotonically at low n due to
scheduling noise) but the exact 503-contention threshold is **not a fixed number** —
it varies run to run on this shared local minikube. Treat "20 clients breaks Ditto" as
an observed-once caveat, not a reproducible ceiling.

## 2026-07-28 audit run (prior session, preserved for comparison)

### (a) Ditto PATCH RTT

| Sensor count | Avg Ditto RTT (ms) | | Concurrent clients | Avg Ditto RTT (ms) | Notes |
|-------------:|--------------------:|-|--------------------:|--------------------:|-------|
| 1 | 615.5 | | 1 | 1002.9 | |
| 2 | 645.0 | | 5 | 886.0 | |
| 4 | 639.8 | | 10 | 3338.6 | |
| 6 | 793.8 | | 15 | 4879.6 | |
| 8 | 1638.3 | | 20 | 5254.7 | **Incomplete sample**: many threads got HTTP **503** `ask.error` / internal timeout; only 3/20 updates completed |
| 10 | 3996.6 | | | | |

### (b) E2E PATCH → Influx visibility

| Sensor count | Avg E2E (s) | | Concurrent clients | Avg E2E (s) |
|-------------:|------------:|-|--------------------:|------------:|
| 1 | 10.55 | | 1 | 9.10 |
| 2 | 10.01 | | 5 | 10.16 |
| 4 | 9.94 | | 10 | 10.20 |
| 6 | 10.19 | | 15 | 10.47 |
| 8 | 9.81 | | 20 | 9.47 (3 samples only) |
| 10 | 10.08 | | | |

(b) sits near **~9–11 s** regardless of concurrency in both runs because of the 10 s
flush interval, except sensor-count n=1 which can undershoot (3.67s / 10.55s across
the two runs — small-n samples are noisy). Treating (b) as "platform latency" would
incorrectly hide the Ditto RTT growth in (a).

## Honest comparison to the paper

- Paper: latency grows with sensor count; exceeds ~1 s past ~20 concurrent clients on a single Thing (mailbox contention).
- Both PoC runs show metric **(a)** exceeding 1 s somewhere between 8–10 concurrent sensors/clients on one Thing — consistent direction with the paper, at much smaller absolute scale on single-node minikube + port-forwards.
- The 20-client 503 contention seen 2026-07-28 did **not** reproduce 2026-08-12 (same code, same cluster, different run) — report this as observed variance, not a hard limit.
- Metric **(b)** must not be compared to paper figures.

## Caveats

- Local minikube, kubectl port-forwards, no Hono/Kafka inbound.
- 2026-07-28 client-count n=20 results are partial due to 503s — reported honestly, not averaged away.
- 2026-08-12 run was executed while the full three-script pipeline (`publisher.py`,
  `aggregator.py`, `failure_detector.py`) was also running against the same cluster,
  i.e. under more realistic concurrent load than an isolated benchmark run.
- Raw data: `notes/scaling_results.json` (currently holds the 2026-08-12 numbers —
  **this file is overwritten by every run of `notes/scaling_benchmark.py`**, it does
  not accumulate history; that is why both runs' numbers are transcribed by hand
  into this comparison doc instead of relying on the JSON alone).
  Charts: `notes/scaling_results.svg` / `.png` (also overwritten each run, so also
  reflect only the 2026-08-12 numbers as of this write-up).
