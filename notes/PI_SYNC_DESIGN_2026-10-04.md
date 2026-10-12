> **2026-10-11 — partially superseded by `notes/PI_TELEMETRY_SPEC_2026-10-11.md`.** The transport, silence-detection, Type and file-name questions below are now decided there, and §14 of that spec lists four corrections to this note (the board is a Pi 3 B+, not a Pi 5; the "emits 0.0" and "no slot" wording; `patch_node` cannot be imported from `twin.publisher`). The text below is left as written on 2026-10-04.

# Pi sync design — 2026-10-04

Design only: no code written, nothing run, no Pi exists. Basis: the 2026-10-01 call as relayed by Harrison (no transcript in the repo). Deliberately departs from `pi_rtt_measurement.py` and `HARDWARE_MEASUREMENT_PLAN.md` (a): the Pi never talks to Ditto.

## What node_0 measures today (read from the code)
`measure_host()`, `twin/publisher.py:90-140`: `cpu_utilization` (psutil, :128), `memory_utilization` (psutil, :129), `active_connections` (ESTABLISHED TCP count, :131-136), `latency_ms` (timed TCP connect to 1.1.1.1:443 by default, :137-139, `config.py:30-32`). A failed probe is omitted, never replaced (:118-125). No SoC-temperature or battery code exists in `twin/` or `scripts/`.

## (a) `twin/pi_transmitter.py` — on the Pi
Every ~3 s (`PUBLISH_INTERVAL_SEC` default, `config.py:23`) read:
- **CPU:** first line of `/proc/stat`, delta of two reads; busy = total − idle − iowait, guest excluded (psutil's formula, pinned 7.2.2, `psutil/__init__.py:1733-1765`). The first read has no baseline: omit CPU that cycle (`publisher.py:54-67`); `pi_idle_jitter_measurement.py:78-79` emits 0.0 instead, so don't copy it.
- **Memory:** `/proc/meminfo`, `100*(MemTotal−MemAvailable)/MemTotal`; if `MemAvailable` is missing, omit (that script's `MemFree` fallback, :68, changes the definition).
- **SoC temp:** `/sys/class/thermal/thermal_zone0/temp` (millidegrees C, ÷1000); at start check `.../thermal_zone0/type` is the SoC.

Standard Linux paths, but **unverified on a Pi 5**. A failed read omits that feature and logs a WARN, as `measure_host()` does. Message = measured features + sequence number + Pi-side timestamp.

**Transport — open, not chosen:**

| Option | For | Against |
|---|---|---|
| TCP/HTTP over Wi-Fi | stdlib only; simplest | new LAN listener on the laptop; no auth unless hand-added; firewall setup |
| MQTT over Wi-Fi | `paho-mqtt` already in `requirements.txt`; reconnect/QoS/auth | needs a broker the Pi can reach (not verified for the current one); `publisher.py:5` says "No MQTT inbound"; extra hop |
| Bluetooth | no LAN exposure | new dependency both ends (none in repo); pairing/range; Windows support unverified; untestable without a Pi |

## (b) `twin/pi_receiver.py` — on the laptop
Per message: keep only whitelisted feature names with numeric values, build `{name: {"properties": {"value": v}}}` as `node_features()` does (`publisher.py:153`), call `patch_node(PI_THING_ID, features)` imported from `twin.publisher` (:156-165), which already wraps `ditto()`/`enc()` (`ditto_client.py:34`, `:23`). No features, no PATCH. `patch_node` raises on HTTP ≥ 400 (:163-164) and `ditto()` has no try/except (`ditto_client.py:40`): catch both and keep listening.
Hazards: (1) the receiver is itself a listener holding the `ditto:ditto` credential (`ditto_client.py:16`), so transport auth is part of the transport decision. (2) If the Pi goes silent, Ditto keeps the last values and `failure_detector.py:148` refreshes liveness on any successful GET of an unpaused node, so a dead Pi looks alive; it also only polls `NODE_IDS` (`config.py:40`). A freshness signal is needed (open).

## (c) Battery — UNRESOLVED, pending Harrison's decision
A mains-powered Pi has no battery to read; no battery probe exists in this repo. Substitution options: a **USB-C power-draw sensor** (measures consumption; how it is read is unknown) or a **UPS/battery HAT** (real battery state; read interface varies, unverified). Neither is chosen; the transmitter has no battery field until then. Cooling-device state is postponed per the call and not designed.

## Type fit (recommendation only; `setup_types_and_twins.py` not edited or run)
`NodeType` defines exactly `cpu_utilization`, `memory_utilization`, `active_connections`, `latency_ms` (:62-67). CPU and memory fit; **SoC temp and battery have no slot**; the other two slots have no Pi counterpart.
**Recommend a new Type** (name TBD) with only the approved features, not extending `NodeType`: it is the shared schema for node_0..9, linked under `RackType` ×5 (:152) and read by name in `aggregator.py:52-53` and `failure_detector.py:86-89`; and `put_type()` has no update path (the comment at :107 says PATCH, none is coded), so if the Extended API rejects a PUT of an existing Type, an edited `NodeType` never reaches the cluster.
That implies a new Thing via `create_twin_from_type` (:127-132), not node_0, else `measure_host()` and the receiver both write node_0. Harrison to confirm.
Not verified: the live-registered Types (cluster not queried); whether Ditto/OpenTwins accepts a PATCH to a feature absent from the Type (nothing in the repo does).

## Prior art (repo-wide grep)
No `pi_transmitter`, `bluetooth` or receiver exists. Closest: `notes/pi_idle_jitter_measurement.py` reads the same three Linux sources; never run on a Pi.
