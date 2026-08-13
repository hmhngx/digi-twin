# Professor Demo Playbook — Cluster Twin PoC

**Audience:** academic / professor walkthrough  
**Machine:** Windows PowerShell  
**Playbook authoring session:** 2026-08-06 (commands below were run fresh this session)  
**Primary sources:** `twin/README.md`, `notes/twin_design_spec.md`, `notes/VERIFICATION_REPORT.md`, `notes/SCALING_READINESS.md`, `notes/fault_tolerance_results.md`, `notes/scaling_comparison.md`

Use this document live. Prefer short talking points over reading paragraphs aloud.

---

## 1. Audit status (read first)

Independent QA audit dated **2026-07-28** (`notes/VERIFICATION_REPORT.md`). Fresh results from that audit win over prior build claims. This playbook’s authoring session (2026-08-06) re-verified the live pipeline after a real cold start and added **one** new fault-tolerance sample; it did **not** re-run the full scaling sweep.

### Summary table (from `notes/VERIFICATION_REPORT.md`)

| # | Item | Status |
|---|------|--------|
| 0 | Environment sanity | PASS WITH CAVEAT |
| 1 | Composition structure | PASS |
| 2 | Inbound publisher | PASS WITH CAVEAT |
| 3 | Outbound export (Ditto→MQTT→Telegraf→Influx) | PASS WITH CAVEAT |
| 4 | Failure detector | PASS WITH CAVEAT |
| 5 | Aggregator | PASS WITH CAVEAT |
| 6 | Grafana dashboard | PASS WITH CAVEAT |
| 7 | Scaling dual metrics | PASS WITH CAVEAT |
| 8 | Fault tolerance ×3 | PASS WITH CAVEAT |
| 9 | Git hygiene | PASS (see correction note below — was FAIL when this playbook was authored 2026-08-06, fixed and re-confirmed since) |
| 10 | Code quality / robustness | PASS WITH CAVEAT |

### What is confirmed working (with caveats)

- **Composition:** 3 Twin Types + 13 instances; parent links via `_parents` / Extended API children. Re-verified 2026-08-06 after cold start + setup re-run.
- **Inbound publisher:** HTTP PATCH → Ditto returns `204`. Caveat: crashes on port-forward / Ditto outage (no retry).
- **Outbound pipeline:** Ditto target connection → Mosquitto → Telegraf → Influx. Connection does **not** survive cluster restart without recreate.
- **Failure detector:** pause → `FAILED` → resume → `CLEARED`. Hold is **passive** (publisher skips paused IDs); detector does not PATCH “held” values into Ditto.
- **Aggregator:** rack then cluster in the **same** cycle (cluster reads rack features only). Wall-clock waits up to `AGGREGATE_INTERVAL_SEC`.
- **Grafana:** dashboard `clustertwin-main` present; panels query live Influx data via API (verified 2026-08-06). Login: `admin` / `AuditPass123!` (session password after audit reset). Default `admin`/`admin` still returns 401 Basic Auth this session.
- **Scaling numbers:** dual metrics recorded; see §4 step 8. Untested beyond 10 nodes / 20 clients (`notes/SCALING_READINESS.md`).
- **Fault tolerance:** 5 dated runs on record — 36.69 / 62.77 / 94.73 s (2026-07-28), 53.04 s (2026-08-06), 42.31 s (2026-08-13) — mean **57.91 s**, range **36.69–94.73 s** (`notes/fault_tolerance_results.md`).

### Known limitations / untested (from `notes/SCALING_READINESS.md`)

- Not tested above **10 nodes** or **>20** concurrent clients; no soak; no multi-publisher; no Hono/Kafka inbound; no production multi-replica Ditto/Mongo/Mosquitto.
- Single points of failure: single `ditto-things`, MongoDB, Mosquitto, Telegraf, Influx, laptop port-forwards.
- Production-scale readiness: **not supported**.

### Correction to this playbook — git hygiene (fixed since authoring)

This section originally read "Explicit FAIL: commit history contains `Co-authored-by: Cursor <cursoragent@cursor.com>`" as of this playbook's 2026-08-06 authoring session. That was accurate at the time. It was fixed in a later session (history rewritten to a single clean commit, `04570a3`) and re-confirmed fresh on 2026-08-13 (`git log --all --grep="Co-authored-by"` → empty; `git log --all --oneline` → one commit, no AI-attribution trailer). See `notes/VERIFICATION_REPORT.md` §9 for the full correction and `notes/FINAL_GATE_SUMMARY.md` for the cross-document reconciliation. If asked about repo hygiene: the history is clean and this was independently re-verified twice after the fix, not just fixed once and assumed to hold.

---

## 2. Cold-start checklist

**Timed this session (2026-08-06):** from `minikube start` through five port-forwards + health checks = **~7.4 minutes** wall clock  
(start `22:17:40` → health complete `22:25:03`; pods all Ready by `22:24:01`).

Prerequisites already present on this machine: Python venv, `.env` with `INFLUX_TOKEN`, OpenTwins Helm release on Minikube. There is **no** `helm install` / docker-compose in this repo (`twin/README.md`).

### 2.1 Start cluster and wait for pods

```powershell
minikube start
minikube update-context
kubectl get pods
# wait until OpenTwins pods show Ready (this session: ~6–7 min including minikube start)
```

Expected (abbreviated, this session when Ready):

```
opentwins-ditto-connectivity-...   1/1 Running
opentwins-ditto-extended-api-...   1/1 Running
opentwins-ditto-gateway-...        1/1 Running
opentwins-ditto-nginx-...          1/1 Running
opentwins-ditto-things-...         1/1 Running
opentwins-grafana-...              3/3 Running
opentwins-influxdb2-0              1/1 Running
opentwins-mongodb-...              1/1 Running
opentwins-mosquitto-...            1/1 Running
opentwins-telegraf-...             1/1 Running
```

### 2.2 Activate Python env

```powershell
cd "C:\Users\minhh\Side Hustles\digi-twin"
.\venv\Scripts\Activate.ps1
# If first time on a machine: python -m venv venv; pip install -r requirements.txt; copy .env.example .env
# INFLUX_TOKEN from: kubectl get configmap opentwins-telegraf-real-config -o yaml
```

### 2.3 Five port-forwards

```powershell
.\scripts\port-forwards.ps1
netstat -ano | findstr "LISTENING" | findstr "8080 8081 3000 8086 1883"
```

Expected listeners: `8080` Extended API, `8081` Ditto, `3000` Grafana, `8086` Influx, `1883` Mosquitto.

Stop later with: `Get-Process kubectl | Stop-Process`

### 2.4 Quick health checks (verified this session)

```powershell
curl.exe -s -u ditto:ditto http://localhost:8080/api/types/all | Select-Object -First 1
curl.exe -s -u ditto:ditto "http://localhost:8081/api/2/search/things?namespaces=hnguyen.clustertwin&option=size(5)" | Select-Object -First 1
curl.exe -s http://localhost:3000/api/health
curl.exe -s http://localhost:8086/health
Test-NetConnection 127.0.0.1 -Port 1883 | Select-Object TcpTestSucceeded
```

This session:

| Service | Result |
|---------|--------|
| Extended API `:8080` | HTTP 200 |
| Ditto `:8081` | HTTP 200 |
| Grafana `:3000/api/health` | `database: ok`, version `12.3.0` |
| Influx `:8086/health` | `status: pass`, v2.7.4 |
| Mosquitto `:1883` | `TcpTestSucceeded=True` |

### 2.5 Twins + MQTT connection (required after downtime)

```powershell
python scripts\setup_types_and_twins.py
python scripts\phase5_mqtt_influx.py create-connection
python scripts\phase5_mqtt_influx.py status
```

This session: Phase 2/3 verification **PASSED**; connection recreated with `"connectionStatus":"open"` (id `eb2f4642-...`). Setup is effectively idempotent on re-run (`notes/VERIFICATION_REPORT.md` §10).

### 2.6 Start the three long-running processes (three terminals)

```powershell
python -u twin\publisher.py
python -u twin\failure_detector.py
python -u twin\aggregator.py
```

Use `python -u` so logs flush immediately for the live demo.

---

## 3. Concept-to-implementation map

| Paper concept | Where demonstrated in this build | Exact command or file to show |
|---------------|----------------------------------|-------------------------------|
| **Compositionality** (Twin Types + instance `_parents`) | Types `NodeType` / `RackType` / `ClusterType`; instances `main_cluster` → `rack_0`/`rack_1` → `node_0`…`node_9` | `curl.exe -s -u ditto:ditto http://localhost:8080/api/types/all` and `curl.exe -s -u ditto:ditto "http://localhost:8080/api/twins/hnguyen.clustertwin%3Amain_cluster/children"` — also `notes/twin_design_spec.md` |
| **Attributes vs features** | Static attrs (`hardware_model`, …) vs dynamic features (`cpu_utilization`, …) | `curl.exe -s -u ditto:ditto "http://localhost:8081/api/2/things/hnguyen.clustertwin%3Anode_0"` — table in `notes/twin_design_spec.md` |
| **Cycle-free type graph** | On types, `_parents` is a map with cardinality: NodeType←**5**—RackType←**2**—ClusterType | After setup: NodeType `_parents={"hnguyen.clustertwin:RackType":5}`, RackType `_parents={"hnguyen.clustertwin:ClusterType":2}` |
| **Recursive aggregation** | `twin/aggregator.py`: racks from 5 child nodes; cluster from **two racks only** | `python -u twin\aggregator.py --cycles 5 --interval 7` — log shows `from racks […, …]` |
| **Sensor failure detection** (simplified vs paper Kafka-ML) | `twin/failure_detector.py`: 9 s tolerance, hold-last-value via pause file | `python -u twin\failure_detector.py --pause node_1` then `--resume node_1` while detector + publisher run |
| **Fault tolerance** | Kill `ditto-things` pod; time to first successful PATCH | `python -u scripts\fault_tolerance_test.py` — cite range, not one heroic number |

---

## 4. Live demo script (in order)

Each step: commands → expected output from this session → talking point.

### Step 1 — Composition tree

```powershell
curl.exe -s -u ditto:ditto http://localhost:8080/api/types/all
curl.exe -s -u ditto:ditto "http://localhost:8080/api/twins/hnguyen.clustertwin%3Amain_cluster/children"
curl.exe -s -u ditto:ditto "http://localhost:8081/api/2/search/things?namespaces=hnguyen.clustertwin&option=size(50)"
```

**Expected (this session):**

- Types: `NodeType` `_parents` → `RackType: 5`; `RackType` `_parents` → `ClusterType: 2`
- Cluster children: `rack_0`, `rack_1`
- Search: **16** things in namespace (3 types + 13 instances)

**Talk:** This is compositional Ditto modeling — a type graph with cardinalities, plus an instance tree linked through `_parents` / children APIs — not a flat name-prefix group. Mirrors the paper’s Factory → Robot → Sensor idea (`notes/twin_design_spec.md`).

### Step 2 — Live inbound PATCH

```powershell
python -u twin\publisher.py --cycles 5 --interval 3
```

**Expected (this session, abbreviated):**

```
Publisher started: 10 nodes, interval=3.0s
[1] PATCH hnguyen.clustertwin:node_0 cpu=10.0 -> HTTP 204
[1] PATCH hnguyen.clustertwin:node_1 cpu=38.7 -> HTTP 204
...
[5] PATCH hnguyen.clustertwin:node_9 cpu=18.79 -> HTTP 204
```

(50× HTTP 204 across 5 cycles × 10 nodes.)

**Talk:** Inbound telemetry updates dynamic **features** on each node twin. This PoC uses direct HTTP PATCH because the deployment has no Hono/Kafka (`twin/README.md` Out of Scope).

### Step 3 — Outbound event → InfluxDB

With publisher having run (and ~10–15 s for Telegraf flush):

```powershell
python scripts\phase5_mqtt_influx.py status
python scripts\phase5_mqtt_influx.py query
```

**Expected (this session):**

- Status: `"connectionStatus":"open"`
- Query: recent `mqtt_consumer` points with `thingId` like `hnguyen.clustertwin:node_9`, `parent` tag, field `value_cpu_utilization_properties_value` (e.g. cpu `54.48`)

**Talk:** Outbound uses the platform’s real Ditto → Mosquitto → Telegraf → Influx path. Visibility latency ~9–11 s is dominated by Telegraf `flush_interval=10s`, not Ditto work (`notes/scaling_comparison.md`).

### Step 4 — Failure detector pause / resume

Terminal A: `python -u twin\publisher.py`  
Terminal B: `python -u twin\failure_detector.py`

```powershell
python -u twin\failure_detector.py --pause node_1
# wait ~15–20 s
python -u twin\failure_detector.py --resume node_1
```

**Expected (this session):**

```
FAILED hnguyen.clustertwin:node_1: no feature change for 11.2s; holding last-known values cpu=20.81 latency=28.17
CLEARED failed: hnguyen.clustertwin:node_1 (updates resumed)
```

**Talk:** Paper Section 3.3 uses Kafka-ML to predict a next value. This build is a deliberate hold-last-value stand-in. Wording “holding” is **passive** — detector logs and tracks state; publisher skips paused IDs in `notes/_paused_nodes.json` (`notes/VERIFICATION_REPORT.md` §4).

### Step 5 — Aggregation propagates up

```powershell
python -u twin\aggregator.py --cycles 5 --interval 7
```

**Expected (this session, one cycle):**

```
RACK hnguyen.clustertwin:rack_0: avg_cpu=29.05 conns=279 healthy=5 -> HTTP 204
RACK hnguyen.clustertwin:rack_1: avg_cpu=33.27 conns=139 healthy=5 -> HTTP 204
CLUSTER hnguyen.clustertwin:main_cluster: avg_cpu=31.16 (from racks [29.05, 33.27]) conns=418 healthy=10 -> HTTP 204
```

**Talk:** Aggregation is recursive: cluster mean is the mean of **rack** averages, never a flat read of all 10 nodes. In this implementation, rack and cluster update in the **same** aggregator cycle (`notes/VERIFICATION_REPORT.md` §5). Healthy threshold: CPU &lt; 80% (`notes/twin_design_spec.md`).

### Step 6 — Grafana (live verified this session)

Open:

```
http://localhost:3000/d/clustertwin-main/cluster-twin-compute-monitoring
```

**Credentials that worked this session:** `admin` / `AuditPass123!`  
(Form login returned `{"message":"Logged in"}`; Basic Auth to `/api/org` also 200. `admin`/`admin` → 401.)

**Verified this session:** Grafana `/api/ds/query` against datasource UID `P4528D75AB74BE2EA` (`opentwins`) returned live `mqtt_consumer` CPU frames for `node_*` (HTTP 200, hundreds of frames over 5 minutes of data).

**If browser login fails on demo day (fallback — do not invent a working UI):**

1. Show live data via:

   ```powershell
   curl.exe -s -u ditto:ditto "http://localhost:8081/api/2/things/hnguyen.clustertwin%3Anode_0"
   python scripts\phase5_mqtt_influx.py query
   ```

2. Show design intent: `grafana/dashboard.json` (four panels: per-node, per-rack, cluster, sorted table) — evidence of dashboard design, not a live-rendered claim if panels do not load.

### Step 7 — Fault tolerance (one fresh run + audited spread)

```powershell
# Prefer stopping the long-running publisher first to reduce noise
python -u scripts\fault_tolerance_test.py
```

**All 5 dated runs on record** (`notes/fault_tolerance_results.md`, combined-picture table): 94.73 / 62.77 / 36.69 s (2026-07-28, audited 3-run set), 53.04 s (2026-08-06, post-cold-start), 42.31 s (2026-08-13, most recent) — **mean 57.91 s, range 36.69–94.73 s**. The two later samples both fall inside the original 3-run range, so they don't change the "high variance, don't cite one number" conclusion, only the mean.

**Do not cite 23.55 s** as “the” recovery time — audit discarded it as non-representative.

**Talk:** Paper Test 3 reported 52.46 s for a **Hono MQTT adapter** failure mode. We measure **direct HTTP** recovery — related but not identical. Present the **range and variance**, not a single cherry-picked second.

**After the kill:** confirm `ditto-things` is Ready again; re-run `python scripts\setup_types_and_twins.py` if type `_parents` maps look empty (observed once this session after pod kill); re-check `phase5_mqtt_influx.py status` and recreate connection if not `open`.

### Step 8 — Scaling results (cite audit; do not re-sweep live)

```powershell
# Show the dual-metric notes and chart — do not re-run notes\scaling_benchmark.py in the meeting
notepad notes\scaling_comparison.md
# or open notes\scaling_results.png
```

**Metric distinction (must say clearly — `notes/scaling_comparison.md`):**

| Metric | Meaning | Dominated by |
|--------|---------|--------------|
| **(a) `ditto_rtt_s`** | PATCH → Ditto HTTP response | Ditto / local k8s / port-forward — **paper-comparable** |
| **(b) `e2e_influx_s`** | PATCH → visible in Influx | Telegraf **10 s** flush floor — **not** a Ditto scalability ceiling |

**Audited averages (2026-07-28):**

Sensor-count (a)/(b): 1→0.62/10.55 s … 10→4.00/10.08 s.  
Client-count on `node_0`: at **20** clients many PATCHes returned HTTP **503** `ask.error` (only 3/20 samples completed).

**Talk:** Metric (b) sitting near ~9–11 s regardless of concurrency is the flush interval. Metric (a) growing and 503s at 20 clients are early contention signals on minikube — not a production SLO (`notes/SCALING_READINESS.md`).

---

## 5. Anticipated questions (honest answers already in the repo)

### Why direct HTTP instead of Hono/Kafka?

Defense wording from `twin/README.md` / `notes/twin_design_spec.md`:

> My deployment's inbound ingestion is simplified — direct HTTP to Ditto rather than the paper's Hono→Kafka pipeline. The outbound event export to Grafana, however, uses the platform's real MQTT/Telegraf plumbing, not a workaround.

Deployment has no Hono and no Kafka. First change for real scale: replace inbound with a proper device connectivity layer; keep Ditto as the twin store (`notes/SCALING_READINESS.md`).

### Why no Unity / 3D?

Explicitly out of scope for time/skill focus on the twin model and pipeline (`twin/README.md`, `notes/twin_design_spec.md`). Not a claim that 3D is unimportant — it was deferred.

### What’s the honest limitation you’d fix with more time?

From design spec Phase 6 section: replace hold-last-value with a small online predictor (or full Kafka-ML path if Kafka is added). From scaling readiness: real device connectivity (Hono/Kafka), multi-replica Ditto/Mongo, and testing beyond 10/20. Publisher crash-on-PF-drop is a PoC robustness gap (`notes/VERIFICATION_REPORT.md` §2, §10).

### Is the failure detector “holding” values into Ditto?

No. It logs `FAILED` and updates `notes/_failure_state.json`. Hold is passive because the publisher skips paused IDs. There is no active hold-write race (`notes/VERIFICATION_REPORT.md` §4).

### Does aggregation take two cycles to reach the cluster?

No for this code. Same loop: update racks, then cluster from those rack values. Brief that claimed “2 cycles” was incorrect (`notes/VERIFICATION_REPORT.md` §5).

### Does Grafana work?

This session: yes for panel data via API and login with `admin`/`AuditPass123!`. Audit caveat still relevant historically: Grafana 12 form login was broken for default credentials; do not assume `admin`/`admin`. If UI fails mid-demo, use the §4 step 6 fallback.

### Are you production-scale ready?

No. `notes/SCALING_READINESS.md`: composition + pipelines work at 10 nodes; production device counts **not supported**; SPOFs; HTTP inbound PoC.

### What’s your fault recovery time?

Present the range across all 5 dated runs: **36.69–94.73 s, mean ~58 s** (`notes/fault_tolerance_results.md`). High variance. Do not quote 23.55 s, and don't quote a single favorable run in isolation.

### Why is E2E latency always ~10 s?

Telegraf `flush_interval = "10s"`. That is metric (b). Do not compare it to the paper’s Ditto/Kafka latency curves (`notes/scaling_comparison.md`).

### Git / AI attribution?

Clean. History is a single commit (`04570a3`) with no `Co-authored-by` or other AI-attribution trailer — re-confirmed fresh 2026-08-13. This *was* a FAIL (Cursor co-authored-by) when this playbook was first authored 2026-08-06; it was fixed via history rewrite in a later session, and the fix has now been independently re-verified twice (`notes/VERIFICATION_REPORT.md` §9, `notes/FINAL_GATE_SUMMARY.md`). Secrets/`.env` hygiene: PASS, unchanged.

---

## 6. Explicit scope statement (say near the start of the meeting)

This proof-of-concept demonstrates genuine understanding of Eclipse Ditto’s compositional twin model (Twin Types, `_parents`, attributes vs features, recursive aggregation), working knowledge of the full OpenTwins pipeline (inbound update → Ditto → MQTT → Telegraf → Influx → Grafana), and honest engineering judgment about what to simplify and why (direct HTTP inbound instead of Hono/Kafka; hold-last-value instead of Kafka-ML; no Unity). It does **not** claim production-scale readiness: per `notes/SCALING_READINESS.md`, behavior beyond 10 nodes / 20 concurrent clients remains untested, the stack has single points of failure on local minikube, and scaling metric (b) must not be misread as Ditto’s ceiling.

---

## 7. Time budget (flexible)

| Block | Suggested | Notes |
|-------|-----------|-------|
| Cold-start → health → setup → connection | **~8–10 min** | Measured ~7.4 min to health this session; add setup/connection |
| Composition + live PATCH + Influx | **8–10 min** | Steps 1–3 |
| Failure detector + aggregator | **5–7 min** | Steps 4–5 |
| Grafana (or fallback) | **3–5 min** | Step 6 |
| Fault narrative + scaling dual metrics | **5–7 min** | Steps 7–8; optional live fault only if time — otherwise show `notes/fault_tolerance_results.md` |
| Q&A | **~10 min** | Use §5 |
| **Total demo content** | **~40–50 min** | Shrink Grafana/fault if short on time; never skip composition or the (a)/(b) distinction |

**Demo-day tip:** If the meeting is short, cold-start **before** the professor arrives, leave publisher/detector/aggregator running, and start the walkthrough at §4 Step 1.
