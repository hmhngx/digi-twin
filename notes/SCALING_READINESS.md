# Scaling Readiness Assessment

**This is an assessment, not a certification.** Do not read this as “ready for scale.”

Audit session: 2026-07-28. Numbers from fresh dual-metric Phase 9 / Phase 10 runs only.

---

## What has been tested

| Dimension | Exact scope tested this session |
|-----------|----------------------------------|
| Twin / node count | **10** nodes (`node_0`…`node_9`), 2 racks, 1 cluster |
| Sensor-count concurrency | **1, 2, 4, 6, 8, 10** concurrent node PATCHes |
| Client-count concurrency | **1, 5, 10, 15, 20** concurrent clients on **one** Thing (`node_0`) |
| Metric (a) Ditto RTT | Measured; grows from ~0.6 s (1 sensor) to ~4.0 s (10 sensors); client-count avg ~1–5 s, with **503 ask.error** at 20 clients |
| Metric (b) E2E Influx | Measured; ~9–11 s, dominated by Telegraf `flush_interval=10s` |
| Fault recovery | **3** fresh `ditto-things` pod kills: **94.73, 62.77, 36.69 s** (mean 64.73 s, spread 58 s) |
| Pipeline | Single publisher process → Ditto HTTP → MQTT → Telegraf → Influx → Grafana |

---

## What is untested

- Behavior above **10 nodes** or **>20** concurrent clients
- Sustained multi-hour load / soak
- Multiple simultaneous publisher or aggregator instances
- Hono / Kafka inbound path (not deployed)
- Telegraf flush tradeoff curve (where lowering flush_interval breaks Influx write capacity)
- Production multi-replica Ditto / Mongo / Mosquitto

---

## Known single points of failure

1. **Single Ditto things instance** (one pod; kill test shows tens of seconds of inbound outage)
2. **Single MongoDB** backing Ditto / Extended API (restart storms observed after minikube stop)
3. **Single Mosquitto** broker for outbound events
4. **Single Telegraf** + **single InfluxDB** for metrics path
5. **kubectl port-forwards** on the operator laptop (PF drop crashes publisher)

---

## Inbound path real scaling limit

This PoC uses **one Python process** issuing **direct per-node HTTP PATCH**es. That is a deliberate simplification vs the paper’s **Hono → Kafka** ingestion. It will not scale to hundreds/thousands of devices the way Hono is designed to. **First change for real scale:** replace inbound with a proper device connectivity / messaging layer; keep Ditto as the twin store.

---

## Telegraf `flush_interval` tradeoff

- Current: **10 s** → metric (b) visibility ≈ 9–11 s.
- Lowering flush improves Grafana freshness but increases Influx write load.
- **Untested** where that tradeoff breaks down under this stack.

Do **not** present metric (b) as the platform’s Ditto scalability ceiling.

---

## Paper Test 1.2 vs this build’s metric (a)

Paper Test 1.2: latency exceeding ~1 s past ~20 concurrent clients on a single Thing (actor mailbox contention).

**This session’s metric (a):**

- Exceeds **1 s average** by **8 concurrent sensors** and by **10 concurrent clients** on one Thing (minikube + port-forward — not apples-to-apples absolute times).
- At **20 concurrent clients**, Ditto returned **HTTP 503** `ask.error` / internal timeout for many requests; only 3/20 samples completed.

**Conclusion:** At this much smaller tested scale, metric (a) **does show early signs of the same contention pattern** (RTT growth + timeouts under concurrent single-Thing load). That is an observation under constrained lab conditions, not a production SLO.

---

## Bottom line

| Claim | Verdict |
|-------|---------|
| Composition + pipelines work at 10 nodes | Supported by this audit |
| Grafana can show live hierarchy data | Supported (API Basic Auth; form login broken) |
| Comparable to paper sub-second/latency curves via E2E Influx times | **Rejected** — those were flush artifacts |
| Ready to scale to production device counts | **Not supported** — untested beyond 10/20; SPOFs; HTTP inbound PoC |
| Single recovery-time number (e.g. 23.55 s) | **Rejected** — 3-run spread 37–95 s |

Known: 10-node PoC works when the cluster is up. Unknown: everything beyond that matrix.
