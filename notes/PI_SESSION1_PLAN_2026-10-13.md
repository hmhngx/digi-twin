# Pi session 1: plan and pass criteria

Amended 2026-10-13, before the run: section 2 steps 3-5 now keep the logger running during the shakedown schedule and say how to stop; section 3 is unchanged.

Written 2026-10-13, **before the run**. Nothing below has been measured on the Pi, and the pass criteria in section 3 are fixed now: they are not to be changed after the data has been seen. If one has to change, a new dated note says so and this one stays on record. Companion notes: `notes/PI_RUNS_2026-10-12_DRYRUN_AND_LIVE.md` (the live-run figures below come only from there), `notes/PI_ACCURACY_PROTOCOL_2026-10-13.md` (the tools, and the step 0 check), `notes/PI_TELEMETRY_SPEC_2026-10-11.md`.

Source tags, as in the run note: **[analyzer]**, **[recomputed]**, **[user-reported, not on disk]**. One more, as in the spec: **[design]**, a number or rule chosen by us before the run, not a measurement.

## 1. Purpose

1. Validate, on the Pi, the sender from commit 7817f4a, which reads the kernel clock state in-process instead of running `timedatectl` in the loop. The run note (§7.1) records that change as implemented, tests green on Windows and Linux, not validated on the Pi.
2. Shake down the stimulus (`pi/stimulus.py`) and the reference logger (`pi/reference_logger.py`) on the Pi: that they start and stop cleanly, and what they do to the sender.

**No accuracy claim comes out of this session.** The shakedown schedule is 90 s with phases too short for a steady state, and no accuracy figure is to be quoted from it.

## 2. Procedure, in outline

1. As the normal user (not root), the step 0 check of the protocol note §7, and `vcgencmd get_throttled`. Run `vcgencmd get_throttled` again after the last stage.
2. **Sender alone for 10 minutes:** the sender from 7817f4a or later on the Pi, the receiver on the laptop as for the live run. Nothing else is started.
3. **Then the reference logger for 20 minutes,** next to the running sender (`python3 pi/reference_logger.py --out reference_<name>.jsonl`). The logger is NOT stopped before the shakedown; it keeps running.
4. **Then the shakedown schedule,** with the sender and the reference logger still running (`python3 pi/stimulus.py --schedule pi/stimulus_schedule.shakedown.json --out stimulus_<name>.jsonl`; `--print-plan` shows a total of 90 s).
5. After the stimulus has finished, wait about 60 s, then stop the logger with Ctrl+C, then stop the sender last, also with Ctrl+C. The sender prints its `ntp check (end)` line after an interrupt, not after SIGTERM, so criterion B needs Ctrl+C. Keep the sender's stderr: both `ntp check` lines are in it.

## 3. Pass criteria

A. no message with age at send above 100 ms (before the fix: 42 at 258-349 ms);

B. the `ntp check (start)` and `ntp check (end)` lines each show the adjtimex verdict equal to the timedatectl answer; adjtimex None = fail;

C. over the sender-only first 10 minutes (about 200 messages, first message excluded): at most 2 cpu_utilization values above 1.0 %, none of them spaced 60-63 s apart, and at most 2 cpu_frequency_khz readings of 1400000 (live run before the fix: 45 values above 1.0 % of 853, of which 42 in the message after a timedatectl run; 1400000 in 47 of 854, of which 42 there; other messages: 3 of 811 and 5 of 812);

D. report only: the logger's own CPU cost (process_time_s per second) and the change in the sender's idle CPU when the logger starts;

E. shakedown: workers niced +10 and on different cores, sender delivery uninterrupted during the stimulus, no under-voltage bit in `vcgencmd get_throttled` after the run, and whether cpuinfo_cur_freq is readable as the normal user.

## 4. What the numbers are

- **[design]** The limits in C ("at most 2", "at most 2") are a design choice made before the run, not a measurement. The 1.0 % cutoff and the 1400000 value are the ones the run note used for the live run.
- The live-run figures in A and C are **[recomputed]** in the run note (§3.2 for A, §7.1 for C) and are given for comparison only. The "5 of 812" is derived from the run note's figures: 47 − 42 = 5 readings of 1400000 outside the next messages, among 854 − 42 = 812 such messages.
- The 100 ms in A is the cutoff the run note used to define an artifact message (§3.2).
- D has no limit and no pass or fail. Its result is reported as values.
- The logger's own CPU cost is unmeasured before this session, and D reports it. The cost of the sender's `adjtimex` call is not measured by this session either: C looks at the CPU series, not at the call.
