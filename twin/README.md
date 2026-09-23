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

Run from the repo root, venv activated, `.env` loaded. **Three terminals** — `publisher.py` never exits. `publisher.py` needs `psutil` in the venv (pinned in [requirements.txt](../requirements.txt) since 2026-09-22 — `pip install -r requirements.txt`; before that date it was only in the system Python, not the venv).

| Script | Default interval | What it does |
|---|---|---|
| [publisher.py](publisher.py) | 3 s sleep + ten sequential PATCHes (measured 3.9–21.8 s per full cycle, median 4.7 s, on this laptop 2026-09-22) | PATCH node features on Ditto HTTP. **`node_0` = real telemetry from the machine running it** (psutil + a timed TCP connect); `node_1`–`node_9` = synthetic random values. No MQTT inbound. |
| [aggregator.py](aggregator.py) | 7 s | PATCH rack features from child nodes, then cluster features from **the two rack twins only**. Healthy = CPU &lt; 80. |
| [failure_detector.py](failure_detector.py) | poll ~2 s, fail after 9 s | Logs `FAILED` / `CLEARED`. **As of 2026-09-16, also logs a predicted value, log-only** (linear extrapolation over the node's own recent real history, via [predictor.py](predictor.py)) to `notes/_failure_state.json` and the console, alongside the actual last-known value — see [notes/PREDICTOR_VALIDATION.md](../notes/PREDICTOR_VALIDATION.md). **Never PATCHes Ditto** (verified there, both by code review and live against the nginx access log). Before 2026-09-15 it only logged and held, passively, via the publisher skipping paused IDs, with no prediction computed at all. **2026-09-22:** for the now-real `node_0`, the predictor uses physical clamps only (cpu/mem 0–100, conns/latency ≥ 0) and the same decay-toward-mean as every other node. The ramp-era clamps and freeze are gone. It is checked only against later real readings of this machine, which is an imperfect proxy (see PREDICTOR_VALIDATION.md, 2026-09-22). |

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
- **`node_0` — real, as of 2026-09-22.** Every value is measured on the machine running `publisher.py`, fresh each cycle ([`measure_host()`](publisher.py)):

  | Feature | What it actually is |
  |---|---|
  | `cpu_utilization` | `psutil.cpu_percent(interval=None)` — system-wide CPU %, averaged over the whole time since the previous cycle |
  | `memory_utilization` | `psutil.virtual_memory().percent` — physical RAM in use |
  | `active_connections` | count of this machine's TCP connections in state `ESTABLISHED` (IPv4+IPv6, loopback included — so the twin stack's own port-forwards are part of it) |
  | `latency_ms` | a timed TCP connect from this machine to `LATENCY_PROBE_HOST:LATENCY_PROBE_PORT` (default `1.1.1.1:443`) |

  If a probe fails, that feature is **left out of the PATCH** (a `WARN` line is printed) rather than filled with an invented number; a failing latency probe also costs up to `LATENCY_PROBE_TIMEOUT_SEC` (2 s) per cycle. Until 2026-09-22 `node_0` was a synthetic ramp (CPU ~10 → ~95 % over ~360 s); that formula no longer exists.
- **`node_1`–`node_9` — unchanged, still synthetic:** independent `random.uniform`/`random.randint` draws, no physical counterpart.
- **What that makes each node** (Digital Model / Shadow / Twin, by direction of automated data flow — see [notes/DIGITAL_TWIN_ADAPTATION_ANALYSIS.md](../notes/DIGITAL_TWIN_ADAPTATION_ANALYSIS.md) §3): `node_0` is a **Digital Shadow** node (real, automated, one-way — nothing flows back to the machine, so not a Digital Twin). `node_1`–`node_9` are still a **Digital Model**. The system as a whole is **mixed** — one real node, nine synthetic — and is neither "a digital twin" nor "a digital shadow" as a whole. `rack_0` and the cluster average `node_0`'s real CPU together with synthetic nodes, so those aggregates don't shadow anything physical either.
- **Demo-day tradeoff:** `node_0` no longer climbs a clean ramp on camera. It shows whatever this machine is really doing — noisier, often flat, sometimes spiky. That is the point of the change, not a regression. *Suggestion only, not decided:* a live beat of "watch `node_0` react when I load the CPU" could replace the ramp — e.g. `python notes\_node0_cpu_burn.py --seconds 30` (a gitignored local helper) took `node_0`'s CPU from ~58–68 % to 90.3 % and back to ~43 % in the 2026-09-22 verification run. Expect a lag: one publish cycle (median 4.7 s, up to ~22 s under load) plus Telegraf's 10 s flush before Grafana shows it.
- Windows has two CPU counters and they disagree. In the 2026-09-22 run, the twin's `node_0` CPU tracked `\Processor(_Total)\% Processor Time` (0–100), which read 87–100 during the burst while `node_0` read 90.3. `\Processor Information(_Total)\% Processor Utility` is frequency-scaled and read 71–173 on this laptop during the same run. A tool showing the second one will not match the twin's number; which one Task Manager displays on this machine was not checked.
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
