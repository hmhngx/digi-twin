# Live System Behavior Record — 2026-09-23

**What this is:** a record of what the running system actually did when each of its capabilities was exercised on 2026-09-23, between 21:10:53Z and about 21:37Z. It contains observations only. It does not assess what the twin should be used for, what to build, or which adaptation option to pursue. Those questions are waiting on Prof. Siddiqui's answer (`notes/PROFESSOR_UPDATE_2026-09-22_DRAFT.md`, `notes/PURPOSE_AND_DIRECTION_AUDIT.md`).

**Repo state during the session:**
- HEAD is `dfe5be3`. `twin/` has no uncommitted changes.
- `notes/PROFESSOR_UPDATE_2026-09-22_DRAFT.md` is modified relative to HEAD, by someone other than this session. Its line 22 now asks the purpose question directly. `PURPOSE_AND_DIRECTION_AUDIT.md` line 160 says the draft "never asks directly"; that statement predates the edit. Noted here, not edited.
- No code was written or changed.

**Raw evidence:** every command's output is saved under `notes/_live_2026-09-23/`, which is gitignored by `notes/_*`. File names are cited in each section. Times are UTC. Local time was UTC−4.

**Credentials used:**
- `ditto:ditto` for Ditto and the Extended API, as `twin/ditto_client.py` uses them.
- For the Ditto connectivity API, the repo's own functions and constant in `scripts/phase5_mqtt_influx.py` (`DEVOPS_AUTH`).
- **Grafana was not logged into.** This session does not enter passwords. Everything that depends on a Grafana login is listed in the last section.

**State this session changed, and restored:**

| Change | Restored? |
|---|---|
| Three node pauses (`node_3`, `node_0`, `node_5`) via the repo's `--pause` CLI | Each resumed. Both pause files ended as `[]`. |
| One temporary Thing, `hnguyen.clustertwin:zz_cardinality_probe` | Deleted; `GET` returns 404. |
| Probe's Ditto events reached InfluxDB via MQTT | **No.** A few points tagged `thingId=hnguyen.clustertwin:zz_cardinality_probe` now sit in the `default` bucket. |
| A second, isolated `failure_detector.py` process | Ran from a scratchpad copy, not the repo, for 900 s (21:21:45Z–about 21:36:46Z), then exited with code 0. |

Harrison's own processes were not stopped, restarted, or modified. At 21:37:03Z the same six PIDs from Part 1 were still running (32416/28656 publisher, 8772/22632 aggregator, 34084/21080 detector), and they were the only Python processes on the machine (`p_end_processes.txt`).

---

## Part 1 — What was running

| Component | Observed state | How checked (evidence file) |
|---|---|---|
| Minikube | `host: Running`, `kubelet: Running`, `apiserver: Running`, `kubeconfig: Configured`; exit 0, at 21:10:53Z | `minikube status` (`p1_cluster.txt`) |
| OpenTwins pods, namespace `default` | All 13 `Running` and Ready: `ditto-connectivity`, `ditto-extended-api`, `ditto-fixer`, `ditto-gateway`, `ditto-nginx`, `ditto-policies`, `ditto-things`, `ditto-thingssearch` (1/1 each), `grafana` (3/3), `influxdb2-0`, `mongodb`, `mosquitto`, `telegraf` (1/1 each). Every pod showed a restart about 50 min before the check, i.e. near 20:20Z; for example `ditto-connectivity` had 26 restarts, the latest "50m ago". `storage-provisioner` in `kube-system` restarted "30m ago". | `kubectl get pods -A -o wide` (`p1_cluster.txt`) |
| Port-forwards | 5 `kubectl port-forward` processes, all created 20:43:58Z: `svc/opentwins-ditto-extended-api 8080:8080`, `svc/opentwins-ditto-nginx 8081:8080`, `svc/opentwins-grafana 3000:80`, `svc/opentwins-influxdb2 8086:80`, `svc/opentwins-mosquitto 1883:1883`. Each port had a LISTEN socket on both `127.0.0.1` and `::1`, owned by that process. No port-forward to MongoDB; the repo doesn't use one. | `Get-CimInstance Win32_Process`, `Get-NetTCPConnection -State Listen` (`p1_processes.txt`) |
| Service reachability at 21:31:41Z | Ditto `GET /api/2/things/…main_cluster` → 200 in 1.30 s. Extended API `GET /api/types/all` → 200 in 1.12 s. InfluxDB `GET /health` → 200, `"status":"pass"`, v2.7.4. Grafana `GET /api/health` → 200, `"database":"ok"`, v12.3.0. Mosquitto TCP connect to 127.0.0.1:1883 OK in 20.1 ms. | curl and a Python socket connect (`p1_service_health.txt`) |
| `twin/publisher.py` | Running, started 20:45:07Z | `p1_processes.txt`, `p1_process_tree.txt` |
| `twin/aggregator.py` | Running, started 20:45:26Z | same |
| `twin/failure_detector.py` | Running, started 20:45:32Z | same |
| Real pause file `notes/_paused_nodes.json` | `[]`; last written 20:59:21Z today, before this session's first action | `ls`, `cat` |

**How the three twin processes appear.** Each script appears as **two** OS processes: the venv's `python.exe` and the base `Python312\python.exe`. The process tree shows these are one launcher plus its child, not duplicates:
- the venv `python.exe`'s parent is `pwsh.exe`;
- the base interpreter's parent is that venv `python.exe` (e.g. publisher: 32416 ← pwsh 18688; child 28656 ← 32416).

Each script has its own `pwsh` parent (18688, 7564, 35184), so they run in three separate console windows. None of those windows was readable from this session; the one terminal tab this session can read was idle at a prompt.

---

## Part 2 — Ditto and Extended API surface

**The endpoint list comes from the service itself.** `GET http://localhost:8080/docs/swagger-ui-init.js` → 200 contains the Extended API's embedded Swagger 2.0 document ("Extended API for Eclipse Ditto" 1.0.0). It has 36 operations under `/api/twins`, `/api/types`, `/api/connections` and `/api/policies`. The parsed spec is saved in `p2_extapi_swagger.json`.

Every call below is logged in full in `p2_calls.jsonl` (timestamps, status, first 4000 characters of each body). "ext" means the Extended API at :8080; "ditto" means the Ditto core API at :8081.

### 2.1 Twins

| Method and path | Status | Representative response |
|---|---|---|
| ext `GET /api/twins/` | 200 (1006 B) | **Root twins only:** `hnguyen.clustertwin:main_cluster` with its three features (e.g. `avg_cpu_utilization` 38.37, `total_active_connections` 570, `healthy_node_count` 10), plus two Things outside this namespace: `org.eclipse.ditto:05464685-5aa8-4d8e-b28f-6466b272d958` and `org.eclipse.ditto:1c9200c3-d37b-43cd-b991-4256c91a4dab`, each with `"attributes": {}` |
| ditto `GET /api/2/search/things?namespaces=hnguyen.clustertwin&option=size(50)` | 200 | 16 items: `ClusterType`, `NodeType`, `RackType`, `main_cluster`, `rack_0`, `rack_1`, `node_0`…`node_9` (`p2_search_count.txt`) |
| ditto `GET /api/2/things/{id}`, each of the 13 instance twins | 200 ×13 (447–538 B) | e.g. `node_3`: `"attributes":{"_parents":"hnguyen.clustertwin:rack_0","hardware_model":"generic-1U","rack_position":3,"install_date":"2026-01-01",…,"type":"hnguyen.clustertwin:NodeType","_isType":false}`, `"features":{"cpu_utilization":{"properties":{"value":30.43}},…}` |
| ext `GET /api/twins/{id}`, each of the 13 | 200 ×13 (592–731 B) | Same Thing as pretty-printed JSON. This view does **not** include the `_isType` attribute the core API shows. Calls were sequential; consecutive calls completed 0.66–3.66 s apart. |
| ditto `GET /api/2/things/hnguyen.clustertwin%3Anode_99` | **404** | `{"status":404,"error":"things:thing.notfound","message":"The Thing with ID 'hnguyen.clustertwin:node_99' could not be found or requester had insufficient permissions to access it.",…}` |
| ext `GET /api/twins/hnguyen.clustertwin%3Anode_99` | **404** | empty body |
| ditto `GET /api/2/things/node_0` (no namespace) | **400** | `{"status":400,"error":"things:id.invalid","message":"Thing ID 'node_0' is not valid!","description":"It must conform to the namespaced entity ID notation…"}` |
| ext `GET /api/twins/node_0` | **400** | empty body |
| ext `GET /api/twins/…main_cluster/children` | 200 | `rack_0`, `rack_1` |
| ext `GET /api/twins/…rack_0/children` | 200 | `node_0`…`node_4` |
| ext `GET /api/twins/…rack_1/children` | 200 | `node_5`…`node_9` |
| ext `GET /api/twins/…node_0/children` | 200 | `[]` |
| ext `GET /api/twins/…node_0/parent` | 200 | `"hnguyen.clustertwin:rack_0"` |
| ext `GET /api/twins/…node_7/parent` | 200 | `"hnguyen.clustertwin:rack_1"` |
| ext `GET /api/twins/…rack_1/parent` | 200 | `"hnguyen.clustertwin:main_cluster"` |
| ext `GET /api/twins/…main_cluster/parent` | **404** | empty body. This is the root. |
| ext `GET /api/twins/…node_99/parent` | **404** | empty body |

### 2.2 Types

| Method and path | Status | Representative response |
|---|---|---|
| ext `GET /api/types/` | 200 (586 B) | Root types only: `ClusterType` |
| ext `GET /api/types/all` | 200 (1982 B) | `ClusterType`, `NodeType`, `RackType` |
| ext `GET /api/types/…NodeType` | 200 | `"_parents": {"hnguyen.clustertwin:RackType": 5}`, features all at value 0 |
| ext `GET /api/types/…RackType` | 200 | `"_parents": {"hnguyen.clustertwin:ClusterType": 2}` |
| ext `GET /api/types/…ClusterType` | 200 | no `_parents` |
| ext `GET /api/types/…NoSuchType` | **404** | empty body |
| ext `GET /api/types/…NodeType/children` | 200 | `[]` |
| ext `GET /api/types/…NodeType/parent` | 200 | `{"hnguyen.clustertwin:RackType": 5}` |
| ext `GET /api/types/…RackType/children` | 200 | `NodeType` |
| ext `GET /api/types/…RackType/parent` | 200 | `{"hnguyen.clustertwin:ClusterType": 2}` |
| ext `GET /api/types/…ClusterType/children` | 200 | `RackType` |
| ext `GET /api/types/…ClusterType/parent` | **404** | empty body |

### 2.3 Cardinality: tried, reversibly, not assumed

This is the one write sequence in Part 2 (`p2_cardinality.txt`, `p2_cardinality.jsonl`). The attempt was to link a sixth NodeType twin under `rack_0`, whose RackType allows five.

| Time | Call | Result |
|---|---|---|
| before | ext `GET …rack_0/children` | `node_0`…`node_4` (5 children) |
| 21:16:33.365 | ditto `GET /api/2/things/…zz_cardinality_probe` | 404; the probe did not already exist |
| 21:16:39.091 | ext `POST /api/types/…NodeType/create/…zz_cardinality_probe` | 200, body `""` |
| 21:16:40.291 | ext `GET …zz_cardinality_probe/parent` | 404; no parent yet |
| 21:16:41.709 | ext `PUT /api/twins/…rack_0/children/…zz_cardinality_probe` | **200, body `""`: the sixth child was accepted** |
| right after | ext `GET …rack_0/children` | still listed only 5 |
| 21:16:43.797 | ext `GET …zz_cardinality_probe/parent` | 200 `"hnguyen.clustertwin:rack_0"` |
| 21:16:44.412 | ditto `GET …zz_cardinality_probe/attributes` | 200, `"_parents":"hnguyen.clustertwin:rack_0"` |
| 21:16:45.190 | ext `PATCH …zz_cardinality_probe/parent/unlink` | 204 |
| right after | ext `GET …rack_0/children` | listed **6**: `node_0`…`node_4`, `zz_cardinality_probe` |
| 21:16:47.184 | ext `DELETE /api/twins/…zz_cardinality_probe` | 204 |
| 21:16:49.332 | ditto `GET …zz_cardinality_probe` | 404 |
| final | — | `rack_0` children back to `node_0`…`node_4`; namespace count 16; `/api/policies/` list identical before and after. Re-checked 404 at 21:30:46Z (`p5_telegraf_and_cleanup.txt`). |

Observed:
- **The RackType→NodeType cardinality of 5 was not enforced when the twins were linked.** The link returned 200, and the probe's `_parents` in Ditto's core API became `rack_0`.
- **`/children` lagged the core API by a few seconds in both directions.** It still showed 5 children right after the link, and showed the probe right after the unlink.
- `PUT /api/twins/fix` and `PUT /api/types/fix` were **not** called. Both may modify real twins or types.

### 2.4 Connections

| Method and path (credentials) | Status | Representative response |
|---|---|---|
| ext `GET /api/connections/ids` (`ditto:ditto`) | **401** | empty body |
| ditto `GET /api/2/connections` (`ditto:ditto`) | **401** | `The supplied authentication is invalid` |
| ditto `GET /api/2/connections` (repo's `DEVOPS_AUTH`) | 200 | Exactly one connection: `{"id":"dc8b94b0-48f8-40ae-b6c9-332972a049e9","name":"ClusterTwin MQTT events to Mosquitto","connectionType":"mqtt","connectionStatus":"open","uri":"tcp://opentwins-mosquitto:1883","sources":[],"targets":[{"address":"opentwins/hnguyen.clustertwin/{{ thing:name }}",…,"qos":1,"authorizationContext":["nginx:ditto"]…}],"clientCount":1,…}` |
| ext `GET /api/connections/dc8b94b0-…` (`ditto:ditto`) | **401** | empty body |
| ditto `GET /api/2/connections/dc8b94b0-…/status` (`DEVOPS_AUTH`) | 200 | `"connectionStatus":"open","liveStatus":"open","recoveryStatus":"succeeded","connectedSince":"2026-09-23T20:44:33.259Z"`; client status `CONNECTED`; target status `"unknown"` with `"Producers for targets with placeholders are started on-demand."` |
| ditto `GET /api/2/connections/dc8b94b0-…/metrics` (`DEVOPS_AUTH`) | **503** | `{"status":503,"error":"connectivity:connection.timeout","message":"The operation 'connectivity.commands:retrieveConnectionMetrics' on connection '…' timed out."…}` |

**The connection ID changed since yesterday.** The previous session saw `d86df83c-da77-402a-b0cf-718be4194699` on 2026-09-22. Today:
- `notes/_connection_id.txt` contains `dc8b94b0-…` and was last written at 20:44:29Z, four seconds before `connectedSince`.
- `scripts/phase5_mqtt_influx.py`'s `create_connection()` (lines 166–202) always deletes an existing connection before creating a new one, so its ID changes on every run.

These observations cannot tell whether yesterday's connection survived today's restart.

### 2.5 Policies

| Method and path | Status | Representative response |
|---|---|---|
| ext `GET /api/policies/` | 200 | `["org.eclipse.ditto:1c9200c3-…","org.eclipse.ditto:05464685-…","hnguyen.clustertwin:basic_policy","org.eclipse.ditto:ea41b377-1d50-46cb-87cb-a9dbdcb60d55"]` |
| ditto `GET /api/2/policies/hnguyen.clustertwin%3Abasic_policy` | 200 | `{"policyId":"hnguyen.clustertwin:basic_policy","imports":{},"entries":{"DEFAULT":{"subjects":{"nginx:ditto":{"type":"generated"}},"resources":{"thing:/":{"grant":["READ","WRITE"],"revoke":[]},"policy:/":{"grant":["READ","WRITE"],"revoke":[]},"message:/":{"grant":["READ","WRITE"],"revoke":[]}},"importable":"implicit"}}}` |
| ditto `GET /api/2/policies/…no_such_policy` | **404** | `{"status":404,"error":"policies:policy.notfound",…}` |

All 13 instance twins and all 3 types report `"policyId":"hnguyen.clustertwin:basic_policy"`.

---

## Part 3 — Read path and aggregation math, recomputed independently

**Method.**
- Take snapshots: 81 multi-gets over 119 s (21:17:43–21:19:42Z), about 1 s apart, of all 13 Things with their features and Ditto's `_modified` timestamps. Each is one call: `GET /api/2/things?ids=…&fields=thingId,features,_modified` → 200. Saved in `p3_snapshots.jsonl`.
- Recompute every rack and cluster aggregate stored during that window in a separate scratch calculation, *not* by calling `aggregator.py`. The formulas follow `twin/aggregator.py`:
  - rack `avg_cpu_utilization` = `round(mean of the 5 nodes' cpu, 2)`
  - rack `total_active_connections` = `int(sum of the 5 nodes' connections)`
  - rack `healthy_node_count` = the number of those nodes with cpu < 80
  - cluster `avg_cpu_utilization` = `round(mean of the two rack averages, 2)`
  - cluster totals = sums of the rack totals

**Node values at the last snapshot, 21:19:42Z:**

| Node | cpu | mem | conns | latency_ms | Ditto `_modified` |
|---|---:|---:|---:|---:|---|
| node_0 | 49.2 | 92.5 | 70 | 8.7 | 21:19:39.693 |
| node_1 | 30.48 | 29.09 | 50 | 17.07 | 21:19:39.775 |
| node_2 | 48.47 | 69.8 | 68 | 32.78 | 21:19:39.866 |
| node_3 | 53.31 | 46.22 | 47 | 36.57 | 21:19:39.962 |
| node_4 | 48.83 | 60.49 | 11 | 24.9 | 21:19:40.056 |
| node_5 | 21.08 | 55.85 | 23 | 14.05 | 21:19:40.156 |
| node_6 | 51.02 | 66.66 | 58 | 24.66 | 21:19:40.270 |
| node_7 | 32.82 | 40.71 | 41 | 38.35 | 21:19:40.391 |
| node_8 | 23.4 | 41.51 | 48 | 26.94 | 21:19:40.511 |
| node_9 | 45.84 | 63.45 | 73 | 32.09 | 21:19:40.619 |

At that moment Ditto held these aggregates:
- `rack_0` {31.93, 229, 5}, written 21:19:30.384
- `rack_1` {40.59, 229, 5}, written 21:19:32.772
- `main_cluster` {36.26, 458, 10}, written 21:19:36.003

All three were written *before* the node values above, so they cannot be compared with them directly. The comparisons below account for that.

**Results** (`p3_aggregation_check.txt`, `p3_aggregation_check_full.txt`, `p3_aggregation_torn_read_check.txt`):

- **Cluster: 9 of 9 cluster writes match exactly.** Each was recomputed from the two rack values stored just before it. Only the aggregator writes racks, and it writes the cluster right after both racks. For example, at 21:19:36.003 the stored value was {36.26, 458, 10}; (31.93 + 40.59)/2 = 36.26, 229 + 229 = 458, and 5 + 5 = 10.
- **Rack writes: 13 of 16 match** at least one combination of node values that were current during the aggregator's read window. (The first write of each rack was skipped because its window started before the first snapshot. The read window used was 6.68 s before each rack write: the largest observed gap between a `rack_0` write and the following `rack_1` write, plus 0.5 s.)
- **Rack writes: the other 3 all belong to `rack_1`**, written at 21:18:08.777, 21:18:52.917 and 21:19:19.593. They match **only** when CPU is taken from a node's older publish and connections from its newer publish, and only for the last one or two nodes (node_8/node_9, or node_9 alone). That matches the code order: `aggregator.py` lines 52–53 read `cpu_utilization` and `active_connections` in two separate GETs, and in these three windows the publisher rewrote those nodes within about 0.2–0.5 s before the rack write (e.g. node_8 at 21:18:08.576 and node_9 at 21:18:08.664 against the rack write at 21:18:08.777). This is *consistent with* one publish landing between the aggregator's two reads of the same node. It is not proven, since with 32 possible combinations per case some coincidental matches are possible.
- **One rack write had inputs that were steady for more than 4 s** before it: `rack_0` at 21:17:51.375, which **matched exactly**. Worked by hand:
  - cpu = 49.6 + 36.37 + 52.05 + 53.19 + 41.97 = 233.18; 233.18 / 5 = 46.636 → **46.64**
  - connections = 70 + 15 + 36 + 45 + 45 = **211**
  - all 5 cpu values are below 80 → **healthy 5**
  - Stored value: {46.64, 211, 5}.

**Observed cadence** over the same window, from `_modified` gaps:

| Series | Observed gaps | Configured interval |
|---|---|---|
| `rack_0` writes | 10.9–18.6 s | `AGGREGATE_INTERVAL_SEC = 7` |
| `main_cluster` writes | 11.6–16.4 s | same |
| `node_3` publishes | 4.4–8.9 s | `PUBLISH_INTERVAL_SEC = 3` |

In both loops, the configured interval is a sleep between cycles, not the cycle period.

---

## Part 4 — Failure detection and prediction

**Setup.** Harrison's running detector writes to `notes/_failure_state.json`, and its console is in a window this session cannot read. To capture console output without touching his process, an **isolated copy** was run:
- the seven `twin/*.py` files were copied to the session scratchpad, byte-identical to `dfe5be3` (each file checked with `cmp`);
- it was started as `python -u twin/failure_detector.py --duration 900`, with every console line timestamped (`p4_isolated_detector_console.txt`);
- the copy reads its own pause file and writes its own state file (`ROOT`-relative paths), so it never touched the repo's `notes/_failure_state.json`.

Over its full 900 s run, the isolated copy's console contains exactly four `FAILED`/`CLEARED` lines: the two deliberate pauses in §4.1 and §4.2. None concern any other node.

Each pause and resume went through the repo's CLI, `python twin/failure_detector.py --pause|--resume <node>`, which affects Harrison's publisher and detector. The same command was run against the copy, except in §4.3. Every 2–3 s the harness recorded both detectors' state for the node and Ditto's value and `_modified` for it.

### 4.1 `node_3`, synthetic (`p4_node3_run.txt`, `p4_node_3_samples.jsonl`)

Timeline:

| Time (Z) | Event |
|---|---|
| 21:22:21.363 | Last publish before the pause: cpu 30.09, mem 55.42, conns 48, latency 39.17 |
| 21:22:21.776 | **Pause** written, 0.4 s after that publish |
| 21:22:21.363 → 21:23:12.266 | Ditto's `_modified` frozen, i.e. the publisher skipped the node |
| 21:22:31.719 | Isolated console: `FAILED hnguyen.clustertwin:node_3: no real update for 10.9s; predicting (log-only) from 8 real samples`, 9.9 s after the pause |
| 21:22:32.491 | Harrison's detector also `failed`, age 10.39 s |
| 21:23:07.675 | **Resume**, 45.9 s after the pause |
| 21:23:10.046 | Isolated console: `CLEARED failed: hnguyen.clustertwin:node_3 (updates resumed)` |
| 21:23:12.266 | First new publish after resume |
| 21:23:12.675 | Harrison's detector no longer `failed` |

**Logged predictions** (isolated copy, 9 console lines):

| Feature | Range over the pause | Last value |
|---|---|---|
| cpu | 46.94 → 53.27, then back to 46.37 | 46.37 |
| mem | 32.65–37.16 | 37.16 |
| conns | 58.60–69.29 | 58.60 |
| latency | 30.21–31.02 | 30.21 |

Harrison's detector logged different predictions for the same pause (cpu 43.19–49.99, mem ≈44.0–44.2), because it fits its own history buffer. The isolated copy had been running only 35 s and used 8 samples.

Two observations:
- **The reported "last known real value" was not the value Ditto held.** Throughout the pause, both detectors reported `last_known` = {cpu 40.02, mem 38.96, conns 71, latency 14.24}. That was an earlier publish: the 21:22:21.363 value landed after their last poll and before the pause took effect. Ditto held {30.09, 55.42, 48, 39.17} the whole time.
- **The isolated detector cleared on a pre-pause value.** It printed `CLEARED … (updates resumed)` at 21:23:10.046, 2.2 s before the first genuinely new publish. Its first poll after resume saw the 21:22:21.363 value, which differed from its stale `last_known`.

### 4.2 `node_0`, real host telemetry (`p4_node0_run.txt`, `p4_node_0_samples.jsonl`, `p4_node0_error_summary.txt`)

As well as reading state, the harness took an **independent real reading at every sample**. It called the publisher's unmodified `measure_host()` in its own process, which has its own `cpu_percent` window, after `prime_host_probes()`. This follows the method of `PREDICTOR_VALIDATION.md` §2026-09-22: predictions compared only with real readings.

Timeline:

| Time (Z) | Event |
|---|---|
| 21:24:05.239 | Last publish before the pause: cpu 47.3, mem 89.9, conns 76, latency 23.49. **Both detectors' `last_known` matched this value exactly.** |
| 21:24:10.590 | **Pause**, 5.3 s after that publish |
| 21:24:22.272 | Isolated console: `FAILED hnguyen.clustertwin:node_0: no real update for 11.6s; predicting (log-only) from 10 real samples`, 11.7 s after the pause |
| by 21:24:23.093 | Harrison's detector `failed` (first sample to show it) |
| 21:24:22.483 → 21:25:26.645 | 12 prediction lines logged by the isolated copy; see the series below |
| 21:25:27.256 | **Resume**, 76.7 s after the pause |
| 21:25:28.260 | Immediate independent real reading: cpu 37.0, mem 96.5, conns 68, latency 9.59 |
| 21:25:29.524 | First new publish after resume: cpu **60.4**, mem 96.3, conns 68, latency 29.31 |
| 21:25:30.138 | Isolated console: `CLEARED failed: hnguyen.clustertwin:node_0 (updates resumed)`, 0.6 s after that genuinely new publish |
| by 21:25:33.900 | Harrison's detector no longer `failed` |

**Isolated copy's predictions:**
- **cpu** extrapolated down from 44.65 to 41.62 (at 21:24:59.744), then moved back up: 42.72 → 43.80 → 44.11. The predictor docstring says predictions decay toward the window mean once past one window width.
- **mem** 90.33 → 91.20 → 90.49
- **conns** 75.32 → 78.99 → 75.98
- **latency** 19.43 → 21.77 → 19.85

One gap between consecutive console lines was 12.2 s (21:24:59.744 → 21:25:11.938). It occurred while the machine was at 77–94% CPU.

**Real host behaviour during the pause:** independent readings showed CPU spikes to 84.7 (21:24:28) and 87–94 (21:24:59–21:25:14), and memory rising from 90.1 to 97.5.

**Error of prediction minus independent real reading**, n = 21 paused samples with a prediction:

| Feature | Mean \|error\| | Error range | Predicted range | Real range |
|---|---:|---|---|---|
| cpu | 19.17 | −52.10 … +1.25 | 41.62–44.65 | 41.0–94.0 |
| mem | 2.49 | −6.30 … +0.23 | 90.33–91.20 | 90.1–97.5 |
| conns | 8.92 | +1.32 … +17.02 | 75.32–78.99 | 61–77 |
| latency | 10.39 | −11.70 … +12.84 | 19.43–21.77 | 8.6–31.34 |

**Last prediction before resume vs. the real value after resume:**

| Comparison | cpu | mem | conns | latency |
|---|---:|---:|---:|---:|
| Isolated copy's last prediction {43.80, 90.58, 76.36, 20.09} minus immediate reading | +6.80 | −5.92 | +8.36 | +10.50 |
| Harrison's detector's last prediction {44.02, 90.50, 75.78, 18.03} minus immediate reading | +7.02 | −6.00 | +7.78 | +8.44 |
| Isolated copy's last prediction minus the first post-resume value in Ditto | −16.6 | −5.72 | +8.36 | −9.22 |

**The first post-resume CPU value (60.4) averages the whole pause.** `publisher.py`'s `measure_host()` docstring says the first reading after a pause averages over the whole pause, and 60.4 is higher than both the immediate reading (37.0) and most mid-pause readings — consistent with that average including the spikes.

**Limits of this comparison** (same caveats as `PREDICTOR_VALIDATION.md` §2026-09-22):
- It is the same machine measuring itself.
- The independent reader's CPU window differs from the publisher's.
- A pause is a simulated failure.
- Each comparison is against one noisy instant.

### 4.3 A node whose data stops arriving, without the detector being told (`p4_node5_unannounced_stall.txt`)

`node_5` was paused in the **real** pause file only. Harrison's publisher therefore stopped writing it, but the isolated detector's pause file stayed `[]`.

| Time (Z) | Observed |
|---|---|
| 21:26:12.456 | Last `node_5` publish |
| 21:26:20.249 | Pause (real file only) |
| 21:26:24 → 21:26:58 | Ditto `_modified` stayed at 21:26:12.456. The isolated detector showed `failed=False`, `age=0.0` at every sample. Harrison's detector, which reads the real pause file, showed `failed=True` from 21:26:36 (age 17.39). |
| 21:26:58.563 | Resume, 38.3 s after the pause |
| 21:27:03.733 | Next `node_5` publish, 51.3 s after the previous one |
| 21:27:11.132 | Harrison's detector no longer `failed` |

**No new data for `node_5` for 51.3 s, and the isolated detector never marked it failed.** Its console has no `node_5` lines at all. Harrison's detector marked it failed only because the pause file told it to.

`twin/README.md` states: "If publisher is stopped, the detector will `FAILED` every node after the tolerance window." What was observed here, for one node, is different. The detector's liveness timer advanced on every successful `GET`, whether or not the data behind it was new (`failure_detector.py`, the `last_seen[node_id] = now` path). Stopping the whole publisher was not exercised (see the last section).

---

## Part 5 — Grafana and the outbound pipeline

### 5.1 Grafana, without a login (`p5_grafana_access.txt`)

| Call (no credentials) | Status |
|---|---|
| `GET http://localhost:3000/api/health` | 200: `{"database":"ok","version":"12.3.0",…}` |
| `GET /api/search` | **401** `{"message":"Unauthorized","messageId":"auth.unauthorized",…}` |
| `GET /api/dashboards/uid/clustertwin-main` | **401**, same body |
| `GET /d/clustertwin-main/cluster-twin-compute-monitoring` | **302** → `/login?redirectTo=…` |

Also checked, all without a login:
- **Pod containers:** `grafana-sc-datasources`, `grafana-sc-plugins`, `grafana`.
- **ConfigMaps whose names contain "grafana" or "dashboard":** `opentwins-enable-grafana-plugin`, `opentwins-grafana`, `opentwins-influxdb2-grafana-datasource`. None contains a dashboard.
- **Grafana container:** `sqlite3` is not present, and `/var/lib/grafana/grafana.db` exists.

**Consequence:** whether each panel renders in the Grafana UI, and the live dashboard's current definition, were **not observed**.

### 5.2 Which feature types appear on a panel (`p5_panel_fields.txt`)

Two dashboard definitions could be read, and neither is the current live one:
- the committed `grafana/dashboard.json` (last commit `4525256`, 2026-08-13);
- `notes/_grafana_dashboard_get.json`, a saved `GET` of the live dashboard with `meta.updated` 2026-07-28T13:50:43Z and version 1. `README.md` recorded live version 5 on 2026-08-25, so this copy is older than the live version.

In **both**, the four panels query these InfluxDB fields:

| Panel | Type | Title | Field queried |
|---|---|---|---|
| 1 | timeseries | Per-node CPU utilization (all 10 nodes) | `cpu_utilization` |
| 2 | timeseries | Per-rack avg CPU | `avg_cpu_utilization` |
| 3 | timeseries | Cluster avg CPU (from racks) | `avg_cpu_utilization` |
| 4 | table | Current node state (sorted by CPU descending) | `cpu_utilization` |

| Feature type | On any panel in either definition? |
|---|---|
| `cpu_utilization` | yes |
| `memory_utilization` | no |
| `active_connections` | no |
| `latency_ms` | no |
| `avg_cpu_utilization` | yes |
| `total_active_connections` | no |
| `healthy_node_count` | no |

Whether the *current* live dashboard (version 5 or later) differs from these was not verifiable without a login.

### 5.3 The panels' own queries, run against InfluxDB (`p5_panel_queries_and_freshness.txt`)

Each committed panel's Flux query was run as-is against InfluxDB at 21:28:31Z, with `v.timeRangeStart = -15m` (the JSON's default range) and `v.windowPeriod = 10s`. This is the data Grafana would draw, not Grafana itself.

| Panel | Result |
|---|---|
| 1 | OK in 2.09 s; 10 series (`node_0`…`node_9`), 851 rows |
| 2 | OK in 0.10 s; 2 series (`rack_0`, `rack_1`), 110 rows |
| 3 | OK in 0.07 s; 1 series (`main_cluster`), 54 rows |
| 4 | OK in 1.06 s; 10 rows, one per node |

### 5.4 End-to-end freshness: Ditto → InfluxDB (`p5_freshness.json`)

**Method:**
- no writes of this session's own;
- wait for a genuinely new publisher write to `node_7` or `node_0` in Ditto (polled every 0.2 s);
- then poll InfluxDB every 0.5 s until a point with that exact CPU value appears for that `thingId`.

| Node | Ditto `_modified` | cpu | Influx point `_time` | First visible in a query | Visible − Ditto |
|---|---|---:|---|---|---:|
| node_7 | 21:29:03.484 | 53.23 | 21:29:03.497 | 21:29:08.838 | 5.35 s |
| node_0 | 21:29:16.443 | 48.4 | 21:29:16.519 | 21:29:19.220 | 2.78 s |
| node_7 | 21:29:25.176 | 26.46 | 21:29:25.187 | 21:29:28.689 | 3.51 s |
| node_0 | 21:29:38.654 | 46.2 | 21:29:38.813 | 21:29:48.778 | 10.12 s |
| node_7 | 21:29:55.131 | 34.89 | 21:29:55.227 | 21:29:58.672 | 3.54 s |
| node_0 | 21:29:59.652 | 45.1 | 21:29:59.662 | 21:30:09.430 | 9.78 s |
| node_7 | 21:30:16.922 | 40.78 | 21:30:17.002 | 21:30:18.676 | 1.75 s |
| node_0 | 21:30:21.120 | 53.0 | 21:30:21.148 | 21:30:28.620 | 7.50 s |

Summary, n = 8:
- **Ditto write → visible in an InfluxDB query:** min 1.75 s, median 5.35 s, max 10.12 s. Polling granularity adds up to about 0.5 s plus query time.
- **Influx point `_time` − Ditto `_modified`:** 0.010–0.159 s. The point is timestamped almost immediately; what varies is when it becomes queryable.

**Live Telegraf configuration**, from the `telegraf.conf` key of ConfigMap `opentwins-telegraf-real-config`, reading only the interval lines (`p5_telegraf_and_cleanup.txt`): `interval = "10s"`, `flush_interval = "10s"`, `flush_jitter = "0s"`, `collection_jitter = "0s"`, `round_interval = true`. The observed 1.75–10.12 s visibility range falls within one 10 s flush interval.

**Grafana panel lag** (InfluxDB → drawn on a panel) was **not measured**; it needs a login. The committed JSON sets `refresh: "5s"`, but the live dashboard's refresh setting was not readable.

---

## What wasn't exercised and why

- **`scripts/fault_tolerance_test.py` was not run.** It deletes a Ditto pod, and this repo's history records it crashing the publisher and aggregator as a side effect. Not run, per this task's boundary.
- **`notes/scaling_benchmark.py` was not run.** It overwrites `notes/scaling_results.json`, and repo history associates concurrent-load runs with InfluxDB crash-loops. Not run, per this task's boundary.
- **Anything behind a Grafana login was not exercised:** panel rendering in the UI, the live dashboard's current definition and version, live alert rules, and InfluxDB-to-panel lag. This session does not enter passwords. `grafana.db` was not copied, because it holds credential material. `sqlite3` is absent from the container, so it could not be queried in place.
- **Extended API write endpoints were not called**, apart from the probe's create, link, unlink and delete. That excludes `PUT /api/twins/fix`, `PUT /api/types/fix`, `POST …/duplicate/…`, type link/unlink, and connection open/close/delete/POST. They would modify real twins, types, or the live MQTT connection.
- **Cardinality was tested only at the node level.** A third rack under the cluster (ClusterType cardinality 2) was not tried. `notes/twin_design_spec.md` says create-from-type on RackType recursively instantiates child NodeType twins, so that test would not have been cleanly reversible.
- **Extended API connection endpoints** returned 401 with `ditto:ditto` and were not retried with other credentials.
- **Harrison's running detector's console output** runs in a separate window this session cannot read. Its state file was read instead, and console output came from the isolated copy (§4).
- **MongoDB:** pod state only. It is not port-forwarded and the repo does not use it directly.
- **Stopping the whole publisher, a Ditto outage, or a port-forward dying** were not exercised. Each would mean stopping Harrison's processes or cluster components. §4.3 covers one node's data stopping, not the publisher process stopping.
- **The detector's no-PATCH invariant** was not re-checked against the nginx access log in this session.
- **Probe residue in InfluxDB:** the temporary probe's Ditto events reached InfluxDB (`thingId=hnguyen.clustertwin:zz_cardinality_probe`). Deleting points from InfluxDB was not attempted.
