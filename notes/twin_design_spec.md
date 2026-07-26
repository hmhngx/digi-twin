# Cluster Twin Design Spec

Academic proof-of-concept digital twin on an already-deployed OpenTwins stack
(Eclipse Ditto, Extended API, Mosquitto, Telegraf, InfluxDB, Grafana). No Hono, no Kafka.

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

**This build (verified):** per-node last-update timer (~3 s publish interval +
9 s tolerance). Pause a node via `notes/_paused_nodes.json` (publisher skips it).
On timeout the detector logs `FAILED` and holds last-known feature values
unchanged. On resume it logs `CLEARED`.

Verified run: paused `hnguyen.clustertwin:node_1` →
`FAILED ... holding last-known values cpu=26.1 latency=25.45` → resumed →
`CLEARED failed: hnguyen.clustertwin:node_1 (updates resumed)`.

**Paper:** Kafka-ML predicts a plausible next value from historical data and
writes that predicted state into the twin (full Hono→Kafka-ML→Ditto lifecycle).

**What I'd build next with more time:** replace hold-last-value with a small
online predictor (even a rolling mean / ARIMA) fed from Influx history, then
optionally wire a real streaming ML path if Kafka is added to the deployment.

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

Script: `scripts/port-forwards.ps1`
