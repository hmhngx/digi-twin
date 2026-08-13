# Fault tolerance results (Phase 10)

## Live re-run — 2026-08-13 (this session, run at explicit request, on a healthy baseline)

- Inbound path: **direct HTTP PATCH** to Ditto (`ditto:ditto`), not Hono MQTT adapter
- Killed pod: `opentwins-ditto-things-788f855654-2bqzw` (ditto-things); replacement pod `opentwins-ditto-things-788f855654-dmcft` confirmed `1/1 Running`, 0 restarts, afterward
- Measured: time from `kubectl delete pod` to first successful HTTP PATCH against `hnguyen.clustertwin:node_0`
- **Recovery time: 42.31 seconds**
- Observed at least one failed PATCH before recovery: **True** (six consecutive `Read timed out (read timeout=5)` probes between t=6.76s and t=36.91s, then success at t=42.31s)

**Side effect, observed live and worth recording as real evidence, not hidden:** this pod kill also crashed the independently-running `twin/publisher.py` and `twin/aggregator.py` processes (both hit the same unhandled-`ConnectionError`/timeout crash documented elsewhere in this repo's audit — neither retries, both exit). `twin/failure_detector.py` **did not crash** — it kept running throughout, correctly logged **`FAILED`** for all 10 nodes once publisher's outage meant nothing was updating anymore, and correctly logged **`CLEARED`** for every node once publisher was manually restarted and resumed publishing. This is a genuine, real demonstration of the detector's relative robustness (it survived a pod-kill event that took down the other two scripts), not just a code-read claim — though note this appears to be because its GET calls didn't happen to hit a connection-level exception during this particular outage window, not because it has explicit error handling the other two lack (its code has no try/except around the network call either; see `twin/failure_detector.py`). Do not generalize this to "the detector is crash-proof" — it was not tested under a scenario that reliably triggers a GET-level connection exception.

Both `publisher.py` and `aggregator.py` were manually restarted after this run and confirmed healthy again (publisher cycling normally, aggregator recomputing racks/cluster, detector clearing all previously-failed nodes).

## Fresh session sample (playbook authoring — 2026-08-06)

- Inbound path: **direct HTTP PATCH** to Ditto (`ditto:ditto`), not Hono MQTT adapter
- Killed pod: `opentwins-ditto-things-788f855654-qd6qg` (ditto-things)
- Measured: time from `kubectl delete pod` to first successful HTTP PATCH
  against `hnguyen.clustertwin:node_0`
- **Recovery time: 53.04 seconds**
- Observed at least one failed PATCH before recovery: **True**
- Raw log: `notes/_playbook_fault_once.txt`

This is a single post-cold-start sample for live demo credibility. It is **not** a
replacement for the audited three-run set below.

## Audited three-run set (2026-07-28 independent QA)

Prior single build value **23.55 s** is **not** used as representative.

| Run | Recovery time (s) | Saw failed PATCH before recovery |
|----:|------------------:|----------------------------------|
| 1 | 94.73 | True (see `notes/_audit_fault_run1.txt`) |
| 2 | 62.77 | True |
| 3 | 36.69 | True |

- **Mean:** 64.73 s
- **Min / max:** 36.69 / 94.73 s
- **Spread (max−min):** 58.04 s

**Variance is high.** Do not present a single favorable run as "the" recovery number.
Also documented in `notes/VERIFICATION_REPORT.md` §8 and `notes/SCALING_READINESS.md`.

## Combined picture across all 5 known runs

| Date | Recovery (s) |
|---|---:|
| 2026-07-28 (audit run 1) | 94.73 |
| 2026-07-28 (audit run 2) | 62.77 |
| 2026-07-28 (audit run 3) | 36.69 |
| 2026-08-06 (playbook sample) | 53.04 |
| 2026-08-13 (this session) | 42.31 |

Mean across all 5: **57.91 s**. Range: **36.69–94.73 s**. Still high variance — the
2026-08-13 sample sits comfortably inside the existing range, it does not change the
overall picture or justify tightening the quoted spread.

## Comparison to paper Test 3

- Paper Test 3 reported **52.46 s** recovery for the **Hono MQTT adapter** failure mode.
- This deployment tests **direct HTTP recovery** (no MQTT broker layer in the inbound path),
  a different failure mode than the paper's MQTT adapter recovery.
- All 5 samples on record (36.69–94.73 s, mean ~58 s) bracket the paper's 52.46 s figure,
  with large run-to-run spread on local minikube.

## Caveats

- Local minikube + port-forward; absolute times are not production numbers.
- Cluster restart state and Mongo readiness contribute to spread.
- Outbound MQTT connection may briefly interrupt independently; this test
  specifically measures inbound PATCH recovery.
- **This script has no CLI flag to append instead of overwrite** — every run replaces
  the entirety of what `OUT.write_text(...)` produces. The historical runs above only
  survive because this file is manually re-merged by hand after each run. If rerunning,
  copy this file's content out first.
