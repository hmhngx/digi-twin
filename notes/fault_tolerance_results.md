# Fault tolerance results (Phase 10)

## Setup

- Inbound path: **direct HTTP PATCH** to Ditto (`ditto:ditto`), not Hono MQTT adapter
- Killed pod: `opentwins-ditto-things-788f855654-mj67c` (ditto-things)
- Measured: time from `kubectl delete pod` to first successful HTTP PATCH
  against `hnguyen.clustertwin:node_0`

## Measured recovery

- **Recovery time: 23.55 seconds**
- Observed at least one failed PATCH before recovery: **True**

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
