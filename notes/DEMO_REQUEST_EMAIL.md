# Demo request email — copy above the separator into Gmail/Outlook

**To:** Prof. Siddiqui  
**From:** Harrison  
**Subject:** Digital twin PoC demo — request for ~30-minute meeting

---

Hi Professor Siddiqui,

I'd like to demonstrate the digital twin proof-of-concept I built over the summer for your review, and discuss how it connects to the Fall research direction. Would you have time for a ~30-minute call in the next week?

Over the summer I built a three-level digital twin on our OpenTwins stack (Eclipse Ditto, Extended API, Mosquitto, Telegraf, InfluxDB, and Grafana). It models a cluster with 2 racks and 10 nodes, using Ditto Twin Types and parent links through the Extended API. Data goes into Ditto over HTTP (simpler than the paper's Hono→Kafka path, which we don't have in this deployment). Data goes out through the real platform path: Ditto → MQTT → Telegraf → InfluxDB → Grafana, which we've checked live. The twin also detects node failures and rolls summaries up the hierarchy — the cluster reads from the racks, not from every node. When I killed the Ditto pod, it came back in about 37–95 seconds across three test runs (around 65 seconds on average). The whole setup is scripted and reproducible at commit `04570a3`, tagged `twin-build-complete`.

One caveat: that recovery time varies a lot between runs, so I treat it as a range from local Minikube testing, not a single hard number. Also, Grafana's browser login has been flaky; if that happens in the meeting, I'll show the live data through API calls and share the dashboard JSON.

I think this is ready to walk through. The build is tagged, we ran a verification audit on 2026-07-28 (everything functional passed, with a few noted caveats), the git history on that commit is clean, and I have a step-by-step demo guide in the repo. I've tested 10 nodes and up to 20 concurrent clients — I haven't tested larger scales or long-running load, so I'm not claiming this is ready to scale. Happy to demo live or talk about what this means for Fall research.

I'm excited to show you what this taught me about Ditto's compositionality model and how it shapes the Fall work. Let me know what works with your schedule.

Best,  
Harrison

---

## Claim → source checklist (do not paste into email)

| Claim in email | Source |
|----------------|--------|
| Components: Ditto, Extended API, Mosquitto, Telegraf, InfluxDB, Grafana | `twin/README.md`, `notes/twin_design_spec.md` |
| Hierarchy: cluster → 2 racks → 10 nodes; parent links via Extended API | `twin/README.md`, `notes/twin_design_spec.md`, `notes/VERIFICATION_REPORT.md` §1 |
| Inbound: HTTP → Ditto; no Hono/Kafka | `twin/README.md` (Out of Scope), `notes/twin_design_spec.md` |
| Outbound: Ditto → MQTT → Telegraf → InfluxDB → Grafana, verified live | `notes/twin_design_spec.md` (Phase 5), `notes/VERIFICATION_REPORT.md` §3 |
| Failure detection + aggregation (cluster from racks) | `twin/README.md`, `notes/twin_design_spec.md`, `notes/VERIFICATION_REPORT.md` §4–§5 |
| Fault recovery ~37–95 s, mean ~65 s (3 runs); not 23.55 s | `notes/fault_tolerance_results.md`, `notes/VERIFICATION_REPORT.md` §8 |
| Commit `04570a3`, tag `twin-build-complete` | `git log --oneline`, `git tag -l` |
| Grafana login flaky; API/JSON fallback | `notes/VERIFICATION_REPORT.md` §6, `notes/PROFESSOR_DEMO_PLAYBOOK.md` §4 step 6 |
| Audit date 2026-07-28; functional items passed with caveats | `notes/VERIFICATION_REPORT.md` summary table |
| Git history clean on tagged commit | `git log` / `git cat-file -p 04570a3` (no `Co-authored-by`) |
| Demo guide exists | `notes/PROFESSOR_DEMO_PLAYBOOK.md` |
| Tested: 10 nodes, ≤20 clients; larger scale untested | `notes/SCALING_READINESS.md` |
