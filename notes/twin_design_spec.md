# Cluster Twin Design Spec

Academic proof-of-concept digital twin on an already-deployed OpenTwins stack
(Eclipse Ditto, Extended API, Mosquitto, Telegraf, InfluxDB, Grafana). No Hono, no Kafka.

**Last live-verified: 2026-08-25.** Operator run, ports, Grafana login, and live datasource UID belong in the repo-root [README.md](../README.md). This file is the type/instance and pipeline design. Historical “verified run” anecdotes below were **not** re-executed that day unless the root README says so.

## Namespace

Every Thing ID uses one namespace:

`hnguyen.clustertwin:<name>`

Examples: `hnguyen.clustertwin:main_cluster`, `hnguyen.clustertwin:rack_0`,
`hnguyen.clustertwin:node_0`. Type IDs follow the same rule:
`hnguyen.clustertwin:NodeType`, `hnguyen.clustertwin:RackType`,
`hnguyen.clustertwin:ClusterType`.

Verified against the Extended API live Swagger (`http://localhost:8080/docs`) and
its unit tests inside the `opentwins-ditto-extended-api` container: Thing IDs are
always `namespace:name`. Bare names like `node_0` are invalid for Ditto lookups.

## Instance hierarchy (three levels, compositional)

```
hnguyen.clustertwin:main_cluster
├── hnguyen.clustertwin:rack_0
│   ├── node_0 … node_4
└── hnguyen.clustertwin:rack_1
    ├── node_5 … node_9
```

This mirrors the paper's Factory → Robot → Sensor example. Parent-child links are
established through the Extended API (`PUT /api/twins/{parent}/children/{child}`),
not by name-prefix grouping. Verification uses the restricted attribute `_parents`
(string on twins) and `GET /api/twins/{id}/children`.

## Attributes vs features

Per Eclipse Ditto's Thing model: attributes are relatively static metadata;
features hold dynamic telemetry/state.

| Twin    | Attributes (static)                         | Features (dynamic)                                              |
|---------|---------------------------------------------|-----------------------------------------------------------------|
| Node    | hardware_model, rack_position, install_date | cpu_utilization, memory_utilization, active_connections, latency_ms |
| Rack    | datacenter_zone                             | avg_cpu_utilization, total_active_connections, healthy_node_count   |
| Cluster | cluster_name, region                        | avg_cpu_utilization, total_active_connections, healthy_node_count   |

Feature JSON shape (OpenTwins / Ditto convention):

```json
"cpu_utilization": { "properties": { "value": 0.0 } }
```

## Twin Types (cycle-free graph)

- `hnguyen.clustertwin:NodeType`
- `hnguyen.clustertwin:RackType` — parent of NodeType, cardinality **5**
- `hnguyen.clustertwin:ClusterType` — parent of RackType, cardinality **2**

On types, `_parents` is a map `{ "<parentTypeId>": <cardinality> }`.
On twins, `_parents` is a single parent Thing ID string.

**Why shared NodeType matters:** type membership and the instance parent-child tree
are independent concerns. All ten node twins are instances of the same NodeType,
even though they hang under two different rack parents. That is the paper's point
about Twin Types as a reusable, cycle-free schema graph (Fig. 2b) rather than a
1:1 mirror of one instance tree. Two nodes in different racks share identical type
structure without sharing an instance parent.

### Instantiation note (verified Extended API behavior)

`POST /api/types/{typeId}/create/{twinId}` recursively instantiates linked child
types (see `duplicateThingRecursive` in the Extended API). To get explicit IDs
(`rack_0`, `node_0`, …) we: (1) link types and verify the type graph, (2) temporarily
unlink type children, (3) instantiate each twin from its own type, (4) link twin
children by hand, (5) re-link the type graph. This preserves both compositional
guarantees and the intended instance IDs.

## Aggregation rules

- Healthy node threshold: **CPU utilization &lt; 80%**
- Each rack recomputes from its five child nodes:
  - `avg_cpu_utilization` = mean of child CPU values
  - `total_active_connections` = sum of child connections
  - `healthy_node_count` = count of children with CPU &lt; 80
- Cluster recomputes from its **two rack twins only** (never re-reads all 10 nodes):
  - `avg_cpu_utilization` = mean of the two rack averages
  - `total_active_connections` = sum of rack totals
  - `healthy_node_count` = sum of rack healthy counts

## Ingestion and event export

### Inbound (simplified — deployment necessity)

Publisher → direct HTTP PATCH to Ditto Things API
(`http://localhost:8081/api/2/things/{thingId}/features`) as `ditto:ditto`.

No MQTT source connection is configured in Ditto. This deployment has no Eclipse
Hono and no Kafka. Defense wording:

> My deployment's inbound ingestion is simplified — direct HTTP to Ditto rather
> than the paper's Hono→Kafka pipeline. The outbound event export to Grafana,
> however, uses the platform's real MQTT/Telegraf plumbing, not a workaround.

### Outbound (real platform path)

Ditto target connection → Mosquitto topic under `opentwins/#` → existing Telegraf
→ InfluxDB (`org=opentwins`, `bucket=default`) → Grafana.

### Phase 5 appendix — Telegraf format and Ditto connection (verified live)

**Telegraf** (`configmap/opentwins-telegraf-real-config`):

- MQTT input topic: `opentwins/#`
- `data_format = "json_v2"`
- Tags from Ditto Protocol envelope: `extra.thingId` → `thingId`,
  `extra.attributes._parents` → `parent`, `headers.correlation-id` → `correlationId`,
  `headers.ditto-originator` → `originator`
- Objects: `{value}` and `value.features` flattened into Influx fields such as
  `value_cpu_utilization_properties_value`
- Output: InfluxDB v2 `org=opentwins`, `bucket=default`

**Manual MQTT test:** published a Ditto-protocol JSON message to `opentwins/test`;
Influx received measurement `mqtt_consumer` with matching `correlationId` / `thingId`.

**Working Ditto target connection** (created via `POST /api/2/connections` as
`devops:foobar`; ID is generated by Ditto — do not set `id` in the body):

```json
{
  "name": "ClusterTwin MQTT events to Mosquitto",
  "connectionType": "mqtt",
  "connectionStatus": "open",
  "uri": "tcp://opentwins-mosquitto:1883",
  "sources": [],
  "targets": [{
    "address": "opentwins/hnguyen.clustertwin/{{ thing:name }}",
    "topics": [
      "_/_/things/twin/events?extraFields=thingId,attributes/_parents,features/idSimulationRun/properties/value"
    ],
    "authorizationContext": ["nginx:ditto"],
    "qos": 1
  }],
  "specificConfig": {
    "clientId": "ditto-clustertwin-mqtt",
    "cleanSession": "true"
  }
}
```

**E2E proof:** publisher PATCH of `hnguyen.clustertwin:node_0` produced Influx points
with `thingId=hnguyen.clustertwin:node_0`, `parent=hnguyen.clustertwin:rack_0`,
topic `opentwins/hnguyen.clustertwin/node_0`, field
`value_cpu_utilization_properties_value`. Connectivity logs show
`things.events:thingMerged` published to that MQTT topic.

## Failure detection (Phase 6) vs paper Section 3.3

**Through 2026-09-14 (superseded, kept for history):** per-node last-update
timer (~3 s publish interval + 9 s tolerance), exact-signature equality. On
timeout the detector logged `FAILED` and held last-known feature values
unchanged, passively (the publisher skipping paused IDs, not an active write).
Verified run: paused `hnguyen.clustertwin:node_1` →
`FAILED ... holding last-known values cpu=26.1 latency=25.45` → resumed →
`CLEARED failed: hnguyen.clustertwin:node_1 (updates resumed)`.

**2026-09-15, superseded same-day:** a pass driven by
[`notes/EXPERIMENTAL_TWIN_REQUIREMENTS.md`](EXPERIMENTAL_TWIN_REQUIREMENTS.md)
made "unchanged" a tolerance-band comparison (kept, see below) and had the
detector PATCH its prediction back to Ditto every cycle. The write-back half
was deliberately reverted the same session, before being relied on for
anything: it flips a property (`twin/failure_detector.py` never PATCHes) this
repo's own audit trail had independently verified and cited multiple times,
and that's a decision that needs its own explicit go-ahead, not something to
fold silently into "add a predictor." Real numbers from that reverted version
(it did work) are preserved in git history for the record, not in this file.

**As of 2026-09-16, current behavior:** "unchanged" is a tolerance-band
comparison, not exact equality — exact equality was dormant against synthetic
`random.uniform()` data but would misfire on a real sensor sitting at physical
equilibrium. On timeout, the detector fits a per-feature linear regression
over each node's own rolling buffer of recent *real* samples (frozen the
instant the node goes quiet, extrapolation capped at one window-width past the
last real sample so a long failure settles rather than drifting toward a
clamp), via [`twin/predictor.py`](../twin/predictor.py). **The prediction is
logged only** — `notes/_failure_state.json` and the console, alongside the
actual last-known real value — and never reaches Ditto or Grafana. See
[`notes/PREDICTOR_VALIDATION.md`](PREDICTOR_VALIDATION.md) for the validation
run (a full `node_0` ramp cycle with measured error at several checkpoints,
plus a long-window distributional check on three noise nodes) and the
explicit no-PATCH-invariant verification.

**Paper:** Kafka-ML predicts a plausible next value from historical data and
writes that predicted state into the twin (full Hono→Kafka-ML→Ditto lifecycle).
This repo's predictor matches the "predict on timeout" shape using ordinary
least-squares instead of a trained model (no historical training corpus
exists here to fit one against), but deliberately does not match the
"writes it into the twin" half yet.

**Same-day addendum, 2026-09-16 (after this section was first written):** past the extrapolation cap, recovery is conditional on node category, not uniform. `node_0` (a known deterministic formula, `_is_structured()` in `twin/predictor.py`) stays frozen at the capped value — decaying it toward its own training-window mean was tried and measured worse (error grew unboundedly on a node that's still genuinely trending). Every noise node (`node_1`–`node_9`) decays the frozen value toward the training window's empirical mean instead of sitting at a fixed number indefinitely. See `notes/PREDICTOR_VALIDATION.md`'s later sections for the measured arc behind this branch.

**Known limitation, found during verification:** on a node with no real trend
(9 of the 10 nodes are pure `random.uniform` noise with no temporal structure
— see `EXPERIMENTAL_TWIN_REQUIREMENTS.md` section 2.3), a small residual slope
estimated from a real but finite sample is small but not exactly zero (least
squares on i.i.d. data has no reason to land on precisely 0), which can pull
the prediction measurably off the true sample mean before the extrapolation
cap and feature clamp bound it. This is expected behavior for least-squares on
a finite noisy sample, not a bug — the fidelity claim this repo can actually
back up on those nine nodes is distributional (stays in range, approximates
the mean over many samples), not point-wise accuracy at any one instant. See
`PREDICTOR_VALIDATION.md` for the measured numbers.

## Auth

- Direct Ditto HTTP: Basic Auth `ditto:ditto` (subject `nginx:ditto`)
- Do **not** use `devops:foobar` against per-Thing policies unless a policy is
  explicitly updated to grant `nginx:devops` (optional stretch; not default)

Shared policy for this PoC: `hnguyen.clustertwin:basic_policy` granting
`nginx:ditto` READ/WRITE on `thing:/`, `policy:/`, `message:/`.

## Explicitly out of scope

1. **Inbound Hono→Kafka pipeline** — not present in this Helm deployment; inbound
   uses direct Ditto HTTP PATCH. Outbound MQTT/Telegraf path is real.
2. **3D / Unity visualization** — Unity model, WebGL export, Grafana Unity panel.
3. **Full Kafka-ML production ML pipeline** — Hono→Kafka-ML→Ditto, RabbitMQ/AMQP
   bridge, MongoDB topic/device mappings. Phase 6 is a simplified stand-in.

Missing or simplified pieces are due to time allocation and deployment constraints
for a pre-semester proof-of-concept, not lack of understanding of the paper.

## Local access (port-forwards)

| Service       | URL / endpoint              |
|---------------|-----------------------------|
| Extended API  | http://localhost:8080       |
| Ditto HTTP    | http://localhost:8081       |
| Grafana       | http://localhost:3000       |
| InfluxDB      | http://localhost:8086       |
| Mosquitto     | localhost:1883              |

Script: `scripts/port-forwards.ps1`. Confirm LISTENING ports plus HTTP, not the script banner. Grafana’s **live** Influx datasource UID is generated by Grafana (2026-08-25: `P4528D75AB74BE2EA`, name still `opentwins`); [grafana/dashboard.json](../grafana/dashboard.json) uses UID `opentwins` and must be remapped on import. See root README.
