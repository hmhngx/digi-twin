# Scaling benchmark notes (Phase 9)

## Measured results (local minikube)

See `scaling_results.json` and `scaling_results.png`.

| Sensor count | Avg E2E latency (s) |
|-------------:|--------------------:|
| 1 | 8.79 |
| 2 | 10.00 |
| 4 | 10.64 |
| 6 | 8.78 |
| 8 | 10.32 |
| 10 | 10.65 |

| Concurrent clients (single Thing) | Avg E2E latency (s) |
|----------------------------------:|--------------------:|
| 1 | 8.77 |
| 5 | 10.16 |
| 10 | 10.04 |
| 15 | 12.45 |
| 20 | 8.28 |

Latency is **PATCH → visible in InfluxDB** via Ditto → Mosquitto → Telegraf.

## Honest comparison to the paper

- Paper: latency grows roughly linearly with sensor count; exceeds ~1 s past
  20 concurrent clients on a single Thing (Figs. 4–6).
- This PoC: absolute latencies sit near **~9–12 s**, dominated by Telegraf's
  configured `flush_interval = "10s"` (verified in live ConfigMap). That is a
  batching floor, not Ditto HTTP RTT.
- Directionally, higher concurrency still tends to increase wall time / spread
  (e.g. client-count n=15 wall 13.1 s, avg 12.4 s), but the small sample and
  10 s flush mask fine-grained linear growth.
- Plausible differences: single-node minikube, kubectl port-forwards, no Hono/
  Kafka inbound, tiny Thing set (10 nodes), Telegraf flush dominating E2E.

Defense takeaway: report the Telegraf flush floor explicitly; do not claim
sub-second platform latency when the metrics pipeline batches every 10 s.
