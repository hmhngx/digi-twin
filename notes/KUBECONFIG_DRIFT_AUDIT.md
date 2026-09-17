# Kubeconfig Drift — Root Cause Audit

**Written:** 2026-09-16. Investigated after hitting the exact failure the root [README.md](../README.md) already documents (`kubeconfig endpoint: got X, want Y`) twice in one working session, roughly 19 hours apart. This document explains *why* it happens, with direct evidence, not just the recovery steps the README already has.

## The symptom, already documented

From `README.md`'s "Runtime topology" section: *"After a Docker bounce the VM can look Running while kubelet/apiserver are Stopped and kubeconfig still points at the previous API port."* That was written from a single 2026-08-25 example. This session reproduced it twice more, independently, with the exact same shape:

| Occurrence | `minikube status` reported |
|---|---|
| 1 (2026-09-16, morning UTC) | `kubeconfig endpoint: got: 127.0.0.1:52626, want: 127.0.0.1:61228` |
| 2 (2026-09-16, evening UTC, ~19h later) | `kubeconfig endpoint: got: 127.0.0.1:61228, want: 127.0.0.1:55072` |

Note occurrence 2's "got" is exactly occurrence 1's "want" — direct proof the port advances by one each time the cluster is restarted, and the kubeconfig always lags by exactly one generation.

## Root cause, confirmed with direct evidence (not inferred)

**1. The "minikube" container is never recreated — it's the same container this entire project has ever used.**

```
docker inspect minikube --format 'ID: {{.Id}} / Created: {{.Created}} / RestartCount: {{.RestartCount}}'
→ ID: d7582588433d... / Created: 2026-07-26T12:04:49Z / RestartCount: 0
```

`Created` matches this repo's very first build session. `RestartCount: 0` (Docker's own crash-restart counter, unrelated to manual stop/start) confirms Docker itself never force-restarted it. This rules out "the container gets destroyed and rebuilt" as the explanation.

**2. Its port bindings are configured with an empty (dynamic) host port — confirmed by reading the container's own stored config, not the live `docker port` report:**

```
docker inspect minikube --format '{{json .HostConfig.PortBindings}}'
→ "8443/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]   (same for 22, 2376, 5000, 32443)
```

An empty `HostPort` is a real, standard Docker setting meaning "assign a free ephemeral port fresh, every time this container starts" — not just once at creation. This is minikube's own default for the docker driver (so multiple minikube profiles on one machine don't collide on a fixed port), not a misconfiguration specific to this repo.

**3. The container does, in fact, stop between work sessions, because nothing keeps Docker Desktop running across them:**

```
docker settings-store.json → "AutoStart": false
wsl.exe --list --verbose → docker-desktop: Running, Ubuntu: Stopped
wsl.exe -d docker-desktop -- cat /proc/uptime → 2793s (~46 min)
```

Docker Desktop does not start on Windows login. The `docker-desktop` WSL2 VM's own uptime (~46 minutes at the time of this check) lines up with this session's second recovery, not with the container's 51-day-old `Created` date — meaning the VM itself had been shut down and freshly booted since the previous work session, which stops every container inside it, including "minikube."

**The causal chain, end to end:** machine sleeps / Docker Desktop is closed / Windows restarts → nothing auto-starts Docker Desktop → its WSL2 VM (and everything running inside it) is down → next time `minikube`/`kubectl` is used, Docker Desktop and the VM come back up, and minikube (or `docker start`) brings the "minikube" container back with them → because that container's ports were never pinned, Docker hands it a **new** random host port for 8443 → `~/.kube/config`, written during the *previous* session, still points at the *previous* port, until `minikube update-context` regenerates it.

This is not a bug in this repo, and not really a bug in minikube either — it's the documented behavior of an intentionally-dynamic port binding, colliding with a Windows/Docker Desktop setup that doesn't keep the VM alive across sessions. The existing recovery (`minikube start` then `minikube update-context`) is minikube's own supported answer to exactly this, which is why it reliably works every time it's been needed this session — the problem isn't that the fix doesn't work, it's that it has to be diagnosed and applied manually each time.

## Why the obvious-sounding "just pin the port" fix isn't recommended

minikube does support a `--ports=[]` start flag ("List of ports that should be exposed, docker driver only") that can request specific host:container bindings. But this only applies at cluster **creation**, not to an existing profile — applying it now would mean `minikube delete` and a fresh `minikube start --ports=...`, which throws away the running cluster's entire state (the 16 Ditto Things, the type graph, the MQTT connection, Grafana's provisioned dashboard) and would need re-running `scripts/setup_types_and_twins.py` and the rest of the setup sequence from scratch. Every operational doc in this repo already warns against exactly this kind of cluster recreation (`README.md`: *"Do not `minikube delete` or `helm uninstall`"*). The cost of eliminating the drift this way is far larger than the cost of the drift itself (a ~30-60 second manual fix that already has a documented, reliable recovery).

## 2026-09-17 — actually investigating the trigger, not just documenting the symptom again

By this point the drift had happened a third time (once fully unprompted, mid-session, while running an unrelated audit — `minikube status` returned `kubelet: Stopped`, `apiserver: Stopped`, `kubeconfig: Misconfigured`, detected 2026-09-16 23:37:47 local). Three occurrences is enough to actually dig for a trigger instead of re-describing the same symptom a third time. Real hypotheses were checked against real logs — some panned out, one didn't, and that's reported honestly below rather than rounded up.

### Occurrence 3: a real, dated, first-party trigger was found

Checked, in order: Windows' `System` event log for sleep/resume around the detection time, then narrowed to Hyper-V-specific events once the general sleep pattern turned out to be too frequent to be useful on its own.

- **This laptop uses Modern Standby, not classic sleep, and cycles through it constantly** — the `System` log shows dozens of enter/exit-standby events on 2026-09-16 alone (lid open/close, idle timeout, battery-budget events), most lasting seconds to minutes. If "any sleep" caused the drift, it would happen many times a day, not three times in two days. That ruled out "sleep" as the direct trigger and pointed at something rarer.
- **`Microsoft-Windows-Hyper-V-Hypervisor` (the log source for the hypervisor WSL2 itself runs on) shows the hypervisor restarting only 4 times in the ~53 hours checked** (9/15 19:22, 19:24, 19:25 — three restarts within 3 minutes, unrelated to this investigation — then 9/16 21:46:02, then 9/16 23:35/23:38 which is this session's own recovery). That's a far rarer, more discriminating signal than "any standby cycle."
- **The 9/16 21:46:02 restart is directly corroborated by a second, independent log source:** `Microsoft-Windows-Hyper-V-VmSwitch` shows the "WSL (Hyper-V firewall)" virtual switch's NICs and ports being deleted and recreated at 21:46:05–21:46:35 — the concrete signature of the WSL2 virtual machine platform (which hosts the `docker-desktop` VM, which hosts the "minikube" container) being torn down and rebuilt from scratch, not just a lighter-weight process restart.
- **Timing:** this restart happened one second after a `Kernel-Power` "exiting Modern Standby, Reason: Lid" event at 21:44:55 — i.e., opening the laptop lid after a standby period triggered a full WSL2/Hyper-V platform restart within about a minute. The drift itself wasn't detected until 23:37:47, **1h52m later** — consistent with the existing causal chain (the container gets a fresh ephemeral port the moment the platform restarts, but nothing surfaces that as a problem until the next `minikube`/`kubectl` command actually runs).
- **No other candidate event exists in that 1h52m window** — checked directly, not assumed.

**This is real, dated, first-party evidence for occurrence 3's specific trigger: a lid-open/resume-from-Modern-Standby event caused a full WSL2 platform restart, which is what actually rebinds the port** — refining the previous audit's "Docker Desktop doesn't survive across sessions" into the more precise "a specific class of resume, not every resume, tears down WSL2, and that's the moment that matters."

### Occurrences 1 and 2: honestly, not pinned with the same confidence

This audit's own earlier entry recorded occurrence 1 as "2026-09-16, morning UTC" and occurrence 2 as "2026-09-16, evening UTC, ~19h later" — day-part labels, not exact timestamps, because exact times weren't captured when they happened. Searching the event log for a matching WSL2-platform-restart event in the plausible early-morning-local window that "morning UTC" implies **found nothing** — the only organic (not-this-session's-own-recovery) WSL2 restarts in the entire 2026-09-15 19:00 through 2026-09-16 23:46 window checked were at 9/15 19:22–19:25 (three restarts in three minutes, not obviously tied to anything) and 9/15 21:16:24, neither of which lines up cleanly with "2026-09-16 morning."

Stated plainly rather than papered over: **either the original "morning UTC" / "evening UTC" labels were themselves approximate (most likely, since they were never recorded to the minute), or occurrences 1 and 2 had a different or additional trigger this specific signal doesn't capture.** Both are genuinely possible; this session did not find enough to say which. Retention wasn't the limiting factor — the `Hyper-V-VmSwitch-Operational` log's own retention goes back to 2026-07-12, well before the window needed, so the search was real and came up empty, not cut off.

### Is this a known category of issue? Checked, not assumed

- **minikube's own official docs confirm this is an anticipated scenario, not a fluke:** `minikube.sigs.k8s.io/docs/commands/update-context/` describes the command's exact purpose as "Update kubeconfig in case of an IP or port change... corrects kubeconfig if incorrect" — minikube ships a dedicated command for this exact situation because it's common enough to need one. Our recovery procedure (`minikube start` + `minikube update-context`, wrapped in `cluster-preflight.ps1`) is minikube's own recommended fix, not a workaround this repo invented.
- **A cross-platform precedent exists in minikube's own issue tracker:** [kubernetes/minikube#21593](https://github.com/kubernetes/minikube/issues/21593), "Minikube hangs after laptop resumes from sleep mode" (macOS, Docker driver) — a different exact symptom (hang, not port drift) and a different OS, closed unresolved (`lifecycle/rotten`, not fixed) — so it isn't a direct match, but it does corroborate that "Docker-driver minikube plus laptop sleep/resume equals trouble" is a theme that recurs across platforms in minikube's own tracker, not something unique to this repo's setup. No Windows-specific GitHub issue matching this exact port-drift symptom was found in the searching done this session.

### What's actually worth doing about it — no scope creep

**Nothing new needs to be built.** The existing `cluster-preflight.ps1` already does exactly the right thing: it doesn't try to predict or prevent the WSL2 restart (which would mean watching the Windows Event Log continuously in the background — real complexity for a problem that costs about 4 minutes to fix when it's noticed, per this session's own timed recovery), it just detects the resulting symptom cheaply (`minikube status`) and applies minikube's own recommended fix only when needed. That's already the right-sized response to a rare (3 times in the life of this repo), cheap-to-recover-from event.

One genuinely small, optional addition worth naming without building it now: Windows Task Scheduler supports an "On workstation unlock" / "on resume from sleep" trigger, which could run `cluster-preflight.ps1` automatically after a resume instead of waiting for a human to notice the next `kubectl` failure. This is a real option, explicitly **not done here** — it's a standing, persistent system configuration change (a scheduled task that runs on every resume, indefinitely), which is a different category of decision than a one-off script and shouldn't be added without Harrison deciding he wants it, the same way the earlier "Docker Desktop auto-start on login" option was left as his call.

## What's actually worth doing, in order of cost

1. ~~Nothing code-side is required~~ — superseded below; built at Harrison's request.
2. **Low-cost, optional, your call, not done here:** turn on Docker Desktop's "Start Docker Desktop when you sign in to your computer" (Settings → General). Doesn't eliminate the drift (sleep/hibernate and manually closing Docker Desktop still trigger it), but removes the most common trigger — a plain reboot — from the list. Entirely reversible, a Docker Desktop GUI setting, not something this session changed.
3. **Built: [`scripts/cluster-preflight.ps1`](../scripts/cluster-preflight.ps1).** Checks `minikube status` first and only runs `minikube start` + `minikube update-context` if something's actually Stopped/Misconfigured, then waits for all pods Ready. **Important correction found by testing it, not assumed:** the first version ran `minikube start` unconditionally on the theory that it's a safe no-op when already healthy. Tested against a cluster that had been running fine for 52 minutes (confirmed via `docker ps`) — it was *not* a no-op. It knocked every pod back to not-Ready for several minutes (same shape as a genuine cold start: nginx and grafana took longest) before things settled back to healthy on their own. Fixed to check status first and skip the restart entirely when nothing is wrong; re-tested and confirmed it now completes in seconds on a healthy cluster instead of minutes, while the recovery branch (verified separately against the real captured broken-status text from this session, not just assumed) still triggers correctly when something actually needs fixing.
