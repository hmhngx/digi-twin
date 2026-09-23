Plain-text draft for Prof. Siddiqui, written 2026-09-22 after her feedback on the live demo. **Not sent by this session.** No hardware has been ordered, and this message does not say or imply that one is coming or that funding has been discussed (see `notes/PURCHASE_CHECKLIST.md` — still "no order placed"). Copy only the part between the separators.

**Before sending:** the email points her at `notes/DIGITAL_TWIN_ADAPTATION_ANALYSIS.md`. As of this draft that file is **untracked** (not committed, not pushed), so it won't be on GitHub unless it is committed and pushed first.

---

Hi Professor Siddiqui,

Thank you for the feedback on the demo and for pointing me to Micelli & Montagna's paper. I read it against my own code, and I think I see the problem you meant. In their architecture, the twin's state is only ever updated by a shadowing function that reacts to events from something physical, and prediction is an add-on that consumes that real data.

In what I showed you, nothing physical was connected at all. All ten nodes were fed made-up values (one from a time-based formula, the other nine from random numbers). I then checked my predictor against the same formula that generated the data. By the usual Digital Model / Digital Shadow / Digital Twin distinction, that makes the system I demoed a Digital Model, not a digital twin. That's my reading of your feedback, and I'd like to check it with you rather than assume it. The full comparison of the paper against my code is written up in the repo, in notes/DIGITAL_TWIN_ADAPTATION_ANALYSIS.md.

As a first, zero-cost step, I've converted one node to real data from the laptop the system runs on. Its CPU use, memory use, number of open network connections, and measured network round-trip time are now read every cycle and pushed into the twin automatically. I cross-checked those values against the operating system's own counters. When I put load on the CPU, the twin's node follows it.

I've also stopped testing the predictor against a formula. For that node it's now compared only against real readings taken afterwards. That comparison showed my old settings were badly wrong for real data (for example, predicting 45% memory use while the machine was actually at about 88%), and I've corrected them.

What has not changed:
- The other nine nodes are still synthetic, so the system as a whole is a mix: one real node and nine simulated ones.
- Nothing flows back from the twin to the laptop, so even the real node is a digital shadow at most, not a digital twin.
- I haven't ordered any hardware.

Before I put more time into this, could I get your read on whether this is the right direction? In particular: is one real node like this the kind of correction you had in mind, or would you rather I narrow the system down to fewer parts that are all real? And how important is the two-way part — the twin acting back on the physical side — for what you want this project to show?

Harrison

---

## Claim → source checklist (do not paste into the email)

| Claim in the draft | Source |
|---|---|
| Twin state updated only by a shadowing function reacting to physical events; prediction is an augmentation fed real data | Micelli & Montagna, GoodIT '25, §3.1.2 ("The Shadowing Function…", "Augmentations… fed with the physical human actual data") — quoted in `notes/DIGITAL_TWIN_ADAPTATION_ANALYSIS.md` §1.2–1.3, re-checked against the PDF text 2026-09-22 |
| All ten nodes were synthetic until now (one formula, nine random) | `git show 551e87c:twin/publisher.py` (`node_features()`); `notes/DIGITAL_TWIN_ADAPTATION_ANALYSIS.md` §2 |
| Predictor was checked against the generating formula | `notes/PREDICTOR_VALIDATION.md` Part 3a |
| "Digital Model" classification, and that it is my reading, not her verdict | `notes/DIGITAL_TWIN_ADAPTATION_ANALYSIS.md` §3 and §6. Taxonomy commonly attributed to Kritzinger et al., *IFAC-PapersOnLine* 51(11), 2018, 1016–1022 (bibliographic details checked; the paper's text was not re-read) |
| One node now reads real CPU, memory, open connections, round-trip time, automatically | `twin/publisher.py` `measure_host()`; `twin/README.md` "Publisher details" |
| Cross-checked against the OS's own counters; follows CPU load | `README.md` "2026-09-22 live check": Windows `% Processor Time` 87–100 during a 30 s burst while node_0 read 90.3; independent-process and `netstat` comparison |
| Predictor now compared only against later real readings; old settings badly wrong (45 % vs ~88 %) | `notes/PREDICTOR_VALIDATION.md`, 2026-09-22 section (before/after pause checks and backtest) |
| Nine nodes still synthetic; no flow back to the laptop; no hardware ordered | `twin/publisher.py` (`node_features()` else-branch); no actuation code anywhere in `twin/`; `notes/PURCHASE_CHECKLIST.md` |
