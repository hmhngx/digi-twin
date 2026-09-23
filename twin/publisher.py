"""
Phase 4 — telemetry publisher.

Inbound path (simplified): direct Ditto HTTP PATCH of node features every ~3s.
Auth: ditto:ditto. No MQTT inbound.
Honors notes/_paused_nodes.json so the failure detector can pause one node.

Two different data sources, on purpose (as of 2026-09-22):
  node_0         -- REAL telemetry measured from the machine this publisher runs
                    on, via psutil and a timed TCP connect (measure_host()). The
                    one node with an automated physical -> digital flow.
  node_1..node_9 -- synthetic random.uniform/randint draws, unchanged. No
                    physical counterpart.
See notes/DIGITAL_TWIN_ADAPTATION_ANALYSIS.md for why node_0 was converted.
"""
from __future__ import annotations

import argparse
import json
import random
import socket
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twin.config import (
    LATENCY_PROBE_HOST,
    LATENCY_PROBE_PORT,
    LATENCY_PROBE_TIMEOUT_SEC,
    NODE_IDS,
    PUBLISH_INTERVAL_SEC,
)
from twin.ditto_client import ditto, enc

PAUSE_FILE = ROOT / "notes" / "_paused_nodes.json"


def load_paused() -> set[str]:
    if not PAUSE_FILE.exists():
        return set()
    return set(json.loads(PAUSE_FILE.read_text(encoding="utf-8")))


def is_real_node(node_id: str) -> bool:
    """node_0 is the one node fed from real host telemetry (measure_host())."""
    return node_id.endswith(":node_0")


def prime_host_probes(settle_sec: float = 1.0) -> None:
    """Call once before the first measure_host().

    psutil.cpu_percent(interval=None) reports CPU use *since its previous call*,
    so its very first call has nothing to compare against and returns a
    meaningless 0.0 (psutil's own docstring says to ignore it, and to leave at
    least 0.1 s between calls). Priming alone is not enough: node_0 is the first
    node measured each cycle, so without the settle wait the first real reading
    lands microseconds after the prime and is 0.0 again -- that is exactly what
    the first live run did on 2026-09-22 (cycle 1 published cpu_utilization=0.0).
    After the wait, node_0's first published CPU value is a real ~1 s average.
    """
    psutil.cpu_percent(interval=None)
    time.sleep(settle_sec)


def tcp_connect_rtt_ms(host: str, port: int, timeout: float) -> float | None:
    """Time one real TCP handshake (connect() returning = SYN sent, SYN-ACK back).

    Measured fresh every call -- nothing cached. Address lookup happens before
    the timer starts, so DNS time (if a hostname is configured) is never folded
    into the number. Returns None on failure/timeout: a failed probe is reported
    as missing, never replaced with a made-up value.
    """
    try:
        family, stype, proto, _, addr = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
        with socket.socket(family, stype, proto) as s:
            s.settimeout(timeout)
            t = time.perf_counter()
            s.connect(addr)
            return (time.perf_counter() - t) * 1000.0
    except OSError as e:
        print(f"WARN node_0 latency probe to {host}:{port} failed: {e!r}")
        return None


def measure_host() -> dict:
    """node_0's four features, each one actually measured on this machine right now.

    - cpu_utilization: psutil.cpu_percent(interval=None) -- system-wide CPU %
      averaged over the whole time since the previous call, i.e. the full
      publish period (the 3 s sleep plus the cycle's ten sequential Ditto
      PATCHes -- measured 5-21 s per cycle on this machine, 2026-09-22). Chosen
      over a short blocking interval (e.g. 0.5 s) for two reasons: a 0.5 s slice
      would be blind to any burst in the rest of each cycle, and it would stall
      the publish loop for all ten nodes every cycle. Consequence to know about:
      after node_0 is paused and resumed, the first reading is the average over
      the whole pause, not over one cycle.
    - memory_utilization: psutil.virtual_memory().percent -- (total - available)
      / total, i.e. physical RAM in use.
    - active_connections: system-wide count of TCP connections (IPv4 + IPv6) in
      state ESTABLISHED -- open, live connections, not LISTEN sockets, not
      TIME_WAIT leftovers, not connectionless UDP. Includes loopback, so the
      twin stack's own kubectl port-forward connections are part of the count.
      Measured 2026-09-22 on this Windows 11 machine as a NON-admin user: no
      psutil.AccessDenied at all (psutil's docstring: limited privileges only
      blank out pid/fd, which this count doesn't use; root is required on
      macOS). AccessDenied is still handled below in case the publisher is ever
      run somewhere it does fire.
    - latency_ms: real TCP connect round-trip time from this machine to
      LATENCY_PROBE_HOST:LATENCY_PROBE_PORT (default 1.1.1.1:443), timed fresh
      every cycle by tcp_connect_rtt_ms(). Means "network RTT from this host to
      a fixed external endpoint", nothing more.

    If a probe fails, that feature is left OUT of the returned dict, so the
    merge-PATCH leaves Ditto's previous value in place instead of writing an
    invented one, and a WARN line says so. No synthetic fallback exists for any
    of the four. (In steady state that previous value is itself an earlier real
    reading. The one exception: a probe failing on the very first cycle after
    switching an existing deployment over would leave whatever the old
    synthetic code last wrote standing for that feature -- the WARN makes it
    visible.)
    """
    values: dict[str, float | int] = {
        "cpu_utilization": round(psutil.cpu_percent(interval=None), 2),
        "memory_utilization": round(psutil.virtual_memory().percent, 2),
    }
    try:
        values["active_connections"] = sum(
            1 for c in psutil.net_connections(kind="tcp") if c.status == psutil.CONN_ESTABLISHED
        )
    except psutil.AccessDenied as e:
        print(f"WARN node_0 active_connections not measurable here, not published: {e!r}")
    rtt = tcp_connect_rtt_ms(LATENCY_PROBE_HOST, LATENCY_PROBE_PORT, LATENCY_PROBE_TIMEOUT_SEC)
    if rtt is not None:
        values["latency_ms"] = round(rtt, 2)
    return values


def node_features(node_id: str) -> dict:
    if is_real_node(node_id):
        values = measure_host()
    else:
        values = {
            "cpu_utilization": round(random.uniform(15.0, 55.0), 2),
            "memory_utilization": round(random.uniform(20.0, 70.0), 2),
            "active_connections": random.randint(5, 80),
            "latency_ms": round(random.uniform(2.0, 40.0), 2),
        }
    return {name: {"properties": {"value": v}} for name, v in values.items()}


def patch_node(node_id: str, features: dict) -> int:
    r = ditto(
        "PATCH",
        f"/api/2/things/{enc(node_id)}/features",
        json=features,
        content_type="application/merge-patch+json",
    )
    if r.status_code >= 400:
        raise RuntimeError(f"PATCH {node_id} failed: HTTP {r.status_code} {r.text[:400]}")
    return r.status_code


def run(cycles: int | None = None, interval: float = PUBLISH_INTERVAL_SEC) -> None:
    n = 0
    prime_host_probes()
    print(
        f"Publisher started: {len(NODE_IDS)} nodes, interval={interval}s "
        f"(node_0 = real host telemetry, latency probe {LATENCY_PROBE_HOST}:{LATENCY_PROBE_PORT}; "
        f"node_1..node_9 = synthetic)"
    )
    while cycles is None or n < cycles:
        n += 1
        paused = load_paused()
        for node_id in NODE_IDS:
            if node_id in paused:
                print(f"[{n}] SKIP paused {node_id}")
                continue
            feats = node_features(node_id)
            status = patch_node(node_id, feats)
            if is_real_node(node_id):
                shown = " ".join(f"{k}={v['properties']['value']}" for k, v in feats.items())
            else:
                shown = f"cpu_utilization={feats['cpu_utilization']['properties']['value']}"
            print(f"[{n}] PATCH {node_id} {shown} -> HTTP {status}")
        time.sleep(interval)


def main() -> None:
    p = argparse.ArgumentParser(description="Cluster twin telemetry publisher")
    p.add_argument("--cycles", type=int, default=None, help="Finite cycles (default: forever)")
    p.add_argument("--interval", type=float, default=PUBLISH_INTERVAL_SEC)
    args = p.parse_args()
    run(cycles=args.cycles, interval=args.interval)


if __name__ == "__main__":
    main()
