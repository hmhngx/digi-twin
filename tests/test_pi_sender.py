"""pi/pi_sender.py: sampling from the real captured Pi files, omit-on-failure,
message building, probes, scheduling, and a full loop against the real
receiver over loopback."""
from __future__ import annotations

import contextlib
import http.client
import importlib.util
import io
import json
import socket
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import fixture_text
from tests.test_pi_receiver_core import ListLog, Recorder, make_clock
from twin import pi_protocol as pp
from twin import pi_receiver as pr

SENDER_PATH = Path(__file__).resolve().parents[1] / "pi" / "pi_sender.py"
_spec = importlib.util.spec_from_file_location("pi_sender_under_test", SENDER_PATH)
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)

TOKEN = "sender-test-token-0123456789abcdef"


class FakeFiles:
    """Stands in for the Pi's /proc and /sys files; a missing or None entry raises OSError."""

    def __init__(self):
        self.files = {
            ps.PROC_STAT: fixture_text("proc_stat_1.txt"),
            ps.PROC_MEMINFO: fixture_text("meminfo.txt"),
            ps.THERMAL_TYPE: fixture_text("thermal_type.txt"),
            ps.THERMAL_TEMP: fixture_text("thermal_temp.txt"),
            ps.CPUFREQ: "600000\n",
        }

    def __call__(self, path):
        value = self.files.get(path)
        if value is None:
            raise FileNotFoundError(path)
        if isinstance(value, Exception):
            raise value
        return value


def stepping_clock(start=1_000_000, step=1):
    state = {"now": start - step}

    def clock():
        state["now"] += step
        return state["now"]

    return clock


def stepping_mono(step_s=3.0, start=1000.0):
    """A fake time.monotonic(): advances step_s seconds per call (default: one sample interval)."""
    state = {"now": start - step_s}

    def mono():
        state["now"] += step_s
        return state["now"]

    return mono


def make_sampler(files=None, clock=None, warns=None, monotonic=None):
    files = files or FakeFiles()
    warns = [] if warns is None else warns
    return ps.Sampler(read=files, clock_ms=clock or stepping_clock(), warn=warns.append,
                      monotonic=monotonic or stepping_mono()), files, warns


class SamplerTests(unittest.TestCase):
    def test_first_sample_has_no_cpu_but_everything_else_from_the_fixtures(self):
        sampler, _, _ = make_sampler()
        m = sampler.sample()
        self.assertNotIn("cpu_utilization", m)  # lone first sample: no baseline -> unavailable
        self.assertEqual(m["memory_utilization"]["value"], 31.12)
        self.assertEqual(m["cpu_temperature_c"]["value"], 40.78)
        self.assertEqual(m["cpu_frequency_khz"]["value"], 600000)

    def test_second_sample_computes_cpu_from_the_two_fixture_reads(self):
        sampler, files, _ = make_sampler(clock=stepping_clock(2_000_000, step=10))
        sampler.sample()
        files.files[ps.PROC_STAT] = fixture_text("proc_stat_2.txt")
        m = sampler.sample()
        self.assertEqual(m["cpu_utilization"]["value"], 1.24)
        self.assertEqual(m["cpu_utilization"]["window_ms"], 3000)  # fake monotonic clock: 3 s between the reads

    def test_every_measurement_has_its_own_measured_at_stamp(self):
        sampler, _, _ = make_sampler(clock=stepping_clock(5000, step=7))
        m = sampler.sample()
        stamps = [m[name]["measured_at_ms"] for name in ("memory_utilization", "cpu_temperature_c", "cpu_frequency_khz")]
        self.assertEqual(len(set(stamps)), 3)
        self.assertEqual(stamps, sorted(stamps))

    def test_a_failed_read_omits_that_feature_and_never_substitutes_zero(self):
        cases = {
            "memory_utilization": ps.PROC_MEMINFO,
            "cpu_temperature_c": ps.THERMAL_TEMP,
            "cpu_frequency_khz": ps.CPUFREQ,
        }
        for feature, path in cases.items():
            with self.subTest(feature):
                sampler, files, warns = make_sampler()
                del files.files[path]
                m = sampler.sample()
                self.assertNotIn(feature, m)
                self.assertEqual(len(m), 2)  # memory/temp/freq minus the broken one (cpu has no baseline yet)
                self.assertTrue(warns)

    def test_unparsable_content_is_omitted_not_zeroed(self):
        sampler, files, _ = make_sampler()
        files.files[ps.PROC_MEMINFO] = "garbage\n"
        files.files[ps.CPUFREQ] = "n/a\n"
        files.files[ps.THERMAL_TEMP] = "hot\n"
        self.assertEqual(sampler.sample(), {})

    def test_wrong_thermal_zone_type_omits_temperature(self):
        sampler, files, _ = make_sampler()
        files.files[ps.THERMAL_TYPE] = "acpitz\n"
        self.assertNotIn("cpu_temperature_c", sampler.sample())

    def test_out_of_range_reading_is_omitted(self):
        sampler, files, _ = make_sampler()
        files.files[ps.THERMAL_TEMP] = "200000\n"  # 200 C: not credible, never sent
        self.assertNotIn("cpu_temperature_c", sampler.sample())

    def test_unreadable_proc_stat_discards_the_baseline(self):
        sampler, files, _ = make_sampler()
        good1, good2 = fixture_text("proc_stat_1.txt"), fixture_text("proc_stat_2.txt")
        sampler.sample()                                   # baseline
        files.files[ps.PROC_STAT] = OSError("read failed")
        self.assertNotIn("cpu_utilization", sampler.sample())   # failure -> omitted, baseline dropped
        files.files[ps.PROC_STAT] = good2
        self.assertNotIn("cpu_utilization", sampler.sample())   # lone first sample again
        files.files[ps.PROC_STAT] = good2.replace("12754", "12760").replace("284321", "284400")
        self.assertIn("cpu_utilization", sampler.sample())      # now there is a baseline

    def test_counters_going_backwards_omit_cpu_then_recover_on_the_next_cycle(self):
        sampler, files, _ = make_sampler()
        sampler.sample()
        files.files[ps.PROC_STAT] = fixture_text("proc_stat_2.txt")
        self.assertIn("cpu_utilization", sampler.sample())
        files.files[ps.PROC_STAT] = fixture_text("proc_stat_1.txt")  # counters went backwards
        self.assertNotIn("cpu_utilization", sampler.sample())
        files.files[ps.PROC_STAT] = fixture_text("proc_stat_2.txt")  # forward again from the new baseline
        self.assertEqual(sampler.sample()["cpu_utilization"]["value"], 1.24)

    def test_warning_is_emitted_once_per_failure_episode(self):
        sampler, files, warns = make_sampler()
        del files.files[ps.CPUFREQ]
        for _ in range(5):
            sampler.sample()
        self.assertEqual(len([w for w in warns if "cpu_frequency_khz" in w]), 1)
        files.files[ps.CPUFREQ] = "600000\n"
        sampler.sample()
        del files.files[ps.CPUFREQ]
        sampler.sample()
        self.assertEqual(len([w for w in warns if "cpu_frequency_khz" in w]), 2)

    def test_every_emitted_measurement_passes_the_receivers_validator(self):
        sampler, files, _ = make_sampler()
        sampler.sample()
        files.files[ps.PROC_STAT] = fixture_text("proc_stat_2.txt")
        m = sampler.sample()
        msg = {"v": 1, "session_id": "abcdefgh", "seq": 0, "sent_at_ms": 1, "measurements": m}
        parsed = pp.validate_telemetry(msg)
        self.assertEqual(parsed.rejected_features, [])
        self.assertEqual(set(parsed.measurements), set(pp.FEATURES))


class SamplerRobustnessTests(unittest.TestCase):
    def test_window_comes_from_the_monotonic_clock_not_from_wall_clock_stamps(self):
        # An NTP step on a Pi that boots without a hardware clock must not corrupt the window.
        walls = iter([5_000_000] * 4 + [4_000_000] * 4 + [9_000_000] * 4)  # then 1000 s back, then 5000 s forward
        sampler, files, _ = make_sampler(clock=lambda: next(walls))
        sampler.sample()
        files.files[ps.PROC_STAT] = fixture_text("proc_stat_2.txt")
        back = sampler.sample()["cpu_utilization"]
        files.files[ps.PROC_STAT] = fixture_text("proc_stat_2.txt").replace("12754", "12760").replace("284321", "284400")
        forward = sampler.sample()["cpu_utilization"]
        self.assertEqual((back["window_ms"], forward["window_ms"]), (3000, 3000))
        self.assertEqual((back["measured_at_ms"], forward["measured_at_ms"]), (4_000_000, 9_000_000))

    def test_cpu_is_omitted_not_clamped_when_the_window_is_under_a_millisecond(self):
        readings = iter([10.0, 10.0004])  # 0.4 ms apart
        sampler, files, _ = make_sampler(monotonic=lambda: next(readings))
        sampler.sample()
        files.files[ps.PROC_STAT] = fixture_text("proc_stat_2.txt")
        self.assertNotIn("cpu_utilization", sampler.sample())

    def test_undecodable_files_are_omitted_not_fatal(self):
        bad = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        for path, feature in ((ps.PROC_STAT, "cpu_utilization"), (ps.PROC_MEMINFO, "memory_utilization"),
                              (ps.THERMAL_TEMP, "cpu_temperature_c"), (ps.CPUFREQ, "cpu_frequency_khz")):
            with self.subTest(path):
                sampler, files, warns = make_sampler()
                files.files[path] = bad
                self.assertNotIn(feature, sampler.sample())  # and it did not raise
                self.assertTrue(warns)


class NtpProbeTests(unittest.TestCase):
    # Adapted 2026-10-13: the probe no longer parses `timedatectl` text; its reader returns True / False / None.
    def test_reports_flag_from_the_reader_and_caches_for_sixty_seconds(self):
        calls = []
        now = {"t": 100.0}

        def reader():
            calls.append(1)
            return True

        probe = ps.NtpProbe(reader=reader, monotonic=lambda: now["t"])
        self.assertIs(probe.get(), True)
        now["t"] = 159.0
        self.assertIs(probe.get(), True)
        self.assertEqual(len(calls), 1)
        now["t"] = 161.0
        probe.get()
        self.assertEqual(len(calls), 2)

    def test_unknown_when_the_reader_fails_or_says_unknown(self):
        def boom():
            raise OSError("no adjtimex")

        self.assertIsNone(ps.NtpProbe(reader=boom, monotonic=lambda: 0.0).get())
        self.assertIsNone(ps.NtpProbe(reader=lambda: None, monotonic=lambda: 0.0).get())

    def test_reports_not_synchronized(self):
        probe = ps.NtpProbe(reader=lambda: False, monotonic=lambda: 0.0)
        self.assertIs(probe.get(), False)

    def test_the_default_reader_is_the_kernel_clock_state_not_a_subprocess(self):
        self.assertIs(ps.NtpProbe()._reader, ps.read_kernel_sync)


class FakeLink:
    """Records posts; `script` is a list of per-call outcomes: an Exception to raise,
    or (status, reply) to return. Falls back to a plausible 200."""

    def __init__(self, script=None, clock=None):
        self.posts = []
        self.script = list(script or [])
        self.clock = clock

    def post(self, path, obj):
        self.posts.append((path, json.loads(json.dumps(obj))))
        if self.script:
            outcome = self.script.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return 200, {"v": 1, "status": "applied", "ditto": "ok", "received_at_ms": 1, "rejected_features": []}

    def probe(self, clock_ms):
        """Same contract as HttpLink.probe: (status, reply, t1, t4) with t1/t4 from the Pi clock."""
        t1 = clock_ms()
        self.posts.append((pp.PROBE_PATH, {"v": 1, "t1": t1}))
        if self.script:
            outcome = self.script.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            status, reply = outcome
        else:
            t = self.clock() if self.clock else t1 + 3
            status, reply = 200, {"v": 1, "t1": t1, "t2": t, "t3": t}
        return status, reply, t1, clock_ms()


def make_sender(link, files=None, count=None, interval=3.0, burst=0, probe_every=60.0, ntp=None,
                monotonic=None, sleeps=None, warns=None, clock=None):
    clock = clock or stepping_clock(1_000_000, step=1)  # one Pi clock for sampler AND sender
    sampler, files, _ = make_sampler(files, clock=clock)
    sleeps = [] if sleeps is None else sleeps
    warns = [] if warns is None else warns
    mono = monotonic or (lambda: 0.0)
    sender = ps.Sender(
        link=link,
        sampler=sampler,
        ntp=ntp or ps.NtpProbe(reader=lambda: True, monotonic=mono),
        clock_ms=clock,
        sleep=sleeps.append,
        monotonic=mono,
        session_id="0123456789abcdef",
        interval=interval,
        probe_burst=burst,
        probe_every=probe_every,
        warn=warns.append,
        say=lambda line: None,
    )
    return sender, sleeps, warns


def telemetry_posts(link):
    return [obj for path, obj in link.posts if path == pp.TELEMETRY_PATH]


class CountingStatFiles(FakeFiles):
    """FakeFiles whose /proc/stat advances on every read, so CPU is present from the second cycle on."""

    def __init__(self):
        super().__init__()
        self.stat_reads = 0

    def __call__(self, path):
        if path == ps.PROC_STAT:
            self.stat_reads += 1
            n = self.stat_reads
            return "cpu  %d 0 %d %d 0 0 0 0 0 0\n" % (100 * n, 50 * n, 1000 * n)
        return super().__call__(path)


class RecordingLink(FakeLink):
    """FakeLink that also notes each request in a shared event list; `on_post` lets a request take Pi time."""

    def __init__(self, events, on_post=None):
        super().__init__()
        self.events = events
        self.on_post = on_post

    def post(self, path, obj):
        self.events.append("post")
        if self.on_post is not None:
            self.on_post()
        return super().post(path, obj)

    def probe(self, clock_ms):
        self.events.append("probe")
        return super().probe(clock_ms)


class NtpRig:
    """A Pi whose clocks the test controls. The wall clock (epoch ms) and the monotonic clock (s) move
    together: every wall-clock read costs 1 ms; sleep() and a refresh advance both. `kernel_reader`
    stands in for the refresh (the adjtimex read): it deliberately costs `ntp_cost_ms` of Pi time, far more
    than a system call, so that any misplaced refresh shows; each call is recorded as a (start, end) window
    on the Pi wall clock. `answers` is what successive refreshes report: "yes" (True), "no" (False), or
    None (unavailable). `timedatectl` stands in for the start/end cross-check reference, costs the same
    and is counted separately."""

    def __init__(self, answers=("yes",), ntp_cost_ms=330, reference_answer=True):
        self.wall = 1_000_000
        self.mono = 1000.0
        self.answers = list(answers)
        self.ntp_cost_ms = ntp_cost_ms
        self.reference_answer = reference_answer
        self.events = []          # "ntp" / "tdctl" / "sample" / "post" / "probe", in the order they happened
        self.cycle_starts = []    # monotonic time at which each sampling cycle began
        self.ntp_windows = []     # (start, end) of every refresh
        self.sleeps = []
        self.runs = 0             # refreshes (kernel reads by the probe)
        self.reference_calls = 0  # timedatectl cross-check invocations
        self.check_lines = []     # what the cross-check printed
        self.warns = []

    def clock(self):
        self.wall += 1
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.mono += seconds
        self.wall += int(round(seconds * 1000.0))

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)

    def kernel_reader(self):
        start = self.wall
        self.events.append("ntp")
        self.runs += 1
        self.advance(self.ntp_cost_ms / 1000.0)
        self.ntp_windows.append((start, self.wall))
        answer = self.answers[min(self.runs, len(self.answers)) - 1]
        return None if answer is None else answer == "yes"

    def timedatectl(self):
        self.events.append("tdctl")
        self.reference_calls += 1
        self.advance(self.ntp_cost_ms / 1000.0)
        return self.reference_answer

    def build(self, interval=25.0, burst=0, probe_every=1e9, cross_check=False, default_reader=False):
        self.link = RecordingLink(self.events, on_post=lambda: self.advance(0.005))  # a request takes 5 ms
        sampler = ps.Sampler(read=CountingStatFiles(), clock_ms=self.clock, warn=lambda m: None,
                             monotonic=self.monotonic)
        real_sample = sampler.sample

        def sample():
            self.events.append("sample")
            self.cycle_starts.append(self.mono)
            return real_sample()

        sampler.sample = sample
        reader = None if default_reader else self.kernel_reader  # None: the real default reader
        cross = None
        if cross_check:
            cross = ps.NtpCrossCheck(reference=self.timedatectl, kernel=lambda: ps.KernelClock(0, 0, 3856),
                                     log=self.check_lines.append)
        return ps.Sender(
            link=self.link, sampler=sampler, ntp=ps.NtpProbe(reader=reader, monotonic=self.monotonic),
            clock_ms=self.clock, sleep=self.sleep, monotonic=self.monotonic, session_id="0123456789abcdef",
            interval=interval, probe_burst=burst, probe_every=probe_every,
            warn=self.warns.append, say=lambda line: None, cross_check=cross,
        )


class SenderLoopTests(unittest.TestCase):
    def test_sequence_increments_per_cycle_even_when_the_send_fails(self):
        link = FakeLink(script=[ConnectionRefusedError("down"), (500, None)])
        sender, _, warns = make_sender(link)
        sender.run(count=4)
        self.assertEqual([m["seq"] for m in telemetry_posts(link)], [0, 1, 2, 3])
        self.assertEqual(sender.stats["failed"], 2)
        self.assertEqual(sender.stats["ok"], 2)
        self.assertEqual(len([w for w in warns if "send failed" in w.lower()]), 2)

    def test_every_message_is_valid_protocol_and_carries_the_session(self):
        link = FakeLink()
        sender, _, _ = make_sender(link)
        sender.run(count=3)
        for obj in telemetry_posts(link):
            parsed = pp.validate_telemetry(obj)
            self.assertEqual(parsed.session_id, "0123456789abcdef")
            self.assertEqual(parsed.rejected_features, [])
            self.assertIs(parsed.ntp_synchronized, True)

    def test_first_message_lacks_cpu_and_later_ones_have_it(self):
        files = FakeFiles()
        link = FakeLink()
        sender, _, _ = make_sender(link, files=files)
        sender.run(count=1)
        files.files[ps.PROC_STAT] = fixture_text("proc_stat_2.txt")
        sender.run(count=1)
        posts = telemetry_posts(link)
        self.assertNotIn("cpu_utilization", posts[0]["measurements"])
        self.assertEqual(posts[1]["measurements"]["cpu_utilization"]["value"], 1.24)

    def test_all_features_failing_still_sends_a_heartbeat(self):
        files = FakeFiles()
        files.files.clear()
        link = FakeLink()
        sender, _, _ = make_sender(link, files=files)
        sender.run(count=2)
        posts = telemetry_posts(link)
        self.assertEqual(len(posts), 2)
        self.assertEqual([p["measurements"] for p in posts], [{}, {}])

    def test_sent_at_is_not_before_any_measured_at(self):
        link = FakeLink()
        sender, _, _ = make_sender(link)
        sender.run(count=2)
        for obj in telemetry_posts(link):
            for m in obj["measurements"].values():
                self.assertGreaterEqual(obj["sent_at_ms"], m["measured_at_ms"])

    def test_401_stops_the_sender_instead_of_hammering_the_receiver(self):
        link = FakeLink(script=[(401, {"error": "unauthorized"})])
        sender, _, warns = make_sender(link)
        with self.assertRaises(ps.AuthRejected):
            sender.run(count=5)
        self.assertEqual(len(telemetry_posts(link)), 1)

    def test_ntp_run_time_is_not_counted_as_network_latency(self):
        # Adapted 2026-10-13 (it used to assert that sent_at is stamped after the NTP check). The check no
        # longer runs inside cycle(), so no run may fall between the sent_at stamp and the request.
        clock = stepping_clock(1_000_000, step=1)
        windows = []
        posted_at = []

        class SlowNtp:
            def get(inner):
                start = clock()
                for _ in range(800):  # a (deliberately slow) refresh takes 800 ms of Pi time
                    clock()
                windows.append((start, clock()))
                return True

        class TimedLink(FakeLink):
            def post(inner, path, obj):
                posted_at.append(clock())  # the instant the request starts, on the Pi clock
                return super().post(path, obj)

        link = TimedLink()
        sender, _, _ = make_sender(link, ntp=SlowNtp(), clock=clock)
        sender.run(count=3)
        posts = telemetry_posts(link)
        self.assertEqual(len(posts), 3)
        self.assertGreaterEqual(len(windows), 2)  # the check really ran, between the cycles as well
        for message, request in zip(posts, posted_at):
            for start, end in windows:
                self.assertFalse(start <= request and end >= message["sent_at_ms"],
                                 "a refresh %r overlaps [sent_at %d, request %d]" % ((start, end), message["sent_at_ms"], request))

    def test_http_200_with_an_unreadable_body_still_counts_as_sent(self):
        link = FakeLink(script=[(200, "not a dict"), (200, None)])
        sender, _, _ = make_sender(link)
        sender.run(count=2)
        self.assertEqual(sender.stats["ok"], 2)  # HTTP 200 is success; the body is informational


class NtpPlacementTests(unittest.TestCase):
    """Where the NTP refresh runs, whatever its implementation. In the live run of 2026-10-13 the former
    `timedatectl` refresh ran between sampling and sending, so 42 of 854 messages were sent 258-349 ms after
    their measurements were read (every other message: 1-2 ms). The rig's refresh deliberately costs 330 ms
    so that any misplacement shows. These tests use a 25 s interval so that a 60 s refresh falls due within
    five cycles: cycle starts are at t = 0, 25, 50, 75, 100 s and the refresh is due at the end of the cycle
    at t = 75 s."""

    def test_no_message_is_sent_later_than_a_few_ms_after_its_first_measurement_read(self):
        rig = NtpRig(answers=("yes", "no"))
        rig.build().run(count=5)
        posts = telemetry_posts(rig.link)
        self.assertEqual(len(posts), 5)
        self.assertEqual(rig.runs, 2)  # the startup check and exactly one refresh (NTP_REFRESH_SEC), not one per cycle
        for post in posts:
            earliest = min(m["measured_at_ms"] for m in post["measurements"].values())
            self.assertLessEqual(post["sent_at_ms"] - earliest, 10,
                                 "seq %d was sent %d ms after its first measurement" % (post["seq"], post["sent_at_ms"] - earliest))
            for start, end in rig.ntp_windows:
                self.assertFalse(start <= post["sent_at_ms"] and end >= earliest,
                                 "a refresh %r overlaps [first measurement %d, sent_at %d]" % ((start, end), earliest, post["sent_at_ms"]))

    def test_the_sampling_grid_does_not_move_when_the_refresh_runs(self):
        rig = NtpRig(answers=("yes", "no"))
        rig.build().run(count=5)
        gaps = [b - a for a, b in zip(rig.cycle_starts, rig.cycle_starts[1:])]
        self.assertEqual(len(gaps), 4)
        for gap in gaps:
            self.assertAlmostEqual(gap, 25.0, places=6)

    def test_order_is_startup_check_then_sample_and_send_with_the_refresh_after_send_and_probe(self):
        rig = NtpRig(answers=("yes", "no"))
        rig.build(probe_every=75.0).run(count=5)  # a probe falls due at the same moment as the refresh
        self.assertEqual(
            rig.events,
            ["ntp"] + ["sample", "post"] * 3 + ["sample", "post", "probe", "ntp"] + ["sample", "post"],
        )

    def test_the_first_message_carries_the_startup_check_and_a_count_of_one_runs_it_once(self):
        rig = NtpRig(answers=("no",))
        rig.build().run(count=1)
        self.assertEqual(rig.events, ["ntp", "sample", "post"])  # checked before the first sample, nothing after the send
        self.assertIs(telemetry_posts(rig.link)[0]["ntp_synchronized"], False)

    def test_a_later_refresh_updates_the_flag_from_the_next_message_on(self):
        rig = NtpRig(answers=("yes", "no"))
        rig.build().run(count=5)
        self.assertEqual([m["ntp_synchronized"] for m in telemetry_posts(rig.link)], [True, True, True, True, False])

    def test_a_refresh_due_after_the_last_cycle_still_runs_so_a_later_run_call_starts_fresh(self):
        rig = NtpRig(answers=("yes", "no"))
        sender = rig.build()
        sender.run(count=4)  # cycles at t = 0, 25, 50, 75 s: the refresh falls due at the end of the fourth
        self.assertEqual(rig.events[-1], "ntp")  # it runs before run() returns (this delays the return by its run time)
        rig.advance(100.0)  # the caller idles; nothing refreshes the flag in the meantime
        sender.run(count=1)
        self.assertEqual([m["ntp_synchronized"] for m in telemetry_posts(rig.link)], [True, True, True, True, False])

    def test_unknown_stays_unknown_and_is_retried_only_when_the_refresh_falls_due(self):
        rig = NtpRig(answers=(None, "yes"))
        rig.build().run(count=5)
        self.assertEqual([m["ntp_synchronized"] for m in telemetry_posts(rig.link)], [None, None, None, None, True])
        self.assertEqual(rig.runs, 2)

    def test_a_refresh_that_overruns_the_idle_slack_triggers_no_catch_up_burst(self):
        rig = NtpRig(answers=("yes", "no"), ntp_cost_ms=2500)  # a 1 s interval cannot absorb a 2.5 s refresh
        rig.build(interval=1.0).run(count=70)
        starts = rig.cycle_starts
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertEqual(len(starts), 70)
        self.assertEqual(rig.runs, 2)
        self.assertTrue(all(s > 0 for s in rig.sleeps))  # never a zero or negative sleep
        self.assertTrue(all(g >= 1.0 - 1e-6 for g in gaps), "two cycles closer than the interval: a catch-up burst")
        long_gaps = [g for g in gaps if g > 1.0 + 1e-6]
        self.assertEqual(len(long_gaps), 1)
        self.assertAlmostEqual(long_gaps[0], 2.505, places=6)  # 5 ms request + 2.5 s refresh, then the next cycle starts at once; the grid resumes after it


class KernelNtpCheckTests(unittest.TestCase):
    """The refresh reads the kernel clock state (adjtimex); `timedatectl` runs only as a cross-check, once
    before the first cycle (before the first CPU baseline read) and once at the end of the run, never in
    the loop. Interval 25 s and 10 cycles: t = 0 ... 225 s, more than three 60 s refresh intervals."""

    def test_a_run_makes_exactly_two_timedatectl_calls_and_none_inside_the_loop(self):
        rig = NtpRig(answers=("yes",))
        sender = rig.build(cross_check=True)
        sender.run(count=10)
        sender.final_check()
        sender.final_check()  # once only
        self.assertEqual(rig.reference_calls, 2)
        events = rig.events
        self.assertGreaterEqual(events.count("ntp"), 4)  # the startup read and at least three later refreshes
        self.assertEqual(events[:2], ["ntp", "tdctl"])  # startup: before the first sample, hence before its CPU baseline read
        self.assertLess(events.index("tdctl"), events.index("sample"))
        self.assertEqual(events[-1], "tdctl")  # end of run: after the last send, so in no further CPU window
        self.assertNotIn("tdctl", events[2:-1])
        self.assertEqual(len(rig.check_lines), 2)
        self.assertTrue(rig.check_lines[0].startswith("ntp check (start): adjtimex rc=0 status=0x0000 maxerror_us=3856 -> True; timedatectl -> True"))
        self.assertTrue(rig.check_lines[1].startswith("ntp check (end): "))

    def test_a_broken_stderr_stops_neither_the_run_nor_the_end_check(self):
        def broken(line):
            raise BrokenPipeError("stderr closed")

        rig = NtpRig(answers=("yes",))
        sender = rig.build()
        sender.cross_check = ps.NtpCrossCheck(reference=rig.timedatectl, kernel=lambda: ps.KernelClock(0, 0, 1), log=broken)
        sender.run(count=3)  # the start line is lost, the run goes on
        sender.final_check()  # and so does the end
        self.assertEqual(len(telemetry_posts(rig.link)), 3)
        self.assertEqual(rig.reference_calls, 2)

    def test_messages_carry_the_adjtimex_answer_not_the_timedatectl_answer(self):
        rig = NtpRig(answers=("yes",), reference_answer=False)
        rig.build(cross_check=True).run(count=3)
        self.assertEqual([m["ntp_synchronized"] for m in telemetry_posts(rig.link)], [True, True, True])

    def test_unavailable_adjtimex_sends_null_warns_once_and_never_falls_back_to_timedatectl(self):
        rig = NtpRig(answers=(None,))
        sender = rig.build(cross_check=True)
        sender.run(count=10)
        sender.final_check()
        self.assertEqual([m["ntp_synchronized"] for m in telemetry_posts(rig.link)], [None] * 10)
        warnings = [w for w in rig.warns if "adjtimex" in w]
        self.assertEqual(len(warnings), 1)
        self.assertIn("ntp_synchronized unavailable", warnings[0])
        self.assertEqual(rig.reference_calls, 2)  # the start and end cross-check only
        self.assertNotIn("tdctl", rig.events[2:-1])

    def test_a_flag_that_is_known_at_startup_raises_no_warning(self):
        rig = NtpRig(answers=("yes", None))
        rig.build().run(count=5)
        self.assertEqual(rig.warns, [])

    def test_message_keys_are_unchanged_and_the_flag_stays_true_false_or_null(self):
        rig = NtpRig(answers=("yes", None, "no"))
        rig.build().run(count=10)  # reads: startup yes; the refresh after cycle 3 says unknown; after cycle 6 and 9, no
        posts = telemetry_posts(rig.link)
        for m in posts:
            self.assertEqual(set(m), {"v", "session_id", "seq", "sent_at_ms", "ntp_synchronized", "measurements", "probes"})
            self.assertTrue(m["ntp_synchronized"] is None or isinstance(m["ntp_synchronized"], bool))
        self.assertEqual([m["ntp_synchronized"] for m in posts], [True] * 4 + [None] * 3 + [False] * 3)

    def test_the_default_reader_loop_never_starts_a_subprocess(self):
        rig = NtpRig()
        with mock.patch.object(ps.subprocess, "run") as run, mock.patch.object(ps.subprocess, "Popen") as popen:
            rig.build(default_reader=True).run(count=10)  # the real default reader, across several refreshes
        run.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(len(telemetry_posts(rig.link)), 10)


class MainExitTests(unittest.TestCase):
    """main() runs the end-of-run cross-check after the existing try/except. SIGTERM stays uncovered: the
    process has no handler for it, so it is killed at once and nothing after run() executes."""

    def run_main(self, run_effect=None, final_effect=None):
        with mock.patch.dict("os.environ", {"PI_SHARED_TOKEN": "good-token_123.~"}, clear=True), \
                mock.patch.object(ps, "Sender") as sender_cls, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            sender = sender_cls.return_value
            sender.stats = {"ok": 0, "failed": 0}
            sender.run.side_effect = run_effect
            sender.final_check.side_effect = final_effect
            with self.assertRaises(SystemExit) as ctx:
                ps.main(["--receiver", "http://192.0.2.10:8765", "--count", "1"])
        return sender, sender_cls, ctx.exception.code

    def test_end_check_runs_after_a_normal_finish(self):
        sender, sender_cls, code = self.run_main()
        self.assertEqual([c[0] for c in sender.method_calls], ["run", "final_check"])
        self.assertEqual(code, 0)
        self.assertIsInstance(sender_cls.call_args.kwargs["cross_check"], ps.NtpCrossCheck)

    def test_end_check_runs_after_ctrl_c(self):
        sender, _, code = self.run_main(run_effect=KeyboardInterrupt())
        self.assertEqual([c[0] for c in sender.method_calls], ["run", "final_check"])
        self.assertEqual(code, 0)

    def test_end_check_runs_after_a_rejected_token_and_the_exit_code_stays_2(self):
        sender, _, code = self.run_main(run_effect=ps.AuthRejected("rejected"))
        self.assertEqual([c[0] for c in sender.method_calls], ["run", "final_check"])
        self.assertEqual(code, 2)

    def test_a_second_ctrl_c_during_the_end_check_is_not_a_traceback(self):
        sender, _, code = self.run_main(final_effect=KeyboardInterrupt())
        self.assertEqual(code, 0)


class ProbeScheduleTests(unittest.TestCase):
    def test_startup_burst_probes_ride_on_the_first_telemetry_message(self):
        link = FakeLink(clock=stepping_clock(1_000_050, step=1))
        sender, _, _ = make_sender(link, burst=3, clock=stepping_clock(1_000_000, step=1))
        sender.run(count=2)
        paths = [p for p, _ in link.posts]
        self.assertEqual(paths[:3], [pp.PROBE_PATH] * 3)
        first, second = telemetry_posts(link)
        self.assertEqual(len(first["probes"]), 3)
        self.assertEqual(second["probes"], [])
        for probe in first["probes"]:
            self.assertTrue(probe["t4"] >= probe["t1"])

    def test_probe_tuple_roles_t1_pi_send_t4_pi_receive(self):
        clock_values = iter([1000, 1008])  # t1 then t4 from the Pi clock

        def clock():
            return next(clock_values)

        link = FakeLink(script=[(200, {"v": 1, "t1": 1000, "t2": 1104, "t3": 1104})])
        sampler, _, _ = make_sampler()
        sender = ps.Sender(link=link, sampler=sampler, ntp=ps.NtpProbe(reader=lambda: None, monotonic=lambda: 0.0),
                           clock_ms=clock, sleep=lambda s: None, monotonic=lambda: 0.0, session_id="0123456789abcdef",
                           interval=3.0, probe_burst=0, probe_every=60.0, warn=lambda m: None, say=lambda l: None)
        probe = sender.probe_once()
        self.assertEqual(probe, {"t1": 1000, "t2": 1104, "t3": 1104, "t4": 1008})
        offset, delay = pp.compute_offset_delay(**probe)
        self.assertEqual((offset, delay), (100.0, 8))

    def test_periodic_probe_after_each_interval_and_probes_are_cleared_after_every_attempt(self):
        now = {"t": 0.0}
        link = FakeLink(clock=stepping_clock(1_000_050, step=1))
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            now["t"] += seconds

        sender, _, _ = make_sender(link, probe_every=6.0, interval=3.0, monotonic=lambda: now["t"])
        sender.sleep = sleep
        sender.run(count=5)
        probe_cycles = [i for i, m in enumerate(telemetry_posts(link)) if m["probes"]]
        self.assertTrue(probe_cycles)  # at least one periodic probe made it into a message
        self.assertTrue(all(len(m["probes"]) <= pp.MAX_PROBES for m in telemetry_posts(link)))
        carried = sum(len(m["probes"]) for m in telemetry_posts(link))
        taken = len([p for p, _ in link.posts if p == pp.PROBE_PATH])
        self.assertEqual(carried + len(sender.pending), taken)  # every probe is carried once or still pending

    def test_failed_probe_is_dropped_quietly_and_bad_reply_is_discarded(self):
        link = FakeLink(script=[ConnectionRefusedError("x"), (200, {"v": 1, "t1": 999999, "t2": 5, "t3": 6}),
                                (500, None)])
        sender, _, _ = make_sender(link)
        self.assertIsNone(sender.probe_once())
        self.assertIsNone(sender.probe_once())  # reply did not echo our t1
        self.assertIsNone(sender.probe_once())

    def test_probe_401_raises_auth_rejected(self):
        link = FakeLink(script=[(401, {"error": "unauthorized"})])
        sender, _, _ = make_sender(link)
        with self.assertRaises(ps.AuthRejected):
            sender.probe_once()

    def test_pending_probes_are_capped_at_the_protocol_limit(self):
        link = FakeLink(clock=stepping_clock(1_000_050, step=1))
        sender, _, _ = make_sender(link, burst=12)
        sender.run(count=1)
        self.assertEqual(len(telemetry_posts(link)[0]["probes"]), pp.MAX_PROBES)


class SchedulingTests(unittest.TestCase):
    def test_sleeps_the_remainder_of_the_interval_and_never_negative(self):
        now = {"t": 0.0}
        sleeps = []

        class Link(FakeLink):
            def post(inner, path, obj):
                now["t"] += 1.0  # each send takes 1 s
                return super().post(path, obj)

        def sleep(seconds):
            sleeps.append(seconds)
            now["t"] += seconds

        link = Link()
        sender, _, _ = make_sender(link, interval=3.0, monotonic=lambda: now["t"])
        sender.sleep = sleep
        sender.run(count=4)
        self.assertTrue(all(s >= 0 for s in sleeps))
        self.assertAlmostEqual(sleeps[0], 2.0, places=6)

        now["t"], sleeps[:] = 0.0, []

        class Slow(FakeLink):
            def post(inner, path, obj):
                now["t"] += 5.0  # slower than the interval: no burst to catch up, no negative sleep
                return super().post(path, obj)

        sender, _, _ = make_sender(Slow(), interval=3.0, monotonic=lambda: now["t"])
        sender.sleep = sleep
        sender.run(count=3)
        self.assertTrue(all(s >= 0 for s in sleeps))


class HostileInputTests(unittest.TestCase):
    def test_a_hostile_deeply_nested_reply_is_not_a_crash(self):
        # anyone on a plain-HTTP hotspot could answer in the receiver's place
        self.assertIsNone(ps._json_or_none(b"[" * 50000))
        self.assertIsNone(ps._json_or_none(b"\xff\xfe"))
        self.assertEqual(ps._json_or_none(b'{"a": 1}'), {"a": 1})

    def test_token_must_be_printable_ascii_without_spaces(self):
        for bad in ("has space", "na" + chr(0xEF) + "ve-token", "tab" + chr(9) + "char", "", "trailing "):
            with self.subTest(bad=bad):
                with mock.patch.dict("os.environ", {"PI_SHARED_TOKEN": bad}, clear=True):
                    with self.assertRaises(SystemExit) as ctx:
                        ps.load_token()
                if bad:
                    self.assertNotIn(bad, str(ctx.exception))
        with mock.patch.dict("os.environ", {"PI_SHARED_TOKEN": "good-token_123.~"}, clear=True):
            self.assertEqual(ps.load_token(), "good-token_123.~")


class CommandLineTests(unittest.TestCase):
    def quiet(self, argv):
        with contextlib.redirect_stderr(io.StringIO()):
            return ps.parse_args(argv)

    def test_receiver_url_must_be_plain_http_with_host_and_port(self):
        for bad in ("https://10.0.0.5:8765", "10.0.0.5:8765", "http://10.0.0.5", "http://:8765", "ftp://h:1", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit):
                    self.quiet(["--receiver", bad])

    def test_defaults(self):
        args = self.quiet(["--receiver", "http://192.0.2.10:8765"])
        self.assertEqual((args.interval, args.probe_burst, args.probe_every, args.timeout, args.count),
                         (3.0, 5, 60.0, 5.0, None))  # timeout 5 s: longer than the receiver's 3 s Ditto timeout
        self.assertEqual((args.host, args.port), ("192.0.2.10", 8765))

    def test_no_token_argument_and_receiver_required(self):
        with self.assertRaises(SystemExit):
            self.quiet(["--receiver", "http://192.0.2.10:8765", "--token", "x"])
        with self.assertRaises(SystemExit):
            self.quiet([])

    def test_bad_numbers_are_refused(self):
        base = ["--receiver", "http://192.0.2.10:8765"]
        for extra in (["--interval", "0"], ["--count", "0"], ["--probe-burst", "-1"], ["--probe-every", "0"],
                      ["--timeout", "0"], ["--interval", "x"]):
            with self.subTest(extra=extra):
                with self.assertRaises(SystemExit):
                    self.quiet(base + extra)

    def test_missing_token_env_is_refused_without_echoing_anything(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                ps.load_token()
        self.assertIn("PI_SHARED_TOKEN", str(ctx.exception))


class EndToEndTests(unittest.TestCase):
    """The real sender talking to the real receiver over loopback, ending in the
    exact Ditto PATCH bodies a live run would produce."""

    def start_receiver(self, patch=None):
        self.patch = patch or Recorder()
        self.log = ListLog()
        self.lines = []
        core = pr.PiReceiver(patch_fn=self.patch, thing_id=pr.PI_THING_ID, run_log=self.log,
                             clock_ms=pp.now_ms, log=self.lines.append)
        server = pr.make_server(core, TOKEN.encode(), "127.0.0.1", 0, log=self.lines.append, request_timeout=2.0)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()

        def stop():
            server.shutdown()
            server.server_close()
            thread.join(5)

        self.addCleanup(stop)
        return server.server_address[1]

    def make_real_sender(self, port, files, token=TOKEN, burst=2, timeout=2.0):
        link = ps.HttpLink("127.0.0.1", port, token, timeout=timeout)
        sampler = ps.Sampler(read=files, clock_ms=pp.now_ms, warn=lambda m: None, monotonic=stepping_mono())
        return ps.Sender(link=link, sampler=sampler,
                         ntp=ps.NtpProbe(reader=lambda: True, monotonic=lambda: 0.0),
                         clock_ms=pp.now_ms, sleep=lambda s: None, monotonic=lambda: 0.0,
                         session_id="e2e0123456789abc", interval=3.0, probe_burst=burst, probe_every=60.0,
                         warn=lambda m: None, say=lambda l: None)

    def test_full_pipeline_produces_the_expected_ditto_bodies_and_run_log(self):
        port = self.start_receiver()
        files = FakeFiles()
        sender = self.make_real_sender(port, files)
        sender.run(count=1)
        files.files[ps.PROC_STAT] = fixture_text("proc_stat_2.txt")
        sender.run(count=1)

        self.assertEqual(len(self.patch.calls), 2)
        first, second = (c[1] for c in self.patch.calls)
        self.assertNotIn("cpu_utilization", first)                       # lone first sample
        self.assertEqual(first["memory_utilization"], {"properties": {"value": 31.12}})
        self.assertEqual(second["cpu_utilization"], {"properties": {"value": 1.24}})
        self.assertEqual(second["cpu_temperature_c"], {"properties": {"value": 40.78}})
        self.assertEqual(second["cpu_frequency_khz"], {"properties": {"value": 600000}})
        self.assertEqual(first["link"]["properties"]["session_id"], "e2e0123456789abc")
        self.assertEqual(first["link"]["properties"]["last_seq"], 0)
        self.assertEqual(second["link"]["properties"]["last_seq"], 1)  # seq keeps counting across run() calls

        samples = self.log.of_type("sample")
        self.assertEqual([s["status"] for s in samples], ["applied", "applied"])
        self.assertEqual([s["seq"] for s in samples], [0, 1])
        probes = samples[0]["probes"]
        self.assertEqual(len(probes), 2)
        for p in probes:  # sender and receiver share one clock here, so the offset is ~0
            self.assertLessEqual(abs(p["offset_ms"]), 50)
            self.assertGreaterEqual(p["delay_ms"], 0)

    def test_probe_leaves_the_tcp_connect_out_of_the_timed_window(self):
        # t1 must be stamped AFTER connect(): the handshake sits only in the forward leg, so
        # timing it would bias the offset estimate by up to delay/2 (seen as +7..+10 ms on loopback).
        port = self.start_receiver()
        link = ps.HttpLink("127.0.0.1", port, TOKEN, timeout=3.0)
        real_connect = http.client.HTTPConnection.connect

        def slow_connect(conn):
            time.sleep(0.3)
            return real_connect(conn)

        started = time.monotonic()
        with mock.patch.object(http.client.HTTPConnection, "connect", slow_connect):
            status, reply, t1, t4 = link.probe(pp.now_ms)
        self.assertGreaterEqual((time.monotonic() - started) * 1000, 250)  # time.sleep(0.3) can undershoot by a timer tick on Windows
        self.assertEqual(status, 200)
        self.assertEqual(reply["t1"], t1)
        self.assertLess(t4 - t1, 150)   # the 300 ms connect delay is not inside [t1, t4]

    def test_wrong_token_is_rejected_end_to_end(self):
        port = self.start_receiver()
        sender = self.make_real_sender(port, FakeFiles(), token="wrong-token-wrong-token-wrong")
        with self.assertRaises(ps.AuthRejected) as ctx:
            sender.run(count=1)
        self.assertEqual(self.patch.calls, [])
        self.assertNotIn("wrong-token", str(ctx.exception))
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_receiver_unreachable_is_survivable(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        # a refused loopback connect on Windows waits out the whole timeout, so keep it short
        sender = self.make_real_sender(port, FakeFiles(), burst=1, timeout=0.3)
        sender.run(count=2)
        self.assertEqual(sender.stats["failed"], 2)


if __name__ == "__main__":
    unittest.main()
