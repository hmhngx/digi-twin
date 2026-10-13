"""pi/stimulus.py: the Pi-side load generator (known CPU duty cycle, known memory allocation).

Everything runs against a fake Pi (clock, /proc readers, worker pool, allocator). Tests that need a real
Linux (fork, sched_setaffinity, real signals) are at the end and skip elsewhere with an explicit reason:

  * RealLinuxTests.test_a_real_cpu_phase_starts_and_stops_its_workers_and_logs_truth
  * RealLinuxTests.test_a_real_sigterm_stops_the_run_and_writes_the_end_event
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "pi" / "stimulus.py"
_spec = importlib.util.spec_from_file_location("stimulus_under_test", SCRIPT)
M = importlib.util.module_from_spec(_spec)
sys.modules["stimulus_under_test"] = M
_spec.loader.exec_module(M)

MIB = 1024 * 1024


class FakePool:
    """Stands in for the worker processes: each 'worker' accrues level% of the elapsed fake time as CPU time."""

    def __init__(self, world, n_workers):
        self.world, self.n_workers = world, n_workers
        self.level, self.last = 0.0, world.t
        self.acc = [0.0] * n_workers
        self.levels = []
        self.stopped = 0

    def _settle(self):
        dt = self.world.t - self.last
        self.acc = [a + dt * self.level / 100.0 for a in self.acc]
        self.last = self.world.t

    def set_level(self, percent):
        self._settle()
        self.level = percent
        self.levels.append(percent)

    def cpu_seconds(self):
        self._settle()
        return list(self.acc)

    def stop(self):
        self.stopped += 1


class FakeWorld:
    """A fake Pi: a clock that only moves when the code sleeps, memory, a temperature, a worker pool."""

    def __init__(self, mem_available_kib=700 * 1024, vmrss_base_kib=9000, temp_c=45.0, alloc_cost_s=0.0):
        self.t = 0.0
        self.alloc_cost_s = alloc_cost_s
        self.sleep_hooks = []
        self.mem_available_kib = mem_available_kib
        self.vmrss_base_kib = vmrss_base_kib
        self.temp_c = temp_c
        self.live_bytes = 0
        self.peak_bytes = 0
        self.alloc_sizes = []
        self.pools = []
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds
        for hook in list(self.sleep_hooks):
            hook(self)

    def alloc(self, nbytes):
        self.t += self.alloc_cost_s  # touching every page takes time
        self.live_bytes += nbytes
        self.peak_bytes = max(self.peak_bytes, self.live_bytes)
        self.alloc_sizes.append(nbytes)
        return ("buf", nbytes)

    def release(self, buf):
        self.live_bytes -= buf[1]

    def make_pool(self, n_workers):
        pool = FakePool(self, n_workers)
        self.pools.append(pool)
        return pool

    def env(self):
        return M.Env(
            now_ms=lambda: 1_791_000_000_000 + int(round(self.t * 1000)),
            mono_ns=lambda: 5_000_000_000_000 + int(round(self.t * 1e9)),
            sleep=self.sleep,
            read_mem_available_kib=lambda: self.mem_available_kib - self.live_bytes // 1024,
            read_vmrss_kib=lambda: self.vmrss_base_kib + self.live_bytes // 1024,
            read_temp_c=lambda: self.temp_c,
            alloc=self.alloc,
            release=self.release,
            make_pool=self.make_pool,
            n_cpus=4,
        )


def phases(*specs):
    """specs: (name, kind, level, duration_s)"""
    return [M.Phase(*s) for s in specs]


def run_stimulus(world, plan, **kw):
    events = []
    kw.setdefault("seed", None)
    stim = M.Stimulus(plan, events.append, world.env(), **kw)
    return stim, events, stim.run()


def of_type(events, kind):
    return [e for e in events if e["type"] == kind]


class ScheduleTests(unittest.TestCase):
    def good(self):
        return {"phases": [{"name": "idle0", "kind": "idle", "level": 0, "duration_s": 60},
                           {"name": "cpu50", "kind": "cpu", "level": 50, "duration_s": 60},
                           {"name": "mem100", "kind": "mem", "level": 100, "duration_s": 60}]}

    def test_a_valid_schedule_parses_in_order(self):
        plan = M.parse_schedule(self.good())
        self.assertEqual([(p.name, p.kind, p.level, p.duration_s) for p in plan],
                         [("idle0", "idle", 0, 60), ("cpu50", "cpu", 50, 60), ("mem100", "mem", 100, 60)])

    def test_invalid_schedules_are_refused(self):
        def mutate(**changes):
            obj = self.good()
            obj["phases"][1].update(changes)
            return obj

        bad = {
            "not an object": [],
            "no phases": {"phases": []},
            "unknown kind": mutate(kind="gpu"),
            "cpu over 100": mutate(level=101),
            "cpu negative": mutate(level=-1),
            "cpu not a number": mutate(level="50"),
            "zero duration": mutate(duration_s=0),
            "negative duration": mutate(duration_s=-5),
            "boolean level": mutate(level=True),
            "duplicate name": mutate(name="idle0"),
            "missing name": {"phases": [{"kind": "idle", "level": 0, "duration_s": 5}]},
        }
        for label, obj in bad.items():
            with self.subTest(label):
                with self.assertRaises(M.ScheduleError):
                    M.parse_schedule(obj)

    def test_mem_level_must_be_a_positive_number_of_mib(self):
        for level in (0, -3, "100"):
            with self.subTest(level=level):
                obj = self.good()
                obj["phases"][2]["level"] = level
                with self.assertRaises(M.ScheduleError):
                    M.parse_schedule(obj)

    def test_the_hard_cap_on_total_duration_is_enforced(self):
        long_plan = phases(("a", "idle", 0, M.HARD_MAX_TOTAL_S), ("b", "idle", 0, 1))
        with self.assertRaises(M.ScheduleError):
            M.check_total(long_plan, None)
        ok = phases(("a", "idle", 0, 100))
        M.check_total(ok, None)
        with self.assertRaises(M.ScheduleError):
            M.check_total(ok, 99)  # a lower cap given on the command line
        with self.assertRaises(M.ScheduleError):
            M.check_total(ok, M.HARD_MAX_TOTAL_S + 1)  # the hard cap cannot be raised

    def test_shuffle_is_seeded_and_keeps_groups_together_and_fixed_phases_in_place(self):
        plan = M.parse_schedule({"phases": (
            [{"name": "idle0", "kind": "idle", "level": 0, "duration_s": 5, "fixed": True}]
            + [p for i in range(6) for p in (
                {"name": "a%d" % i, "kind": "cpu", "level": 10 * (i + 1), "duration_s": 5, "group": "g%d" % i},
                {"name": "r%d" % i, "kind": "idle", "level": 0, "duration_s": 5, "group": "g%d" % i})])})
        first = M.order_phases(plan, 7)
        again = M.order_phases(plan, 7)
        self.assertEqual([p.name for p in first], [p.name for p in again])  # same seed, same order
        self.assertEqual(first[0].name, "idle0")  # fixed phase stays first
        names = [p.name for p in first]
        for i in range(6):  # every activity is still directly followed by its own recovery phase
            self.assertEqual(names[names.index("a%d" % i) + 1], "r%d" % i)
        self.assertEqual(sorted(names), sorted(p.name for p in plan))
        others = {tuple(p.name for p in M.order_phases(plan, s)) for s in range(1, 12)}
        self.assertGreater(len(others), 1)  # different seeds do reorder
        self.assertEqual([p.name for p in M.order_phases(plan, None)], [p.name for p in plan])  # no seed: as written

    def test_total_duration_is_the_sum(self):
        self.assertEqual(M.total_duration(phases(("a", "idle", 0, 5), ("b", "cpu", 10, 7.5))), 12.5)


class MemorySafetyTests(unittest.TestCase):
    RESERVE = 200

    def refusals(self, available_mib, request_mib):
        world = FakeWorld(mem_available_kib=available_mib * 1024)
        stim = M.Stimulus(phases(("m", "mem", request_mib, 60)), lambda e: None, world.env(), seed=None)
        return stim.preflight()

    def test_exactly_the_reserve_left_is_allowed_one_mib_less_is_refused(self):
        self.assertEqual(self.refusals(500, 300), [])  # 200 MiB left: "at least 200" holds
        refused = self.refusals(500, 301)  # 199 MiB left
        self.assertEqual(len(refused), 1)
        self.assertIn("m", refused[0])
        self.assertIn("200", refused[0])

    def test_a_request_larger_than_the_machine_is_refused(self):
        self.assertEqual(len(self.refusals(500, 4096)), 1)

    def test_an_unreadable_meminfo_refuses_mem_phases_rather_than_guessing(self):
        world = FakeWorld()
        env = world.env()._replace(read_mem_available_kib=lambda: None)
        stim = M.Stimulus(phases(("m", "mem", 10, 60)), lambda e: None, env, seed=None)
        self.assertEqual(len(stim.preflight()), 1)

    def test_an_unsafe_schedule_is_refused_before_anything_starts(self):
        world = FakeWorld(mem_available_kib=500 * 1024)
        plan = phases(("c", "cpu", 50, 60), ("m", "mem", 400, 60))
        events = []
        stim = M.Stimulus(plan, events.append, world.env(), seed=None)
        with self.assertRaises(M.SafetyRefusal):
            stim.run()
        self.assertEqual(world.pools, [])  # no workers spawned
        self.assertEqual(world.alloc_sizes, [])  # nothing allocated
        self.assertEqual(events, [])  # and nothing logged: there is no run

    def test_memory_that_shrinks_between_phases_stops_the_run_at_the_phase_that_would_not_fit(self):
        world = FakeWorld(mem_available_kib=700 * 1024)
        plan = phases(("m1", "mem", 100, 30), ("gap", "idle", 0, 30), ("m2", "mem", 300, 30))
        # another process eats memory while m1 runs: at m2's start only 400 MiB are available
        world.sleep_hooks.append(lambda w: setattr(w, "mem_available_kib", 400 * 1024 + w.live_bytes // 1024) if w.t >= 45 else None)
        stim, events, reason = run_stimulus(world, plan)
        self.assertEqual(reason, "refused")
        refused = of_type(events, "phase_refused")
        self.assertEqual([e["name"] for e in refused], ["m2"])
        self.assertEqual(world.live_bytes, 0)
        self.assertEqual(of_type(events, "end")[0]["reason"], "refused")
        self.assertEqual(world.alloc_sizes, [100 * MIB])  # m2 was never allocated


class RunTests(unittest.TestCase):
    def test_every_phase_has_start_and_end_with_both_clocks_and_the_seed_is_logged(self):
        world = FakeWorld()
        plan = phases(("idle0", "idle", 0, 10), ("cpu50", "cpu", 50, 10))
        stim, events, reason = run_stimulus(world, plan, seed=42)
        self.assertEqual(reason, "completed")
        start = of_type(events, "start")[0]
        self.assertEqual(start["seed"], 42)
        self.assertEqual(start["order"], ["idle0", "cpu50"])
        self.assertEqual((start["n_workers"], start["n_cpus"]), (4, 4))
        self.assertEqual(start["vmrss_kib_baseline"], 9000)
        for ev in of_type(events, "phase_start") + of_type(events, "phase_end") + [start, of_type(events, "end")[0]]:
            self.assertIsInstance(ev["t_epoch_ms"], int)
            self.assertIsInstance(ev["t_mono_ns"], int)
        begin, finish = of_type(events, "phase_start")[1], of_type(events, "phase_end")[1]
        self.assertEqual(finish["t_epoch_ms"] - begin["t_epoch_ms"], 10_000)
        self.assertEqual(finish["t_mono_ns"] - begin["t_mono_ns"], 10_000_000_000)
        self.assertEqual((finish["name"], finish["kind"], finish["level"], finish["status"]), ("cpu50", "cpu", 50, "completed"))

    def test_cpu_phase_logs_the_achieved_duty_per_worker_and_in_total(self):
        world = FakeWorld()
        stim, events, _ = run_stimulus(world, phases(("idle0", "idle", 0, 10), ("cpu25", "cpu", 25, 20)))
        idle_end, cpu_end = of_type(events, "phase_end")
        self.assertAlmostEqual(idle_end["achieved_total_percent"], 0.0, places=6)
        self.assertEqual(len(cpu_end["worker_duty"]), 4)
        for duty in cpu_end["worker_duty"]:
            self.assertAlmostEqual(duty, 0.25, places=6)
        self.assertAlmostEqual(cpu_end["achieved_total_percent"], 25.0, places=6)
        self.assertEqual(world.pools[0].levels, [0, 25, 0])  # level set at phase start, back to 0 after
        self.assertEqual(world.pools[0].stopped, 1)

    def test_achieved_total_percent_is_relative_to_all_cpus_not_just_to_the_workers(self):
        world = FakeWorld()  # 4 cpus
        stim, events, _ = run_stimulus(world, phases(("c", "cpu", 50, 20)), workers=2)
        end = of_type(events, "phase_end")[0]
        self.assertEqual(len(end["worker_duty"]), 2)
        for duty in end["worker_duty"]:
            self.assertAlmostEqual(duty, 0.5, places=6)  # each worker really ran half of the time
        self.assertAlmostEqual(end["achieved_total_percent"], 25.0, places=6)  # 2 workers x 50 % of 4 cpus, like /proc/stat's aggregate
        self.assertEqual(of_type(events, "start")[0]["n_workers"], 2)

    def test_the_allocation_time_of_a_mem_phase_is_logged_so_the_ramp_is_not_hidden(self):
        world = FakeWorld(alloc_cost_s=2.0)
        stim, events, _ = run_stimulus(world, phases(("m", "mem", 100, 10)))
        end = of_type(events, "phase_end")[0]
        self.assertAlmostEqual(end["mem_alloc_wall_s"], 2.0, places=6)
        self.assertAlmostEqual(end["wall_s"], 12.0, places=6)  # the hold of duration_s starts after the allocation
        self.assertIsNone(of_type(run_stimulus(FakeWorld(), phases(("c", "cpu", 10, 5)))[1], "phase_end")[0]["mem_alloc_wall_s"])

    def test_no_workers_are_spawned_for_a_schedule_without_cpu_phases(self):
        world = FakeWorld()
        stim, events, _ = run_stimulus(world, phases(("i", "idle", 0, 5), ("m", "mem", 50, 5)))
        self.assertEqual(world.pools, [])
        self.assertIsNone(of_type(events, "phase_end")[0]["achieved_total_percent"])

    def test_mem_phase_allocates_the_request_logs_rss_at_mid_phase_and_frees_it(self):
        world = FakeWorld()
        stim, events, _ = run_stimulus(world, phases(("m100", "mem", 100, 40)))
        self.assertEqual(world.alloc_sizes, [100 * MIB])
        end = of_type(events, "phase_end")[0]
        self.assertEqual(end["vmrss_kib_mid"], 9000 + 100 * 1024)  # truth: what the process really holds mid-phase
        self.assertEqual(end["mem_alloc_mib"], 100)
        self.assertEqual(end["mem_available_kib_at_start"], 700 * 1024)
        self.assertEqual(world.live_bytes, 0)  # freed at the end of the phase
        mid = [s for s in range(len(world.sleeps))]  # the wait was sliced, so a stop request is seen quickly
        self.assertTrue(all(s <= M.SLICE_S + 1e-9 for s in world.sleeps))
        self.assertTrue(mid)

    def test_memory_is_freed_at_the_end_of_each_phase_so_consecutive_mem_phases_never_hold_two_allocations(self):
        world = FakeWorld()
        stim, events, _ = run_stimulus(world, phases(("m1", "mem", 100, 10), ("m2", "mem", 150, 10), ("m3", "mem", 100, 10)))
        self.assertEqual(world.alloc_sizes, [100 * MIB, 150 * MIB, 100 * MIB])
        self.assertEqual(world.peak_bytes, 150 * MIB)  # never 250 MiB: each phase released its buffer before the next began
        self.assertEqual(world.live_bytes, 0)

    def test_sigterm_during_a_cpu_phase_stops_the_workers_and_writes_the_end_event(self):
        world = FakeWorld()
        plan = phases(("idle0", "idle", 0, 60), ("cpu50", "cpu", 50, 60), ("never", "idle", 0, 60))
        holder = {}
        world.sleep_hooks.append(lambda w: holder["stim"].request_stop("SIGTERM") if w.t >= 70 else None)
        events = []
        holder["stim"] = stim = M.Stimulus(plan, events.append, world.env(), seed=None)
        reason = stim.run()
        self.assertEqual(reason, "SIGTERM")
        ends = of_type(events, "phase_end")
        self.assertEqual([(e["name"], e["status"]) for e in ends], [("idle0", "completed"), ("cpu50", "interrupted")])
        self.assertNotIn("never", [e["name"] for e in of_type(events, "phase_start")])
        self.assertGreaterEqual(world.pools[0].stopped, 1)  # workers stopped
        self.assertEqual(events[-1]["type"], "end")
        self.assertEqual(events[-1]["reason"], "SIGTERM")
        self.assertLess(world.t, 75)  # and promptly: within a sleep slice of the signal

    def test_sigint_during_a_mem_phase_frees_the_memory(self):
        world = FakeWorld()
        holder = {}
        world.sleep_hooks.append(lambda w: holder["stim"].request_stop("SIGINT") if w.t >= 10 else None)
        events = []
        holder["stim"] = stim = M.Stimulus(phases(("m", "mem", 200, 60)), events.append, world.env(), seed=None)
        self.assertEqual(stim.run(), "SIGINT")
        self.assertEqual(world.live_bytes, 0)
        self.assertEqual(of_type(events, "phase_end")[0]["status"], "interrupted")
        self.assertEqual(events[-1]["reason"], "SIGINT")

    def test_an_exception_inside_a_phase_still_cleans_up_and_writes_the_end_event(self):
        world = FakeWorld()

        def boom(w):
            if w.t >= 5:
                raise RuntimeError("simulated failure")

        world.sleep_hooks.append(boom)
        events = []
        stim = M.Stimulus(phases(("c", "cpu", 50, 60)), events.append, world.env(), seed=None)
        with self.assertRaises(RuntimeError):
            stim.run()
        self.assertGreaterEqual(world.pools[0].stopped, 1)
        self.assertEqual(events[-1]["type"], "end")
        self.assertEqual(events[-1]["reason"], "error")

    def test_the_first_stop_reason_wins(self):
        world = FakeWorld()
        holder = {}

        def hook(w):
            holder["stim"].request_stop("SIGTERM")
            holder["stim"].request_stop("SIGINT")

        world.sleep_hooks.append(hook)
        events = []
        holder["stim"] = M.Stimulus(phases(("i", "idle", 0, 5)), events.append, world.env(), seed=None)
        self.assertEqual(holder["stim"].run(), "SIGTERM")


class ThermalGuardTests(unittest.TestCase):
    def test_a_cpu_phase_stops_when_the_soc_reaches_the_limit(self):
        world = FakeWorld(temp_c=60.0)
        world.sleep_hooks.append(lambda w: setattr(w, "temp_c", 81.0) if w.t >= 20 else None)
        stim, events, reason = run_stimulus(world, phases(("c", "cpu", 100, 120)), max_temp_c=80.0)
        self.assertEqual(reason, "thermal-limit")
        self.assertEqual(of_type(events, "phase_end")[0]["status"], "interrupted")
        self.assertLess(world.t, 30)

    def test_an_unreadable_temperature_refuses_cpu_phases_unless_the_guard_is_switched_off(self):
        world = FakeWorld()
        env = world.env()._replace(read_temp_c=lambda: None)
        plan = phases(("c", "cpu", 50, 10))
        self.assertEqual(len(M.Stimulus(plan, lambda e: None, env, seed=None).preflight()), 1)
        self.assertEqual(M.Stimulus(plan, lambda e: None, env, seed=None, thermal_guard=False).preflight(), [])
        mem_only = phases(("m", "mem", 10, 10))
        self.assertEqual(M.Stimulus(mem_only, lambda e: None, env, seed=None).preflight(), [])


class OutputFileTests(unittest.TestCase):
    def test_open_exclusive_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "stim.jsonl"
            path.write_text("keep me\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                M.open_exclusive(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "keep me\n")
            fresh = Path(d) / "new.jsonl"
            with M.open_exclusive(fresh) as handle:
                handle.write("x\n")
            self.assertTrue(fresh.exists())

    def test_main_refuses_an_existing_output_file_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            sched = Path(d) / "s.json"
            sched.write_text(json.dumps({"phases": [{"name": "i", "kind": "idle", "level": 0, "duration_s": 5}]}), encoding="utf-8")
            out = Path(d) / "out.jsonl"
            out.write_text("keep me\n", encoding="utf-8")
            world = FakeWorld()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = M.main(["--schedule", str(sched), "--out", str(out)], env=world.env())
            self.assertEqual(code, 2)
            self.assertEqual(out.read_text(encoding="utf-8"), "keep me\n")
            self.assertIn("exists", err.getvalue())
            self.assertEqual(world.sleeps, [])

    def test_main_writes_a_jsonl_log_with_one_event_per_line_and_exit_code_zero(self):
        with tempfile.TemporaryDirectory() as d:
            sched = Path(d) / "s.json"
            sched.write_text(json.dumps({"phases": [{"name": "i", "kind": "idle", "level": 0, "duration_s": 3}]}), encoding="utf-8")
            out = Path(d) / "out.jsonl"
            world = FakeWorld()
            code = M.main(["--schedule", str(sched), "--out", str(out), "--shuffle-seed", "5"], env=world.env())
            self.assertEqual(code, 0)
            lines = out.read_text(encoding="utf-8").splitlines()
            kinds = [json.loads(line)["type"] for line in lines]
            self.assertEqual(kinds, ["start", "phase_start", "phase_end", "end"])
            self.assertEqual(json.loads(lines[0])["seed"], 5)

    def test_main_returns_1_when_the_run_ends_early_and_3_when_a_safety_refusal_precedes_it(self):
        with tempfile.TemporaryDirectory() as d:
            sched = Path(d) / "s.json"
            sched.write_text(json.dumps({"phases": [{"name": "m", "kind": "mem", "level": 400, "duration_s": 3}]}), encoding="utf-8")
            out = Path(d) / "out.jsonl"
            world = FakeWorld(mem_available_kib=500 * 1024)
            with contextlib.redirect_stderr(io.StringIO()):
                code = M.main(["--schedule", str(sched), "--out", str(out)], env=world.env())
            self.assertEqual(code, 3)
            self.assertFalse(out.exists())  # refused before the log was even created


class PrintPlanTests(unittest.TestCase):
    def test_print_plan_lists_phases_and_total_and_spawns_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            sched = Path(d) / "s.json"
            sched.write_text(json.dumps({"phases": [
                {"name": "idle0", "kind": "idle", "level": 0, "duration_s": 300},
                {"name": "cpu75", "kind": "cpu", "level": 75, "duration_s": 90},
                {"name": "mem300", "kind": "mem", "level": 300, "duration_s": 60}]}), encoding="utf-8")
            world = FakeWorld(mem_available_kib=700 * 1024)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = M.main(["--schedule", str(sched), "--print-plan"], env=world.env())
            text = out.getvalue()
            self.assertEqual(code, 0)
            for needle in ("idle0", "cpu75", "mem300", "450", "total"):
                self.assertIn(needle, text)
            self.assertEqual(world.pools, [])
            self.assertEqual(world.alloc_sizes, [])
            self.assertEqual(world.sleeps, [])
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["s.json"])  # no output file

    def test_print_plan_says_when_a_mem_phase_would_be_refused_and_still_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            sched = Path(d) / "s.json"
            sched.write_text(json.dumps({"phases": [{"name": "huge", "kind": "mem", "level": 900, "duration_s": 60}]}), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = M.main(["--schedule", str(sched), "--print-plan"], env=FakeWorld(mem_available_kib=700 * 1024).env())
            self.assertEqual(code, 0)
            self.assertIn("WOULD BE REFUSED", out.getvalue())

    def test_print_plan_works_where_meminfo_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as d:
            sched = Path(d) / "s.json"
            sched.write_text(json.dumps({"phases": [{"name": "m", "kind": "mem", "level": 50, "duration_s": 60}]}), encoding="utf-8")
            env = FakeWorld().env()._replace(read_mem_available_kib=lambda: None)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(M.main(["--schedule", str(sched), "--print-plan"], env=env), 0)
            self.assertIn("unavailable", out.getvalue())

    def test_print_plan_applies_the_seeded_shuffle_and_prints_the_seed(self):
        with tempfile.TemporaryDirectory() as d:
            sched = Path(d) / "s.json"
            sched.write_text(json.dumps({"phases": [
                {"name": "p%d" % i, "kind": "idle", "level": 0, "duration_s": 5} for i in range(8)]}), encoding="utf-8")
            outputs = []
            for _ in range(2):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    M.main(["--schedule", str(sched), "--print-plan", "--shuffle-seed", "11"], env=FakeWorld().env())
                outputs.append(out.getvalue())
            self.assertEqual(outputs[0], outputs[1])
            self.assertIn("seed 11", outputs[0])


class ExampleScheduleTests(unittest.TestCase):
    """pi/stimulus_schedule.example.json is the schedule of notes/PI_ACCURACY_PROTOCOL_*.md."""

    PATH = SCRIPT.parent / "stimulus_schedule.example.json"

    def test_it_is_the_protocol_schedule(self):
        plan = M.load_schedule(self.PATH)
        M.check_total(plan, None)
        self.assertEqual((plan[0].name, plan[0].kind, plan[0].duration_s, plan[0].fixed), ("idle_start", "idle", 300, True))
        active = [p for p in plan if p.kind != "idle"]
        self.assertEqual(len(active), 14)  # seven activities, repeated twice
        self.assertTrue(all(p.duration_s >= 60 for p in active))
        for rep in ("_r1", "_r2"):
            self.assertEqual(sorted((p.kind, p.level) for p in active if p.name.endswith(rep)),
                             [("cpu", 25), ("cpu", 50), ("cpu", 75), ("cpu", 100), ("mem", 100), ("mem", 200), ("mem", 300)])
        self.assertEqual(M.total_duration(plan), 1980)

    def test_after_any_seed_the_idle_start_is_first_and_every_activity_is_followed_by_its_own_rest(self):
        plan = M.load_schedule(self.PATH)
        for seed in (1, 2, 3, 2026):
            ordered = M.order_phases(plan, seed)
            names = [p.name for p in ordered]
            self.assertEqual(names[0], "idle_start")
            for p in ordered:
                if p.kind != "idle":
                    self.assertEqual(names[names.index(p.name) + 1], "rest_" + p.name)
            self.assertEqual(sorted(names), sorted(p.name for p in plan))


class ShakedownScheduleTests(unittest.TestCase):
    """pi/stimulus_schedule.shakedown.json is the 90-second check that the stimulus runs on the Pi (session 1).
    It is not a measurement schedule: its phases are too short for a steady state."""

    PATH = SCRIPT.parent / "stimulus_schedule.shakedown.json"

    def test_it_is_the_ninety_second_shakedown(self):
        plan = M.load_schedule(self.PATH)
        M.check_total(plan, None)
        self.assertEqual([(p.name, p.kind, p.level, p.duration_s, p.group, p.fixed) for p in plan], [
            ("idle_start", "idle", 0, 30, None, True),
            ("cpu50", "cpu", 50, 20, "cpu50", False),
            ("rest_cpu50", "idle", 0, 10, "cpu50", False),
            ("mem100", "mem", 100, 20, "mem100", False),
            ("rest_mem100", "idle", 0, 10, "mem100", False),
        ])
        self.assertEqual(M.total_duration(plan), 90)

    def test_it_has_the_format_and_keys_of_the_example_schedule(self):
        raw = json.loads(self.PATH.read_text(encoding="utf-8"))
        example = json.loads(ExampleScheduleTests.PATH.read_text(encoding="utf-8"))
        self.assertEqual(list(raw), list(example))  # the one top-level key, "phases"
        # fixed and group appear, as the example uses them
        self.assertEqual({k for p in raw["phases"] for k in p}, {k for p in example["phases"] for k in p})

    def test_after_any_seed_the_idle_start_is_first_and_every_activity_is_followed_by_its_own_rest(self):
        plan = M.load_schedule(self.PATH)
        for seed in (1, 2, 3, 2026):
            names = [p.name for p in M.order_phases(plan, seed)]
            self.assertEqual(names[0], "idle_start")
            for activity in ("cpu50", "mem100"):
                self.assertEqual(names[names.index(activity) + 1], "rest_" + activity)
            self.assertEqual(sorted(names), sorted(p.name for p in plan))

    def test_print_plan_parses_it_and_prints_a_total_of_90_s(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(M.main(["--schedule", str(self.PATH), "--print-plan"], env=FakeWorld().env()), 0)
        self.assertIn("total 90 s", out.getvalue())


class PinReportTests(unittest.TestCase):
    def test_it_waits_until_every_worker_has_reported_its_core(self):
        reads = iter([[-2, -2], [0, -2], [0, 1]])
        slept = []
        got = M.wait_until_reported(lambda: next(reads), slept.append, lambda: 0.0, timeout_s=2.0)
        self.assertEqual(got, [0, 1])
        self.assertEqual(len(slept), 2)

    def test_it_gives_up_after_the_timeout_and_returns_what_it_has_so_the_log_can_say_not_reported(self):
        clock = {"now": 0.0}
        got = M.wait_until_reported(lambda: [0, -2], lambda s: clock.__setitem__("now", clock["now"] + s),
                                    lambda: clock["now"], timeout_s=0.5)
        self.assertEqual(got, [0, -2])
        self.assertGreaterEqual(clock["now"], 0.5)


class ReviewFixTests(unittest.TestCase):
    def test_a_nan_or_infinite_cap_cannot_bypass_the_hard_cap(self):
        huge = phases(("a", "idle", 0, 1e9))
        for bad in (float("nan"), float("inf"), float("-inf"), 0, -5):
            with self.subTest(cap=bad):
                with self.assertRaises(M.ScheduleError):
                    M.check_total(huge, bad)
        with self.assertRaises(M.ScheduleError):
            M.check_total(huge, None)  # and with no flag at all

    def test_dangerous_command_line_values_are_refused_before_anything_runs(self):
        with tempfile.TemporaryDirectory() as d:
            sched = Path(d) / "s.json"
            sched.write_text(json.dumps({"phases": [{"name": "i", "kind": "idle", "level": 0, "duration_s": 5}]}), encoding="utf-8")
            for bad in (["--max-total-s", "nan"], ["--max-total-s", "inf"], ["--max-total-s", "0"], ["--workers", "100000"],
                        ["--workers", "0"], ["--reserve-mib", "50"], ["--reserve-mib", "-10"], ["--max-temp-c", "90"],
                        ["--max-temp-c", "0"]):
                with self.subTest(bad=bad):
                    with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
                        M.main(["--schedule", str(sched), "--print-plan"] + bad, env=FakeWorld().env())
                    self.assertEqual(ctx.exception.code, 2)

    def test_safe_values_are_still_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            sched = Path(d) / "s.json"
            sched.write_text(json.dumps({"phases": [{"name": "i", "kind": "idle", "level": 0, "duration_s": 5}]}), encoding="utf-8")
            argv = ["--schedule", str(sched), "--print-plan", "--workers", "2", "--reserve-mib", "300", "--max-temp-c", "70", "--max-total-s", "100"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(M.main(argv, env=FakeWorld().env()), 0)

    def test_memory_that_shrinks_between_the_two_preflights_is_a_refusal_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as d:
            sched = Path(d) / "s.json"
            sched.write_text(json.dumps({"phases": [{"name": "m", "kind": "mem", "level": 300, "duration_s": 5}]}), encoding="utf-8")
            reads = {"n": 0}

            def read():
                reads["n"] += 1
                return 700 * 1024 if reads["n"] == 1 else 100 * 1024  # fine for main()'s check, gone by run()'s

            env = FakeWorld().env()._replace(read_mem_available_kib=read)
            with contextlib.redirect_stderr(io.StringIO()) as err:
                code = M.main(["--schedule", str(sched), "--out", str(Path(d) / "o.jsonl")], env=env)
            self.assertEqual(code, 3)
            self.assertIn("REFUSED", err.getvalue())

    def test_a_temperature_that_becomes_unreadable_mid_run_is_logged_once_not_ignored_silently(self):
        world = FakeWorld(temp_c=50.0)
        world.sleep_hooks.append(lambda w: setattr(w, "temp_c", None) if w.t >= 10 else None)
        stim, events, reason = run_stimulus(world, phases(("c", "cpu", 50, 30)))
        self.assertEqual(reason, "completed")
        warnings = of_type(events, "warning")
        self.assertEqual(len(warnings), 1)
        self.assertIn("thermal guard", warnings[0]["message"])

    def test_the_plan_does_not_claim_total_capacity_when_workers_and_cpus_differ(self):
        world = FakeWorld()
        text = M.plan_text(phases(("c", "cpu", 25, 10)), None, world.env(), 200, workers=2)
        self.assertIn("this machine has 4 cpus", text)
        self.assertIn("only if", text)

    def test_an_unmeasured_allocation_wait_is_documented_in_the_module_text(self):
        self.assertIn("allocation is not interruptible", M.__doc__)
        self.assertIn("SIGHUP", M.__doc__)


class WorkerHelpersTests(unittest.TestCase):
    def test_a_worker_exits_when_its_parent_is_gone(self):
        self.assertFalse(M.worker_should_exit(parent_pid=100, current_ppid=100, stop_set=False))
        self.assertTrue(M.worker_should_exit(parent_pid=100, current_ppid=1, stop_set=False))  # orphaned
        self.assertTrue(M.worker_should_exit(parent_pid=100, current_ppid=100, stop_set=True))

    def test_the_busy_share_of_a_period_is_level_percent_and_level_100_never_sleeps(self):
        self.assertAlmostEqual(M.busy_seconds(25, 0.1), 0.025)
        self.assertAlmostEqual(M.busy_seconds(0, 0.1), 0.0)
        self.assertEqual(M.busy_seconds(100, 0.1), 0.1)
        self.assertEqual(M.sleep_seconds(100, 0.1), 0.0)  # level 100 never sleeps
        self.assertAlmostEqual(M.sleep_seconds(25, 0.1), 0.075)

    def test_cores_are_assigned_round_robin_over_the_allowed_cpus(self):
        self.assertEqual(M.assign_cpus(4, [0, 1, 2, 3]), [0, 1, 2, 3])
        self.assertEqual(M.assign_cpus(4, [2, 5]), [2, 5, 2, 5])  # fewer cores than workers: doubled up, not an error


LINUX = sys.platform.startswith("linux")


@unittest.skipUnless(LINUX, "needs Linux: fork, os.sched_setaffinity, os.nice and real signals")
class RealLinuxTests(unittest.TestCase):
    def schedule_file(self, d, phases_):
        path = Path(d) / "s.json"
        path.write_text(json.dumps({"phases": phases_}), encoding="utf-8")
        return path

    def test_a_real_cpu_phase_starts_and_stops_its_workers_and_logs_truth(self):
        with tempfile.TemporaryDirectory() as d:
            sched = self.schedule_file(d, [{"name": "i", "kind": "idle", "level": 0, "duration_s": 1},
                                           {"name": "c", "kind": "cpu", "level": 50, "duration_s": 2}])
            out = Path(d) / "out.jsonl"
            done = subprocess.run([sys.executable, str(SCRIPT), "--schedule", str(sched), "--out", str(out), "--workers", "2",
                                   "--no-thermal-guard"], capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            events = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["reason"], "completed")
            pinned = events[0]["pinned_cpus"]  # logged only after every worker has said which core it got
            self.assertEqual(len(pinned), 2)
            allowed = os.sched_getaffinity(0)
            self.assertTrue(all(c in allowed for c in pinned), pinned)  # -2 = not reported, -1 = pinning failed: neither is acceptable here
            cpu_end = [e for e in events if e["type"] == "phase_end" and e["name"] == "c"][0]
            # a very loose sanity bound, NOT an accuracy claim: the workers did some work, and not all of it
            self.assertGreater(cpu_end["achieved_total_percent"], 0.0)  # machine-independent (2 workers over any number of cpus)
            self.assertLess(cpu_end["achieved_total_percent"], 95.0)
            self.assertTrue(all(0.05 < d < 0.95 for d in cpu_end["worker_duty"]), cpu_end["worker_duty"])
            self.assertEqual(len(cpu_end["worker_duty"]), 2)

    def test_a_real_sigterm_stops_the_run_and_writes_the_end_event(self):
        with tempfile.TemporaryDirectory() as d:
            sched = self.schedule_file(d, [{"name": "long", "kind": "idle", "level": 0, "duration_s": 120}])
            out = Path(d) / "out.jsonl"
            proc = subprocess.Popen([sys.executable, str(SCRIPT), "--schedule", str(sched), "--out", str(out)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if out.exists() and '"phase_start"' in out.read_text(encoding="utf-8"):
                        break
                    time.sleep(0.1)
                else:
                    self.fail("the run never started")
                proc.send_signal(signal.SIGTERM)
                proc.wait(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.stderr.close()
            events = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["type"], "end")
            self.assertEqual(events[-1]["reason"], "SIGTERM")
            self.assertEqual([e for e in events if e["type"] == "phase_end"][0]["status"], "interrupted")

    def wait_for(self, path, needle, seconds=15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if path.exists() and needle in path.read_text(encoding="utf-8"):
                return
            time.sleep(0.1)
        self.fail("%r never appeared in %s" % (needle, path))

    def leftovers(self, marker):
        found = []
        for pid in os.listdir("/proc"):
            if pid.isdigit():
                try:
                    with open("/proc/%s/cmdline" % pid, "rb") as handle:
                        if marker.encode() in handle.read():
                            found.append(pid)
                except OSError:
                    pass
        return found

    def run_and_signal(self, sched, out, extra_args, sig, needle):
        proc = subprocess.Popen([sys.executable, str(SCRIPT), "--schedule", str(sched), "--out", str(out)] + extra_args,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            self.wait_for(out, needle)
            proc.send_signal(sig)
            proc.wait(timeout=20)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.stderr.close()
        return [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]

    def test_a_real_sigterm_during_a_cpu_phase_stops_the_workers_and_leaves_none_behind(self):
        with tempfile.TemporaryDirectory() as d:
            sched = self.schedule_file(d, [{"name": "c", "kind": "cpu", "level": 50, "duration_s": 120}])
            events = self.run_and_signal(sched, Path(d) / "out.jsonl", ["--workers", "2", "--no-thermal-guard"], signal.SIGTERM, '"phase_start"')
            self.assertEqual(events[-1]["reason"], "SIGTERM")
            end = [e for e in events if e["type"] == "phase_end"][0]
            self.assertEqual((end["status"], len(end["worker_duty"])), ("interrupted", 2))
            time.sleep(0.5)
            self.assertEqual(self.leftovers(d), [])  # no process still carries this run's schedule path in its command line

    def test_a_worker_dies_on_sigterm_even_when_the_parent_installed_a_flag_only_handler(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "worker_sigterm.py"
            script.write_text(
                "import importlib.util, os, signal, sys, time\n"
                "spec = importlib.util.spec_from_file_location('stim', sys.argv[1])\n"
                "m = importlib.util.module_from_spec(spec); sys.modules['stim'] = m; spec.loader.exec_module(m)\n"
                "signal.signal(signal.SIGTERM, lambda signum, frame: None)\n"  # as in main(): inherited by the forked workers
                "pool = m.ProcessPool(2)\n"
                "pid = pool._procs[0].pid\n"
                "time.sleep(0.5)\n"
                "os.kill(pid, signal.SIGTERM)\n"
                "pool._procs[0].join(3)\n"
                "print('worker alive after SIGTERM:', pool._procs[0].is_alive())\n"
                "pool.stop()\n", encoding="utf-8")
            proc = subprocess.Popen([sys.executable, str(script), str(SCRIPT)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, start_new_session=True)
            try:
                out, err = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                self.fail("hung")
            self.assertEqual(proc.returncode, 0, err)
            self.assertIn("worker alive after SIGTERM: False", out)

    def test_a_real_sighup_ends_the_run_like_sigterm(self):
        with tempfile.TemporaryDirectory() as d:
            sched = self.schedule_file(d, [{"name": "long", "kind": "idle", "level": 0, "duration_s": 120}])
            events = self.run_and_signal(sched, Path(d) / "out.jsonl", [], signal.SIGHUP, '"phase_start"')  # a dropped SSH session
            self.assertEqual(events[-1]["reason"], "SIGHUP")

    def test_a_pool_whose_creation_fails_halfway_stops_the_workers_it_started_and_does_not_hang_the_exit(self):
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "pool_failure.py"
            script.write_text(
                "import importlib.util, multiprocessing.context as mc, signal, sys\n"
                "# as in main(): a handler that only sets a flag, which forked workers inherit\n"
                "for sig in (signal.SIGINT, signal.SIGTERM):\n"
                "    signal.signal(sig, lambda signum, frame: None)\n"
                "spec = importlib.util.spec_from_file_location('stim', sys.argv[1])\n"
                "m = importlib.util.module_from_spec(spec); sys.modules['stim'] = m; spec.loader.exec_module(m)\n"
                "orig, calls = mc.ForkProcess.start, {'n': 0}\n"
                "def start(self):\n"
                "    calls['n'] += 1\n"
                "    if calls['n'] == 3:\n"
                "        raise OSError('simulated fork failure')\n"
                "    return orig(self)\n"
                "mc.ForkProcess.start = start\n"
                "try:\n"
                "    m.ProcessPool(4)\n"
                "except OSError as exc:\n"
                "    import multiprocessing\n"
                "    print('raised', exc)\n"
                "    print('alive', len(multiprocessing.active_children()))\n", encoding="utf-8")
            proc = subprocess.Popen([sys.executable, str(script), str(SCRIPT)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, start_new_session=True)
            try:
                out, err = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                self.fail("the process hung at exit: the started workers were left alive")
            self.assertEqual(proc.returncode, 0, err)
            self.assertIn("raised simulated fork failure", out)
            self.assertIn("alive 0", out)  # the workers that did start were stopped before the exception left the constructor
            time.sleep(0.3)
            self.assertEqual(self.leftovers(str(script)), [])


if __name__ == "__main__":
    unittest.main()
