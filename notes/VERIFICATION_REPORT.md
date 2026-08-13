# Verification Report — Independent QA/SRE Audit

**Session date:** 2026-07-28  
**Role:** Independent auditor (prior build notes are hypotheses only)  
**Rule:** Every status below is backed by fresh command output from this session. Fresh results win over prior claims.

---

## 0. Environment sanity

**Status: PASS WITH CAVEAT**

### Fresh evidence

- Minikube was **stopped** at audit start (`kubelet: Stopped`, `apiserver: Stopped`, stale kubeconfig). Started via `minikube start` / `minikube update-context`.
- After recovery, all OpenTwins pods reached Ready (nginx/grafana required recreate; Grafana PVC `init-chown` previously failed — ownership fixed earlier in session; `init-chown-data` removed from deploy).
- Five port-forwards restarted via `scripts\port-forwards.ps1`; `netstat` showed LISTENING on `8080, 8081, 3000, 8086, 1883`.
- Pod restart counts elevated (Ditto services restart≈2, Mongo restart≈6, Telegraf≈7) — **state changed since original build**.
- Phase 5 MQTT connection had to be **recreated** after restarts (`connectionStatus: "open"` after recreate).

### Caveat

Any claim that “the Phase 5 connection from the build session is still open” was **false after minikube downtime** until recreated this session.

---

## 1. Composition structure

**Status: PASS**

### Fresh evidence

`GET http://localhost:8080/api/types/all` (ditto:ditto):

- `NodeType` attributes `_parents`: `{"hnguyen.clustertwin:RackType": 5}`
- `RackType` attributes `_parents`: `{"hnguyen.clustertwin:ClusterType": 2}`
- `ClusterType` present

`GET /api/2/search/things?namespaces=hnguyen.clustertwin&option=size(50)`:

- Exactly **13 instance twins**: `main_cluster`, `rack_0`, `rack_1`, `node_0`…`node_9`
- Plus 3 types → 16 things in namespace
- Every node `_parents` correct (`node_0..4` → `rack_0`, `node_5..9` → `rack_1`)
- Both racks `_parents` → `main_cluster`
- **No duplicate thingIds** (confirmed again after setup re-run: `dups []`)

---

## 2. Inbound publisher

**Status: PASS WITH CAVEAT**

### Fresh evidence (healthy run)

`python -u twin\publisher.py` for ~35s → **50× HTTP 204** (5 cycles × 10 nodes). Sample:

```
[1] PATCH hnguyen.clustertwin:node_0 cpu=10.0 -> HTTP 204
...
[5] PATCH hnguyen.clustertwin:node_9 cpu=42.98 -> HTTP 204
```

Status counts: `204` × 50.

### Deliberate failure

Killed kubectl port-forward on **8081** mid-run:

- Process **exited with code 1**
- Unhandled `requests.exceptions.ConnectionError` / `ConnectionRefusedError` stack trace
- **Does not** log-and-continue; **does not** retry; **crashes**

Same behavior with `DITTO_HTTP_URL=http://localhost:19999` (wrong port).

### Caveat

Publisher is not robust to Ditto unavailability. Must be restarted after PF/Ditto outages.

---

## 3. Outbound event export (Ditto → Mosquitto → Telegraf → InfluxDB)

**Status: PASS WITH CAVEAT**

### Fresh evidence

- `GET /api/2/connections` (devops:foobar): connection present with `"connectionStatus":"open"` after recreate (id e.g. `620d8f5d-...` / later `3fa138fe-...`).
- With publisher running: Influx query `-60s` returned **26+** / **69** recent `mqtt_consumer` points for `node_*` (timestamps within last minute of query).
- Telegraf ConfigMap: `flush_interval = "10s"`; logs show `Wrote batch of N metrics`.
- Telegraf debug: repeated missing path `extra.features.idSimulationRun.properties.value` (non-fatal; CPU fields still ingest).
- Ditto connectivity logs during minikube recovery: many `ERROR` / Mongo timeout / CircuitBreaker — **reconnect/error events occurred** around restart; pipeline healthy again after stabilize + connection recreate.

### Caveat

Connection does **not** survive cluster restart without recreate/check. Prior “still open from build” claim is not durable.

---

## 4. Failure detector

**Status: PASS WITH CAVEAT**

### Fresh evidence (node_5, not node_1)

```
FAILED hnguyen.clustertwin:node_5: no feature change for 14.8s; holding last-known values cpu=31.33 latency=36.99
CLEARED failed: hnguyen.clustertwin:node_5 (updates resumed)
```

### Race analysis (code + live)

Read `twin/failure_detector.py`: on failure it **only logs** and updates `notes/_failure_state.json`. It does **not** PATCH Ditto to “hold” values. Hold is **passive** (publisher skips paused IDs in `_paused_nodes.json`).

Therefore a race between detector “hold write” and publisher resume **does not exist as coded**. Residual race: pause-file timing vs publisher cycle (next PATCH may land one interval after resume). Live resume showed clean CLEARED without observed collision write.

### Caveat

Wording “holding last-known values” implies an active write; behavior is passive. Paper Kafka-ML prediction is out of scope.

---

## 5. Aggregator

**Status: PASS WITH CAVEAT** (contradicts “2 cycles to cluster” expectation)

### Code (line-by-line)

`aggregate_cluster_from_racks()` reads **rack** features only (`avg_cpu_utilization`, etc.), never all 10 nodes.

`run()` loop:

```python
for rid in RACK_IDS:
    aggregate_rack(rid)
aggregate_cluster_from_racks()
```

Same cycle: racks PATCH first, then cluster reads those rack values → **not** one-cycle-stale vs racks computed in that iteration.

### Live timing (node_0 CPU → 97.77)

Aggregator log showed one cycle updating both:

```
RACK ... rack_0: avg_cpu=45.39 ...
RACK ... rack_1: avg_cpu=42.68 ...
CLUSTER ... avg_cpu=44.03 (from racks [45.39, 42.68]) ...
```

After a node write, **one aggregator cycle** (wait for next loop) updates rack **and** cluster. The brief’s “should be 2 cycles” is **incorrect for this implementation**.

### Caveat

Wall-clock still waits up to `AGGREGATE_INTERVAL_SEC` (~5–7s) for the next cycle to start.

---

## 6. Grafana dashboard

**Status: PASS WITH CAVEAT**

### Diagnosis

- Pod health OK after PVC/init fix; `/api/health` → `database: ok`, version 12.3.0.
- Secret defaults `admin`/`admin`; form `POST /login` returns **HTTP 400** `form-auth.invalid` / `bad login data` (Grafana 12 form login broken/disabled path).
- `grafana-cli admin reset-admin-password AuditPass123!` succeeded inside pod.
- **Basic auth** `admin:AuditPass123!` works (`GET /api/org` → 200).

### Fix / verify

- Imported `grafana/dashboard.json` via API with datasource UID remapped from `opentwins` → live UID `P4528D75AB74BE2EA` (name still `opentwins`).
- Import result: `"status":"success","uid":"clustertwin-main"`.
- Four panels present and queried via Grafana `/api/ds/query`:
  1. **Per-node CPU** — frames for all `node_*` (live series)
  2. **Per-rack avg CPU** — `rack_0`, `rack_1`
  3. **Cluster avg CPU** — `main_cluster` value **32.21** observed
  4. **Sorted table** — node rows with CPU values

URL: `http://localhost:3000/d/clustertwin-main/cluster-twin-compute-monitoring` (use Basic Auth / browser login may still fail form UI).

### Caveat

Browser form login remains broken (`form-auth.invalid`). Access verified via API Basic Auth + datasource proxy. Password after reset: `AuditPass123!` (session-local; secret env may still say `admin`).

---

## 7. Scaling benchmark — dual metrics

**Status: PASS WITH CAVEAT**

### Fresh re-run

`notes/scaling_benchmark.py` now records **(a) ditto_rtt_s** and **(b) e2e_influx_s**. Results: `notes/scaling_results.json`, analysis: `notes/scaling_comparison.md`.

| n (sensors) | (a) avg Ditto RTT | (b) avg E2E Influx |
|------------:|------------------:|-------------------:|
| 1 | 0.62 s | 10.55 s |
| 2 | 0.65 s | 10.01 s |
| 4 | 0.64 s | 9.94 s |
| 6 | 0.79 s | 10.19 s |
| 8 | 1.64 s | 9.81 s |
| 10 | 4.00 s | 10.08 s |

| clients | (a) avg Ditto RTT | (b) avg E2E Influx |
|--------:|------------------:|-------------------:|
| 1 | 1.00 s | 9.10 s |
| 5 | 0.89 s | 10.16 s |
| 10 | 3.34 s | 10.20 s |
| 15 | 4.88 s | 10.47 s |
| 20 | 5.25 s (3/20 samples) | 9.47 s |

At **20 concurrent clients**, many PATCHes returned **HTTP 503** `ask.error` / internal timeout — early Ditto contention signal.

### Caveat

Prior report’s single ~9–12 s “latency” was **metric (b)** (Telegraf flush floor), not paper-comparable. Do not treat (b) as scalability ceiling. Untested beyond 10 nodes / 20 clients.

---

## 8. Fault tolerance

**Status: PASS WITH CAVEAT** (updated 2026-08-13 — two more runs were added in later sessions; see below)

### Fresh 3 runs, this session (prior 23.55 s discarded)

| Run | Recovery (s) |
|----:|-------------:|
| 1 | 94.73 |
| 2 | 62.77 |
| 3 | 36.69 |

- Mean **64.73 s**, spread **58.04 s** — **high variance**.
- Fresh evidence **contradicts** presenting 23.55 s as “the” recovery time.

### Update to this section — 2 more runs since (2026-08-13)

This section's original 2026-07-28 evidence above (3 runs, mean 64.73 s) is unchanged and still valid — it's what this audit session actually measured. Two more dated runs were added in later sessions: **53.04 s** (2026-08-06) and **42.31 s** (2026-08-13). Both fall inside the original 36.69–94.73 s range, so the **high-variance / don't-cite-one-number** conclusion is unaffected — but the **mean** across all 5 runs is **57.91 s**, not 64.73 s. Cite the 5-run figure (`notes/fault_tolerance_results.md`, "Combined picture across all 5 known runs") as current; treat 64.73 s as this section's own point-in-time number, superseded by more data, not wrong for what it was.

See `notes/fault_tolerance_results.md`.

---

## 9. Git hygiene

**Status: PASS** (updated 2026-08-12 — history was rewritten after this section was originally written; see below)

### Fresh evidence (re-verified 2026-08-12, independent of this file's own prior text)

- `git log --all --grep="Co-authored-by" --oneline` → **empty output**. No commit in the repository, reachable or unreachable via refs, contains an AI-attribution line.
- `git log --all --oneline` → the entire repository history is a **single commit**, `04570a3` ("Complete cluster twin PoC on OpenTwins with verified composition and pipelines."), authored by `hmhngx <nhminhhung05@gmail.com>`. The previously-flagged commit `425028e` / tag `twin-build-complete` no longer exists in `git log --all` — it was superseded (amend/rewrite), not merely hidden behind the new commit.
- Full commit body (`git log -1 --format=%B`) contains no `Co-authored-by` or other AI-attribution trailer.
- Committed `.gitignore` **contains** `.env` (and other local-state entries) — PASS.
- `.env` never appears in `git log --all --full-history -- .env` — PASS (confirmed again this session: file untracked, `git ls-files | grep -x .env` returns nothing).
- Grepped every blob ever committed to this repo (`git rev-list --objects --all` piped through `git cat-file --batch-check`, then each blob scanned for password/token/key/AKIA/PRIVATE KEY patterns) — the only two matches are `token=INFLUX_TOKEN` variable *references* in `notes/scaling_benchmark.py` and `scripts/phase5_mqtt_influx.py`, not literal secret values. No leaked credentials found in history.
- Hardcoded `ditto:ditto` / `devops:foobar` are OpenTwins platform defaults documented in the README — acceptable defaults for a local PoC, not leaked cloud tokens.

### Correction to this report

This section previously read "Status: FAIL (AI Co-authored-by)" based on 2026-07-28 evidence. That FAIL was accurate **at the time it was written**, but the underlying issue was fixed in a later session (history rewritten to commit `04570a3`) and this file was never updated to reflect it — i.e., the FAIL text sat stale in a completed report for two weeks. Re-verified fresh this session (2026-08-12); the fix is real and holds.

---

## 10. Code quality and robustness

**Status: PASS WITH CAVEAT**

### Fresh evidence

- Config via `.env` / `twin/config.py` defaults (`localhost` ports) — expected for local PoC.
- Wrong-port / PF-down: **unhandled stack trace**, exit 1 — not clean error message.
- No `signal` / `KeyboardInterrupt` handlers in publisher/aggregator/detector — Ctrl+C relies on default Python interrupt; no long-lived thread pools/clients to leak in the main loops (requests are sync per call). Scaling benchmark uses threads/ThreadPoolExecutor without explicit cancel.
- `setup_types_and_twins.py` **re-run**: before=16, after=16, **no duplicates** — effectively **idempotent** (PUT/overwrite style), safe to rerun against populated state.

### Caveat

Error handling is crash-oriented, not resilient. Document as PoC limitation.

---

## Summary table

| # | Item | Status |
|---|------|--------|
| 0 | Environment sanity | PASS WITH CAVEAT |
| 1 | Composition structure | PASS |
| 2 | Inbound publisher | PASS WITH CAVEAT |
| 3 | Outbound export | PASS WITH CAVEAT |
| 4 | Failure detector | PASS WITH CAVEAT |
| 5 | Aggregator | PASS WITH CAVEAT |
| 6 | Grafana dashboard | PASS WITH CAVEAT |
| 7 | Scaling dual metrics | PASS WITH CAVEAT |
| 8 | Fault tolerance ×5 (updated 2026-08-13; was ×3) | PASS WITH CAVEAT |
| 9 | Git hygiene | PASS (fixed in a later session; this row corrected 2026-08-12) |
| 10 | Code quality | PASS WITH CAVEAT |

**Nothing above is marked PASS based on the prior completion report alone.**
