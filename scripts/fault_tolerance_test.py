"""
Phase 10 — fault tolerance test (adapted for direct HTTP inbound).

Kills a Ditto pod and measures time until publisher PATCH succeeds again.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from twin.config import NODE_IDS
from twin.ditto_client import ditto, enc

OUT = ROOT / "notes" / "fault_tolerance_results.md"


def kubectl(*args: str) -> str:
    r = subprocess.run(
        ["kubectl", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"kubectl {args} failed: {r.stderr}")
    return r.stdout.strip()


def pick_ditto_pod() -> str:
    out = kubectl("get", "pods", "-l", "app.kubernetes.io/name=ditto-things", "-o", "jsonpath={.items[0].metadata.name}")
    if not out:
        # fallback: gateway
        out = kubectl(
            "get",
            "pods",
            "-o",
            "jsonpath={range .items[*]}{.metadata.name}{'\\n'}{end}",
        )
        for line in out.splitlines():
            if "ditto-things-" in line and "thingssearch" not in line:
                return line.strip()
        raise RuntimeError("No ditto-things pod found")
    return out


def patch_ok(node_id: str) -> bool:
    body = {
        "cpu_utilization": {"properties": {"value": 1.0}},
        "memory_utilization": {"properties": {"value": 1.0}},
        "active_connections": {"properties": {"value": 1}},
        "latency_ms": {"properties": {"value": 1.0}},
    }
    try:
        r = ditto(
            "PATCH",
            f"/api/2/things/{enc(node_id)}/features",
            json=body,
            content_type="application/merge-patch+json",
            timeout=5,
        )
        return r.status_code in (200, 204)
    except Exception as e:
        print(f"  patch error: {e}")
        return False


def main() -> None:
    node = NODE_IDS[0]
    print("Baseline PATCH before kill...")
    if not patch_ok(node):
        raise RuntimeError("Baseline PATCH failed; aborting fault test")

    pod = pick_ditto_pod()
    print(f"Deleting pod: {pod}")
    t_kill = time.perf_counter()
    kubectl("delete", "pod", pod, "--wait=false")
    print(f"Kill issued at t=0")

    # Wait until PATCH fails at least once (optional) then succeeds
    saw_failure = False
    recovered_at = None
    deadline = time.time() + 180
    while time.time() < deadline:
        ok = patch_ok(node)
        elapsed = time.perf_counter() - t_kill
        print(f"  t={elapsed:.2f}s PATCH ok={ok}")
        if not ok:
            saw_failure = True
        if ok and (saw_failure or elapsed > 2.0):
            # If delete was so fast we never saw failure, still count first
            # success after a short grace as recovery once pod is gone/replaced.
            if saw_failure or elapsed > 5.0:
                recovered_at = elapsed
                break
        time.sleep(1.0)

    if recovered_at is None:
        # last resort: wait for new pod ready then PATCH
        print("Waiting for replacement pod Ready...")
        kubectl("wait", "--for=condition=Ready", "pod", "-l", "app.kubernetes.io/name=ditto-things", "--timeout=180s")
        while time.time() < deadline:
            if patch_ok(node):
                recovered_at = time.perf_counter() - t_kill
                break
            time.sleep(1.0)

    if recovered_at is None:
        raise RuntimeError("Did not recover within timeout")

    md = f"""# Fault tolerance results (Phase 10)

## Setup

- Inbound path: **direct HTTP PATCH** to Ditto (`ditto:ditto`), not Hono MQTT adapter
- Killed pod: `{pod}` (ditto-things)
- Measured: time from `kubectl delete pod` to first successful HTTP PATCH
  against `{node}`

## Measured recovery

- **Recovery time: {recovered_at:.2f} seconds**
- Observed at least one failed PATCH before recovery: **{saw_failure}**

## Comparison to paper Test 3

- Paper Test 3 reported **52.46 s** recovery for the **Hono MQTT adapter** failure mode.
- My deployment tests **direct HTTP recovery** (no MQTT broker layer in the inbound path),
  which is a different failure mode than the paper's MQTT adapter recovery.
  Expected to be faster since HTTP is synchronous and there is no Hono adapter
  reconnect cycle.

## Caveats

- Local minikube + port-forward; absolute times are not production numbers.
- Outbound MQTT connection may briefly interrupt independently; this test
  specifically measures inbound PATCH recovery.
"""
    OUT.write_text(md, encoding="utf-8")
    print(md)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
