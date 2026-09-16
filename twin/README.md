# `twin/` package

Python processes for the cluster twin. **How to start Minikube, port-forwards, and Grafana:** see the repo-root [README.md](../README.md). **Type/instance model and aggregation rules:** [notes/twin_design_spec.md](../notes/twin_design_spec.md).

Namespace `hnguyen.clustertwin` (overridable via `.env`). Shared settings: [config.py](config.py). HTTP: [ditto_client.py](ditto_client.py) (`ditto:ditto`).

```
hnguyen.clustertwin:main_cluster
  ├── rack_0 → node_0 … node_4
  └── rack_1 → node_5 … node_9
```

Types: `NodeType`, `RackType` (cardinality 5), `ClusterType` (cardinality 2).

## Processes

Run from the repo root, venv activated, `.env` loaded. **Three terminals** — `publisher.py` never exits.

| Script | Default interval | What it does |
|---|---|---|
| [publisher.py](publisher.py) | 3 s | PATCH node features on Ditto HTTP. No MQTT inbound. |
| [aggregator.py](aggregator.py) | 7 s | PATCH rack features from child nodes, then cluster features from **the two rack twins only**. Healthy = CPU &lt; 80. |
| [failure_detector.py](failure_detector.py) | poll ~2 s, fail after 9 s | Logs `FAILED` / `CLEARED`. **As of 2026-09-16, also logs a predicted value, log-only** (linear extrapolation over the node's own recent real history, via [predictor.py](predictor.py)) to `notes/_failure_state.json` and the console, alongside the actual last-known value — see [notes/PREDICTOR_VALIDATION.md](../notes/PREDICTOR_VALIDATION.md). **Never PATCHes Ditto** (verified there, both by code review and live against the nginx access log). Before 2026-09-15 it only logged and held, passively, via the publisher skipping paused IDs, with no prediction computed at all. |

```powershell
python -u twin\publisher.py
python -u twin\aggregator.py
python -u twin\failure_detector.py
```

Finite runs:

```powershell
python -u twin\publisher.py --cycles 3
python -u twin\aggregator.py --cycles 2
python -u twin\failure_detector.py --duration 20
```

## Publisher details

- Honors `notes/_paused_nodes.json` (skip those Thing IDs).
- `node_0` ramps CPU ~10% → ~95% and latency ~5 → ~120 ms over ~360 s. Other nodes are random in-range noise.
- 2026-08-25: `--cycles 3` → 30× HTTP 204.

## Failure detector CLI

```powershell
python twin\failure_detector.py --pause node_1
python twin\failure_detector.py --resume node_1
```

Short names get prefix `hnguyen.clustertwin:`. Pause/resume only write the JSON file; start publisher + detector separately to observe `FAILED` / `CLEARED`.

State dump: `notes/_failure_state.json` (gitignored).

If publisher is stopped, the detector will `FAILED` every node after the tolerance window. That is expected.

## Out of scope (same as root README)

Direct HTTP inbound instead of Hono→Kafka. Lightweight, log-only linear-regression extrapolation instead of a trained Kafka-ML model that writes its prediction back into the twin (no historical training corpus exists in this repo to train one, and write-back is a deliberately separate, not-yet-taken decision — see [notes/EXPERIMENTAL_TWIN_REQUIREMENTS.md](../notes/EXPERIMENTAL_TWIN_REQUIREMENTS.md) and [notes/PREDICTOR_VALIDATION.md](../notes/PREDICTOR_VALIDATION.md)). No Unity panel. Connectivity API `devops:foobar` is **not** used by these three processes (MQTT connection setup is [scripts/phase5_mqtt_influx.py](../scripts/phase5_mqtt_influx.py)).
