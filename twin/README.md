# Cluster Twin (OpenTwins PoC)

Simulated compute-cluster digital twin on an already-deployed OpenTwins stack
(Eclipse Ditto, Extended API, Mosquitto, Telegraf, InfluxDB, Grafana).

Three-level compositional hierarchy (not a flattened name-prefix group):

```
hnguyen.clustertwin:main_cluster
  ├── rack_0 → node_0 … node_4
  └── rack_1 → node_5 … node_9
```

Twin Types: `NodeType`, `RackType` (cardinality 5), `ClusterType` (cardinality 2).

## Design summary

See [notes/twin_design_spec.md](notes/twin_design_spec.md) for attributes vs features,
type vs instance composition, aggregation rules (healthy CPU threshold = 80%),
and the verified Ditto→MQTT→Telegraf→Influx connection.

**Update path you must be able to explain from memory:**

1. `publisher.py` PATCH → Ditto Things API (`ditto:ditto`)
2. Ditto target connection publishes Thing events to `opentwins/hnguyen.clustertwin/<name>`
3. Telegraf (`json_v2` on `opentwins/#`) → InfluxDB (`org=opentwins`, `bucket=default`)
4. `aggregator.py` recomputes rack features from child nodes, then cluster features
   from the **two rack twins only** (never skips the rack level)

## Prerequisites

- Minikube with OpenTwins Helm release already running
- `kubectl` context pointing at that cluster
- Python 3.11+ on Windows
- Port-forwards (see below)

## Cold start reproduce (Windows)

```powershell
minikube start
# wait until OpenTwins pods are Ready
kubectl get pods

cd "C:\Users\minhh\Side Hustles\digi-twin"
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# Set INFLUX_TOKEN from the live Telegraf ConfigMap token field:
#   kubectl get configmap opentwins-telegraf-real-config -o yaml

.\scripts\port-forwards.ps1
# keep forwards alive; in a new terminal:

python scripts\setup_types_and_twins.py
python scripts\phase5_mqtt_influx.py create-connection

# three processes (three terminals)
python twin\publisher.py
python twin\failure_detector.py
python twin\aggregator.py
```

Grafana: http://localhost:3000 — import [grafana/dashboard.json](grafana/dashboard.json).
If the Influx datasource UID is not `opentwins`, use Grafana's import dialog to
remap the datasource to the provisioned OpenTwins InfluxDB source.

Optional checks:

```powershell
python scripts\phase5_mqtt_influx.py manual-test
python notes\scaling_benchmark.py
python scripts\fault_tolerance_test.py
```

## Credentials

- Ditto HTTP / Extended API: `ditto:ditto` (subject `nginx:ditto`)
- Do **not** use `devops:foobar` against per-Thing policies
- Connection management (`POST /api/2/connections`) uses `devops:foobar` (Connectivity API)

## Out of Scope

1. **Simplified inbound ingestion path (by deployment necessity, not by design choice):**
   The paper routes inbound telemetry through Eclipse Hono → Kafka. This deployment
   has no Hono and no Kafka; the publisher uses direct Ditto HTTP PATCH.
   Outbound event export to Grafana uses the platform's real MQTT/Telegraf plumbing,
   not a workaround.

   Defense wording: "My deployment's inbound ingestion is simplified — direct HTTP
   to Ditto rather than the paper's Hono→Kafka pipeline. The outbound event export
   to Grafana, however, uses the platform's real MQTT/Telegraf plumbing, not a
   workaround."

2. **3D/Unity visualization** — Unity model, WebGL export, Grafana Unity panel.
   Scoped out for time / skill focus.

3. **Full Kafka-ML production ML pipeline** — Hono→Kafka-ML→Ditto, RabbitMQ/AMQP
   bridge, MongoDB topic/device mappings. Phase 6 failure detector is a simplified
   hold-last-value stand-in, not the paper's predictive ML lifecycle.

If asked why these are missing: time allocation and deployment constraints for a
pre-semester proof-of-concept — not lack of understanding. The design spec states
exactly what the real version would require.

## Repo layout

| Path | Role |
|------|------|
| `twin/publisher.py` | Inbound telemetry (HTTP PATCH) |
| `twin/failure_detector.py` | Simplified failure detection |
| `twin/aggregator.py` | Recursive rack→cluster aggregation |
| `scripts/setup_types_and_twins.py` | Phases 2–3 |
| `scripts/phase5_mqtt_influx.py` | Outbound connection + MQTT test |
| `scripts/fault_tolerance_test.py` | Pod-kill recovery timing |
| `notes/scaling_benchmark.py` | Paper-style scaling tests |
| `grafana/dashboard.json` | Portable dashboard |
