# Pi telemetry sync — specification — 2026-10-11

Partly supersedes `notes/PI_SYNC_DESIGN_2026-10-04.md` (see §14). Written before any code. Nothing described here has run on the real Pi: **real-Pi behaviour is untested until the live bring-up** (`notes/PI_BRINGUP_RUNBOOK_2026-10-11.md`).

Evidence tags used throughout:

- **[device]** read from the real board on 2026-10-11 (capture kept locally in `notes/_pi_runs/`, git-ignored; it holds network addresses, so only facts are cited here). The five sample files are copied into `tests/fixtures/pi3bplus/`.
- **[measured]** computed from this project's own run logs or tests on the date given at each use (the run logs are kept locally in `notes/_pi_runs/`, git-ignored).
- **[docs]** read from official Raspberry Pi documentation on 2026-10-11 (URLs in §11).
- **[repo]** read from this repo at HEAD `35aba11` (file:line).
- **[design]** a decision made in this spec, not a measurement.
- **[unverified]** not checked; say so wherever it is used.

## 1. Architecture and trust boundaries

```
 Raspberry Pi 3 B+                            Laptop (Windows)
 ┌────────────────────┐   plain HTTP, JSON    ┌────────────────────────┐        ┌──────────────┐
 │ pi/pi_sender.py    │ ───────────────────▶  │ twin/pi_receiver.py    │ ─────▶ │ local Ditto  │
 │  reads /proc,/sys  │   Bearer token        │  validates, dedups,    │ PATCH  │ hnguyen.     │
 │  stdlib only       │ ◀───────────────────  │  logs, PATCHes Ditto   │        │ clustertwin: │
 └────────────────────┘   probe replies       └────────────────────────┘        │ pi_0         │
   holds PI_SHARED_TOKEN                        holds PI_SHARED_TOKEN and       └──────────────┘
   knows no Ditto credential                    the ditto:ditto credential
```

- The Pi never talks to Ditto and never holds a Ditto credential [design]. The default `ditto:ditto` credential (`twin/config.py:14-15`) stays on the laptop and must not be exposed to the LAN (`notes/FIDELITY_VALIDATION_PLAN_2026-10-01.md` §3 says the same about rebinding Ditto's port-forward).
- The receiver is the only new network listener and the only holder of both secrets.
- Shared code: `twin/pi_protocol.py` (stdlib only; parsers, message validation, offset math). The Pi needs three files: `pi/pi_sender.py`, `twin/__init__.py` (a one-line comment [repo `twin/__init__.py`]), `twin/pi_protocol.py`. It does **not** need `requests`, `python-dotenv` or `psutil`.
- Code is written to be syntactically valid for Python 3.9 and checked with `ast.parse(..., feature_version=(3,9))`. That proves syntax only, **not** that every stdlib call exists in 3.9. The Pi runs 3.13.5 [device]; the laptop 3.12.10.

## 2. Device facts

From the 2026-10-11 capture [device]: Raspberry Pi 3 Model B Plus Rev 1.3, aarch64, Debian 13 (trixie), kernel 6.18.50+rpt-rpi-v8, Python 3.13.5, MemTotal 926764 kB (≈905 MiB). System timezone America/New_York with no hardware clock (`/dev/rtc*` absent, "RTC time: n/a"); `timedatectl` printed `System clock synchronized: yes`. SoC thermal zone `/sys/class/thermal/thermal_zone0/type` = `cpu-thermal`, `temp` in millidegrees (40780 → 40.78 °C), `vcgencmd measure_temp` = 40.8 °C near the same time. `scaling_cur_freq` = 600000 (kHz) at idle. `vcgencmd get_throttled` printed `throttled=0x0`. `vcgencmd pmic_read_adc` → "Command not registered".

- **Four cores** is from the board spec, **not** read from the device. The two `/proc/stat` samples differ by 403 total ticks; that is consistent with 4 cores at USER_HZ 100 over about one second, which is a consistency check, not a measurement of the core count.
- **Not measurable on this board:** battery state and power draw. They are never invented, faked or sent as 0.0. The 2026-10-04 note's "battery" and "power-draw sensor" ideas stay unresolved and are out of scope.
- The Pi is on a phone-hotspot network (DHCP; reachable as `pitwin.local`), was shut down cleanly, and its boot target is `multi-user.target` (console only). This work assumes **no** reachable Pi.

## 3. Wire protocol (version 1)

Transport: HTTP/1.0-style, one request per connection (`Connection: close`), JSON bodies, `Content-Type: application/json`. Default receiver port 8765 [design; unused by anything in this repo: the repo uses 8080, 8081, 1883, 8086, 3000].

### 3.1 Authentication

`Authorization: Bearer <PI_SHARED_TOKEN>` on every request. The token comes from the environment variable `PI_SHARED_TOKEN` on both ends; there is **no** `--token` argument and the token is never printed or logged. The receiver compares with `hmac.compare_digest` on UTF-8 bytes. Both ends require printable ASCII without spaces (it travels in a header). The receiver refuses to start if the variable is unset, shorter than 16 characters or not of that shape [design]; the sender refuses to start if it is unset or not of that shape.

### 3.2 `POST /v1/probe` (clock probe)

Request `{"v":1,"t1":<int ms>}`. Reply `{"v":1,"t1":<echo>,"t2":<int ms>,"t3":<int ms>}`. On the Pi, `t1` is stamped **after the TCP connection is established** and `t4` as soon as the reply has been read. On the receiver, `t2` is taken as soon as the request body has been read and `t3` is the last thing stamped before the reply is written: the `probe_served` log record is written after the reply has gone out, and the reply leaves as one TCP segment with Nagle's algorithm off (a second small write could otherwise wait for the peer's delayed ACK inside the return leg).

### 3.3 `POST /v1/telemetry`

```json
{
  "v": 1,
  "session_id": "9f3a1c5e7b2d4a60",
  "seq": 41,
  "sent_at_ms": 1791750766000,
  "ntp_synchronized": true,
  "measurements": {
    "cpu_utilization":    {"value": 1.24,   "measured_at_ms": 1791750765995, "window_ms": 3004},
    "memory_utilization": {"value": 31.12,  "measured_at_ms": 1791750765996},
    "cpu_temperature_c":  {"value": 40.78,  "measured_at_ms": 1791750765997},
    "cpu_frequency_khz":  {"value": 600000, "measured_at_ms": 1791750765998}
  },
  "probes": [{"t1": 1791750755877, "t2": 1791750755982, "t3": 1791750755983, "t4": 1791750755888}]
}
```

| Field | Rule |
|---|---|
| `v` | must equal 1 |
| `session_id` | string, `[A-Za-z0-9_-]{8,64}`; random per sender start (`secrets.token_hex(8)`) |
| `seq` | integer ≥ 0 (not boolean), +1 per sample cycle, **including cycles where every measurement failed**; a cycle whose send fails still consumes its number, so the receiver sees loss as a gap |
| `sent_at_ms` | integer epoch UTC ms, Pi clock |
| `ntp_synchronized` | `true`, `false` or `null` (unknown). Parsed from the `System clock synchronized:` line of `timedatectl` [device: that line exists in the captured output]; refreshed at most every 60 s, between cycles and not inside one (§4.3); never assumed |
| `measurements` | object, may be empty (a heartbeat). Keys must be one of the four names in §5; any other key is rejected individually |
| `measurements.*.value` | finite number, not boolean, inside the range in §5 (the sender applies the same check and omits a reading that fails it). `NaN`/`Infinity` literals are rejected at parse time |
| `measurements.*.measured_at_ms` | integer epoch UTC ms, Pi clock, **one per measurement** (the instant that measurement was read) |
| `measurements.cpu_utilization.window_ms` | integer ≥ 1: length of the interval the CPU average covers (second `/proc/stat` read minus first), measured on the Pi's **monotonic** clock so that an NTP step cannot corrupt it. `measured_at_ms` for CPU is the **end** of that window |
| `probes` | optional list, ≤ 8 entries, each `{t1,t2,t3,t4}` integers: completed four-timestamp probes (§4) taken since the last telemetry attempt. Pending probes are cleared after every attempt, successful or not |

Every integer on the wire must lie within ±2^53 (non-negative where stated): a larger one is a protocol error (message level) or drops its measurement, never an exception. Message-level problems reject the whole message (HTTP 400). Measurement-level problems (unknown name, wrong type, non-finite, out of range, bad `measured_at_ms`) drop **only that measurement**; the message is still accepted and the reply lists `rejected_features`. Dropping follows the same rule as failure: omitted, never substituted.

### 3.4 HTTP status codes

| Code | When |
|---|---|
| 200 | processed (probe reply, or telemetry with `status` ∈ applied / duplicate / stale / stale_session) |
| 400 | body not valid JSON, not an object, wrong `v`, or bad `session_id` / `seq` / `sent_at_ms` / `measurements` / `probes` structure |
| 401 | missing, malformed or wrong bearer token (checked **before** the body is read) |
| 404 / 405 | unknown path / any method other than POST (TRACE, PROPFIND, ... included) |
| 411 | no usable `Content-Length` (chunked bodies are not supported); a number longer than 12 digits is treated as too large (413) |
| 413 | `Content-Length` > 8192 bytes (checked before reading) [design] |
| 415 | `Content-Type` is not `application/json` (a `charset` parameter is tolerated) |

Check order: method/path → auth → `Content-Length` → size → content type → read → parse → validate. The whole request (line, headers, body) must arrive within 10 s: a watchdog closes the connection at that deadline, because a per-read timeout is reset by every byte and a host dripping one byte at a time would hold a thread for ever. At most 64 connections are served at once; further ones are closed immediately. A stalled request is dropped with no reply; a body shorter than its `Content-Length` gets 400.

**Early rejections and the unread body.** For 401/404/405/411/413/415 the reply is sent before the body is read. Closing a socket that still has unread data makes Windows reset the connection, and the reset destroys the reply the client has not read yet — the Pi would then see a generic `ConnectionResetError` instead of `401`. (Found on 2026-10-11 by a flaky test run, reproduced deterministically, fixed.) So after replying the receiver half-closes its sending side and discards up to 64 KiB of the still-arriving body, bounded by the same 10 s deadline, before closing.

Telemetry 200 reply: `{"v":1,"status":...,"received_at_ms":<int>,"ditto":"ok"|"failed"|"skipped"|"disabled","rejected_features":[...]}`.

## 4. Time model

- **Every timestamp is integer epoch UTC milliseconds.** Never local time (the Pi's timezone is America/New_York [device]; the laptop's is irrelevant). Source: epoch nanoseconds floored to ms. On Windows that is `GetSystemTimePreciseAsFileTime` (sub-microsecond), because Python 3.12's `time.time_ns()` follows the system timer tick: measured on the laptop on 2026-10-11 it ticked every 1 ms only because another process was holding the timer at 1 ms, and the coarsest setting is 15.625 ms, which would quantize every probe timestamp. On the Pi it is `time.time_ns()`. Wall-clock steps (NTP corrections) can therefore occur mid-run; the `ntp_synchronized` flag and the probe series are how a run exposes that.
- The Pi has no hardware clock [device]: after a boot its clock is wrong until NTP synchronizes. The sender reports the flag and still sends; analysis decides.
- The laptop's clock sync state is **[unverified]** (not queried by the receiver). `offset` below is "receiver clock minus Pi clock", **not** "Pi error against true UTC".

### 4.1 Four-timestamp probe

`t1` = Pi send (Pi clock), `t2` = receiver receive (receiver clock), `t3` = receiver send (receiver clock), `t4` = Pi receive (Pi clock).

```
offset = ((t2 − t1) + (t3 − t4)) / 2      # receiver clock MINUS Pi clock; positive ⇒ receiver clock is ahead
delay  = (t4 − t1) − (t3 − t2)            # round-trip time excluding receiver processing
t_receiver = t_pi + offset                # converting a Pi timestamp to the receiver clock
```

Worked check. Let the true offset be θ, forward and return one-way times d_f and d_b: t2 = t1 + θ + d_f and t4 = t3 − θ + d_b. Then offset = θ + (d_f − d_b)/2 and delay = d_f + d_b. So the offset error is (d_f − d_b)/2, bounded by ±delay/2, and is zero only if the path is symmetric. On a phone-hotspot Wi-Fi link asymmetry is plausible, so every offset is an estimate with a bound of delay/2 plus ±1 ms quantization, and the analysis prefers the minimum-delay probe.

Why `t1` comes after the connect: the TCP handshake and the server's per-connection startup sit only in the forward leg, so timing them biases the offset by up to delay/2. Measured on loopback, where the true offset is 0: with `t1` stamped before connecting, two probes gave +7.0 and +10.0 ms (delay 16 and 24 ms); after the change, eight probes gave offsets from −1.5 to +0.5 ms with delay ≤ 3 ms (same machine, 2026-10-11 smoke test). Telemetry `sent_at_ms` is deliberately still stamped *before* connecting, because connection setup is real transport latency for a sample.

Hand-computed reference cases (used as unit tests):

| Case | t1, t2, t3, t4 | offset | delay |
|---|---|---|---|
| receiver 100 ms ahead, 5 ms each way, 1 ms processing | 10000, 10105, 10106, 10011 | +100.0 | 10 |
| receiver 250 ms behind, d_f 20, d_b 40, 2 ms processing | 50000, 49770, 49772, 50062 | −260.0 (true −250, error (20−40)/2 = −10) | 60 |
| clocks equal, symmetric | 1000, 1004, 1004, 1008 | 0.0 | 8 |
| half-millisecond result | 0, 4, 4, 5 | +1.5 | 5 |

### 4.2 Probe schedule

On sender start: a burst of 5 probes (`--probe-burst`), then one probe every 60 s (`--probe-every`). A probe whose reply does not echo its `t1`, or lacks integer `t2`/`t3`, is discarded. A computed `delay < 0` is kept in the log but excluded from analysis (clock step or quantization).

### 4.3 Where the `ntp_synchronized` check runs (changed 2026-10-13)

`NtpProbe.get()` runs `timedatectl` (3 s timeout, at most once per 60 s). It used to run inside the sample cycle, after the four measurements were read and before `sent_at_ms` was stamped. That kept its run time out of the network leg (§4.1), but it aged the measurements instead: on every cycle where the refresh fell due, `sent_at_ms − measured_at_ms` grew by the run time. **[measured 2026-10-13]** in the first live run (854 messages, 42.6 min): 42 messages (4.9 %), the first one and then one every 60–63 s, were sent 258–349 ms after their measurements were read; the other 812 were sent 1–2 ms after. The 2026-10-12 dry run shows the same pattern (19 of 384 messages, 248–342 ms). With those 42 messages the analyzer's per-measurement end-to-end p95 / p99 were 401.5 / 467.5 ms; without them, by the analyzer's own offset rule (§10.1), 168.5 / 231.5 ms. It is an artifact of the sender, not of the Pi's sensors.

Now the check runs only in `Sender.run()`: once before the first cycle (so the first message carries a real value, or `null` if `timedatectl` cannot be read), and after each cycle's send and any due clock probe, before the idle sleep is computed. `cycle()` only reads the cached value. [design] Consequences:

- No `timedatectl` run can fall between the first measurement read and the request, nor between the `sent_at_ms` stamp and the request.
- A message carries the result of the most recent check. A refresh that falls due after cycle *n* is first reported in cycle *n*+1, so the flag can lag by one sample interval more than before (at most 60 s + one interval + one run).
- The sampling grid is unchanged. A refresh that overruns the idle time (a run of the measured 250–350 ms fits easily in the idle time of a 3 s interval; only a command hanging to its 3 s timeout would not) falls under the existing rule: the next cycle starts at once, no catch-up burst.
- The startup check runs before the grid starts, so it delays the start of sampling by its run time but makes no cycle late.
- The flag is refreshed only inside `run()`, and the refresh after the last cycle of a counted run (`--count`) still runs before `run()` returns, so the return can be delayed by one run. A caller that calls `run()` repeatedly with idle gaps in between (the tests do; `main()` calls it once) starts each call with the value fetched at the end of the previous call.

Tests: `tests/test_pi_sender.py`, class `NtpPlacementTests` (event order, `sent_at − first measured_at` within a few ms on the cycle that crosses the 60 s refresh, first-message value, unknown stays unknown, overrun without a catch-up burst). **[unverified]** on the Pi: the new order has only run against fake clocks; the expected effect on the next live run is an age at send of 1–2 ms on every message, to be checked there.

**Open defect.** Moving the check out of `cycle()` fixes the age-at-send artifact but not the sender's own CPU cost: the `timedatectl` run (every `NTP_REFRESH_SEC` = 60 s) now falls inside the CPU sampling window of the *next* message, so `cpu_utilization` and `cpu_frequency_khz` on that message are affected (**[measured 2026-10-13]** in the live run, which used the old order: all 42 such messages; the position after this change follows from the order of the code and is **[unverified]** on the Pi). The numbers and the unverified fix options are in `notes/PI_RUNS_2026-10-12_DRYRUN_AND_LIVE.md` §7.1; none is decided.

## 5. Features, mapping and units

Wire measurement name = Ditto feature name. All values are measured on the Pi; nothing is derived on the laptop.

| Feature | Unit | Source [device paths] | Formula | Range check (receiver) |
|---|---|---|---|---|
| `cpu_utilization` | percent, 0–100 | first line of `/proc/stat` (the line whose first token is exactly `cpu`), two reads | `busy = (total − idle − iowait)` over the delta; `total = user+nice+system+idle+iowait+irq+softirq+steal` (guest columns excluded: the kernel already counts them in user/nice); `100·Δbusy/Δtotal`, rounded to 2 dp | 0 ≤ v ≤ 100 |
| `memory_utilization` | percent, 0–100 | `/proc/meminfo` | `100·(MemTotal − MemAvailable)/MemTotal`, 2 dp. No `MemFree` fallback (it changes the definition) | 0 ≤ v ≤ 100 |
| `cpu_temperature_c` | degrees Celsius | `/sys/class/thermal/thermal_zone0/temp` ÷ 1000, accepted only if `.../type` reads exactly `cpu-thermal` (the only value seen [device]) | integer millidegrees / 1000, 3 dp | −50 ≤ v ≤ 150 (a sanity bound for garbage reads, **not** a Pi limit) |
| `cpu_frequency_khz` | kilohertz | `/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq` (kHz, integer) | none | 0 < v ≤ 10 000 000 (sanity bound, not a Pi limit) |

Fixture-derived expectations (recomputed from the files on 2026-10-11, asserted in tests): CPU 5/403 = 1.2407 % → **1.24**; memory (926764 − 638312)/926764 = **31.12**; temperature **40.78**. The frequency parser has no fixture file (only the value 600000 from the device capture), so its test uses that literal.

CPU is the average over `window_ms`, not an instantaneous value: a load spike shorter than the window is smeared across it. The effective sampling instant of the CPU value is the window midpoint, `measured_at_ms − window_ms/2`; the analysis reports ages against `measured_at_ms` and notes this.

Not collected in v1: `get_throttled` flags (format `throttled=0x0` verified on the device, bit meanings in §11) — a candidate for a later version, listed in §13.

## 6. Failure semantics — "omitted is not fresh"

A measurement that fails or is unavailable is **omitted**: not sent, not written to Ditto, never replaced by 0.0 or a previous value.

| Situation | Sender / receiver behaviour |
|---|---|
| first CPU sample after sender start (no baseline) | CPU omitted that cycle |
| `/proc/stat` unreadable, unparsable, or fewer than 8 numeric columns | CPU omitted; the baseline is **discarded** (the next good read is again a lone first sample), so no average ever spans a read failure |
| counters went backwards, Δtotal ≤ 0, or computed busy time negative | CPU omitted (never clamped); the current good read becomes the new baseline, so the next cycle computes normally over its own window |
| `MemTotal` or `MemAvailable` missing, `MemTotal` ≤ 0, `MemAvailable` > `MemTotal` | memory omitted |
| thermal `type` ≠ `cpu-thermal`, `temp` unreadable or non-integer, or value outside the sanity bound | temperature omitted; WARN once per failure episode |
| `scaling_cur_freq` unreadable, non-integer, ≤ 0 | frequency omitted |
| a source file contains undecodable bytes | that measurement is omitted (never a crash) |
| two `/proc/stat` reads less than 1 ms apart on the monotonic clock | CPU omitted (never a clamped window) |
| all measurements omitted in a cycle | a heartbeat is still sent (empty `measurements`); it advances `seq` and the link feature |
| send fails (timeout, refused, 5xx) | WARN, no retry of that sample, next cycle sends a fresh one; `seq` gap marks the loss. The sender's request timeout defaults to 5 s, above the receiver's 3 s Ditto timeout, so a slow Ditto does not make the sender report a failure for a sample the receiver did apply |
| receiver replies 401 | sender exits non-zero (a wrong token will not fix itself) |
| Ditto PATCH fails or times out (3 s) | logged, `ditto:"failed"` in the reply and run log; the receiver keeps listening; the sample stays in the run log as received |
| Ditto thing missing (HTTP 404) | same as above; the WARN tells the operator to run `scripts/setup_pi_type.py` |

**Consequence to state plainly:** Ditto keeps the last value of an omitted feature, exactly as for `node_0` (`twin/publisher.py:118-125`). A consumer reading Ditto cannot tell a stale temperature from a fresh one while other features keep arriving. The run log is the authority for per-feature freshness; the `link` feature only says when the last *message* arrived.

## 7. Ordering and deduplication — key `(session_id, seq)`

Per receiver, one *current* session (the session of the most recent message that was applied). For each session: `max_seq` and the set of the last 1024 seen `seq` values.

| Condition | `status` | Ditto PATCH | Run log |
|---|---|---|---|
| unseen session (becomes current), or current session and `seq > max_seq` | `applied` | yes | yes |
| `(session, seq)` already in the seen set | `duplicate` | no | yes (probes not re-logged) |
| current session, `seq <= max_seq`, not in the seen set | `stale` (late or too old to tell) | no | yes |
| known session that is not current | `stale_session` | no | yes |

A gap (`seq > max_seq + 1`) is applied normally and counted as `missed = seq − max_seq − 1`. The first message of a session claims no loss unless its `seq` is 0 (then `missed` = 0); otherwise `missed` is unknown (`null`), because a receiver started mid-run cannot tell loss from a late start. Not applying stale data keeps Ditto from moving backwards. Decision and PATCH are serialized under one lock so they cannot interleave; console output is written after the lock is released, so a stalled console (for example a Windows console paused by a mouse click) cannot block other requests.

Session state lives in memory only. After a receiver restart, or after more than 16 newer sessions, a previously seen session is unknown, so a message from it is applied as a *new* session (see §9).

## 8. Ditto model

Names come from the repo, not guessed [repo `twin/config.py:22,34-37`]: `NAMESPACE = hnguyen.clustertwin` (also the `.env` value), `POLICY_ID = hnguyen.clustertwin:basic_policy`.

- Type `hnguyen.clustertwin:PiType`; Thing `hnguyen.clustertwin:pi_0`; same policy as the existing Things (`policyId` in the Type body, as `setup_types_and_twins.py:54` does for NodeType).
- **Outside the rack hierarchy:** no `link_type_child`, no `link_twin_child`. `aggregator.py` and `failure_detector.py` iterate only `RACK_IDS`/`RACK_NODES`/`NODE_IDS` [repo `config.py:38-44`], so `pi_0` is untouched by them. Dashboard check [repo `grafana/dashboard.json`]: panels 1 and 4 filter `thingId =~ /hnguyen\.clustertwin:node_/`, panels 2 and 3 read the `avg_cpu_utilization` field of `rack_*` / `main_cluster`, so a `pi_0` with a `cpu_utilization` feature **would not appear on any existing panel**. Whether its events reach InfluxDB at all depends on the live MQTT connection and Telegraf config, which are not in this repo **[unverified]**.
- Features, shape as `twin/publisher.py:153`: `{"name": {"properties": {"value": v}}}`:

| Feature | Properties |
|---|---|
| `cpu_utilization`, `memory_utilization`, `cpu_temperature_c`, `cpu_frequency_khz` | `value` |
| `link` | `session_id` (string), `last_seq` (integer), `last_received_epoch_ms` (integer, **receiver** clock) |

`link` carries `session_id` in addition to the two properties asked for, because `last_seq` alone is ambiguous after a Pi restart. [design]

- **Type creation:** the Type declares each feature with **empty** properties (`{"properties": {}}`), not `feature(0.0)` as `NodeType` does, so a freshly created `pi_0` holds no fake zero measurement. Whether the Extended API accepts empty-property features is **[unverified]** (needs the live cluster).
- **PATCH, exact form** (mirrors `publisher.patch_node` [repo `publisher.py:156-165`]): `PATCH {DITTO_HTTP_URL}/api/2/things/hnguyen.clustertwin%3Api_0/features`, `Content-Type: application/merge-patch+json`, basic auth from `twin/ditto_client.py`, timeout 3 s (`requests` semantics: 3 s to connect and 3 s per read, not a total deadline; a Ditto that trickles bytes could take longer — not tested), one request per applied message containing every accepted measurement **plus** `link`. One request means the features of a message land in one Ditto revision (relevant to consistency, §10.4).
- **Pi silence** is detected from `link.last_received_epoch_ms` and the analyzer, **not** by `twin/failure_detector.py` (it polls `NODE_IDS` only). Rule: silent when `now − last_received_epoch_ms > FAILURE_TOLERANCE_SEC` (default 9 s [repo `config.py:25`]); both timestamps are on the laptop's clock, so no cross-device offset enters. Limitation: if the **receiver** dies, `link` stops updating too — a dead receiver and a dead Pi look the same in Ditto.
- `scripts/setup_pi_type.py` prints its plan and makes no request unless run with `--apply`. It never links the Thing into the rack tree.

## 9. Security

- Plain HTTP: the bearer token and every measurement are visible to anyone on the same network. **TLS is out of scope** and the token must not be reused anywhere else. A phone hotspot shared with other devices widens exposure.
- Explicit bind: `--bind` is required, no default. `--bind 0.0.0.0` is accepted only because it is typed, with a console warning. The runbook uses the laptop's hotspot-side address.
- Windows Defender Firewall must allow the port inbound; the runbook scopes the rule to the Pi's address and removes it afterwards.
- Logs and the JSONL run log never contain the token, the `Authorization` header, or client addresses. The receiver's own access log lines carry method, path and status only, and socketserver's default error report (a traceback with the peer's address) is replaced by one line naming the exception type.
- Body limit 8 KiB; auth is checked before the body is read; measurement names echoed in replies are truncated and JSON-encoded. At most 64 connections are served at once and each request must arrive within 10 s in total, so a host without the token cannot hold threads for long.
- The bearer token proves the sender knows the token; it does not authenticate the Pi hardware. Because the transport is plain HTTP, anyone on the LAN who captures one request has the token and can then send forged telemetry with fresh `seq` values that the receiver will apply. A request *replayed within the same receiver run* is classified `duplicate`, `stale` or `stale_session` and not applied. After a receiver restart (state is in memory) the replayed session is unknown and is applied as a new one, and the real sender is then locked out as `stale_session` until it restarts. Whoever can capture a request already holds the token and could forge anyway, so this is documented, not mitigated.

## 10. Fidelity measurements for this pipeline

The four protocols come from `notes/FIDELITY_VALIDATION_PLAN_2026-10-01.md` §3 (latency, behavioural accuracy, detectability, consistency). Below: what each means here and what `scripts/analyze_pi_run.py` computes **now**, from the receiver's JSONL run log. Percentiles are nearest-rank.

### 10.1 Latency

Stages, per applied sample with measurement time `m`, `sent_at`, receiver `received_at`, `patched_at`, and offset estimate `o`:

- `age_at_send = sent_at − m` (Pi clock only)
- `net = received_at − sent_at − o` (cross-clock; true one-way time **plus** the offset error from §4.1)
- `ditto = patched_at − received_at` (receiver clock only; absent when the PATCH failed or `--dry-run`)
- `end_to_end = patched_at − (m + o) = age_at_send + net + ditto`

The offset used is the minimum-`delay` probe within ±300 s of the sample's `received_at` (fallback: the best probe of the whole run, flagged). Beyond `patched_at`: Ditto → Mosquitto → Telegraf → InfluxDB is **not** measured by this pipeline. Prior end-to-end observation, quoted from `FIDELITY_VALIDATION_PLAN_2026-10-01.md` §2 item 4 (I did not re-read its source record): 1.75–10.12 s over 8 samples, Telegraf `flush_interval = 10s` [repo README:165, "confirmed live 2026-08-25"].

### 10.2 Accuracy

Defined as the error between the twin's value and an independent reference reading of the same quantity at the same instant (RMSE over matched pairs, after offset alignment). That needs a reference logger that does not exist yet (§13); **the analyzer prints "not measured (no reference log)".** What exists today only bounds it: CPU resolution is 100/Δtotal percent (≈0.25 % for 403 ticks), memory and frequency are exact reads, temperature is reported to 0.001 °C but the sensor's real accuracy is **[unverified]**.

### 10.3 Detectability

Silence episodes: gaps between consecutive messages that carried new information (statuses applied, stale, stale_session; duplicates do not count), ordered by `received_at`, larger than a threshold (default 9 s). For each: gap length and the moment the §8 rule would have flagged it (`last_received + threshold`). Also per-feature dropouts: longest run of consecutive messages lacking each feature. This characterizes a **total** dropout (no messages) and a **partial** one (one feature omitted) separately; the plan's remark that `_modified`-keyed detection misses partial dropouts is why both exist.

### 10.4 Consistency

- Bundle completeness: share of applied messages that carried all four measurements.
- Intra-message skew: max − min of `measured_at_ms` across a message's measurements.
- Sequence health: applied, duplicate, stale, stale_session counts; missed (gap) count. `stale` is the out-of-order count: a message that arrived after a higher `seq` of its session.
- Sampling regularity: mean/stdev/min/max of successive `measured_at_ms` for `cpu_utilization` within a session.
- Clock stability: spread of the offset estimates, linear-fit drift slope in ppm (positive = the receiver's clock is gaining on the Pi's), and the counts of applied messages with `ntp_synchronized` = true / false / unknown.

## 11. Pi 3 B+ limits (verified against official documentation on 2026-10-11; read-only fetches)

- **Soft limit:** default 60 °C for the 3 B+; when reached the clock drops from 1.4 GHz to 1.2 GHz and voltage drops slightly; changeable with `temp_soft_limit` in `config.txt`. **Hard limit:** 85 °C on all models; between 80 °C and 85 °C the Arm cores are progressively throttled, at 85 °C the GPU is throttled too. Source: https://www.raspberrypi.com/documentation/computers/raspberry-pi.html, section "Frequency management and thermal control". (The pages came through a summarizing fetch tool, not a raw download. I re-queried the 60 °C board attribution once and both answers tied the soft limit to the 3 B+.)
- **`vcgencmd get_throttled` bits:** 0x1 under-voltage detected, 0x2 Arm frequency capped, 0x4 currently throttled, 0x8 soft temperature limit active, 0x10000 / 0x20000 / 0x40000 / 0x80000 the corresponding "has occurred" flags. Source: https://www.raspberrypi.com/documentation/computers/os.html, section "vcgencmd" / `get_throttled`.
- **Board basics:** BCM2837B0 Cortex-A53 @ 1.4 GHz, 1 GB LPDDR2, power input "5V/2.5A DC". Source: https://www.raspberrypi.com/products/raspberry-pi-3-model-b-plus/.
- **Unverified / not measured here:** the under-voltage threshold in volts (not stated in the fetched pages); what the SoC temperature does under load or in this enclosure; whether the board throttles at 60 °C in practice; real power draw. The idle 600000 kHz reading is not claimed to be a throttle or a governor setting: the governor was not read. `get_throttled = 0x0` at capture means none of the bits above were set at that moment, including the "has occurred" bits.

## 12. The hotspot network as a recorded measurement condition

The Pi reaches the laptop over a phone hotspot (variable Wi-Fi latency, possible client isolation, DHCP address changes, phone power-saving). For every run record in the run notes: network type (phone hotspot), date/time, whether the laptop was on the same hotspot by Wi-Fi or by another path, probe delay distribution (min/median/p95/max from the analyzer), and any visible disconnects. Report latency as a **distribution with its conditions**, never as one number. Client-isolation on the hotspot, if present, would block the Pi → laptop connection entirely — **[unverified]** until the bring-up.

## 13. Forward compatibility

- **Reference logger:** a future high-rate (the fidelity plan sketches 10 Hz) logger on the Pi writing its own JSONL for RMSE (§10.2). It would share `twin/pi_protocol.py` parsers and the same epoch-ms convention; `scripts/analyze_pi_run.py` would take a second input file.
- **Per-sample receiver timestamps** are already stored in the run log (`received_at_ms`, `patched_at_ms`); they are what makes stage-level latency possible. `measured_at_ms` is **not** written into Ditto (Telegraf's JSON mapping of extra feature properties is unknown [unverified]); do not add it without checking the pipeline.
- `v` is the version gate: a v2 sender gets HTTP 400 from a v1 receiver.
- Later candidates: `get_throttled` flags; Wi-Fi signal; TLS or a tunnel; receiver heartbeat feature to separate "receiver dead" from "Pi dead".

## 14. Corrections to `notes/PI_SYNC_DESIGN_2026-10-04.md`

1. It says `pi_idle_jitter_measurement.py` "emits 0.0" for the first CPU sample. `sample_once(None)` does return 0.0 (lines 78-79), but `run()` discards that priming reading (`_, prev_ticks = sample_once(None)`, line 97), so no 0.0 ever enters `samples`. The advice not to copy the function's 0.0 stands; the claim about what the script emits is wrong.
2. "SoC temp and battery have no slot" overstates what was checked: `NodeType` *declares* four features (`setup_types_and_twins.py:62-67`), but nothing verified that Ditto or the Extended API rejects other features. The note's own last line says so.
3. It frames the board as a Pi 5 (also the docstrings of both Pi measurement scripts). The board is a **Pi 3 B+** [device].
4. It proposes reusing `patch_node` from `twin.publisher`. That module imports `psutil` at import time, so the receiver calls `ditto()` and `enc()` from `twin/ditto_client.py` directly.
5. Its open questions are decided here: transport = plain HTTP with a bearer token (§1, §3, §9); silence detection = link feature + analyzer (§8); new PiType and `pi_0` (§8). Its battery section stays unresolved. Its file name `twin/pi_transmitter.py` became `pi/pi_sender.py`.

## 15. Open or unverified items

Real-Pi execution of the sender; Python 3.9 stdlib API availability (syntax only checked); Extended API acceptance of empty-property features and of the `pi_0` create call; whether `pi_0` events reach InfluxDB/Grafana; hotspot client isolation; laptop clock sync state; under-voltage voltage threshold; core count measured directly; temperature sensor accuracy.
