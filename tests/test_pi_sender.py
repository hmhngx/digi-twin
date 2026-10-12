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
    def test_reports_flag_from_timedatectl_and_caches_for_sixty_seconds(self):
        calls = []
        now = {"t": 100.0}

        def runner():
            calls.append(1)
            return "System clock synchronized: yes\n"

        probe = ps.NtpProbe(runner=runner, monotonic=lambda: now["t"])
        self.assertIs(probe.get(), True)
        now["t"] = 159.0
        self.assertIs(probe.get(), True)
        self.assertEqual(len(calls), 1)
        now["t"] = 161.0
        probe.get()
        self.assertEqual(len(calls), 2)

    def test_unknown_when_the_command_fails_or_is_unparsable(self):
        def boom():
            raise OSError("no timedatectl")

        self.assertIsNone(ps.NtpProbe(runner=boom, monotonic=lambda: 0.0).get())
        self.assertIsNone(ps.NtpProbe(runner=lambda: "nonsense", monotonic=lambda: 0.0).get())

    def test_reports_not_synchronized(self):
        probe = ps.NtpProbe(runner=lambda: "System clock synchronized: no\n", monotonic=lambda: 0.0)
        self.assertIs(probe.get(), False)


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
        ntp=ntp or ps.NtpProbe(runner=lambda: "System clock synchronized: yes\n", monotonic=mono),
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

    def test_sent_at_is_stamped_after_the_ntp_check_so_its_run_time_is_not_counted_as_network_latency(self):
        clock = stepping_clock(1_000_000, step=1)
        finished = {}

        class SlowNtp:
            def get(inner):
                for _ in range(800):  # `timedatectl` takes 800 ms of Pi time
                    clock()
                finished["at"] = clock()
                return True

        link = FakeLink()
        sender, _, _ = make_sender(link, ntp=SlowNtp(), clock=clock)
        sender.run(count=1)
        self.assertGreater(telemetry_posts(link)[0]["sent_at_ms"], finished["at"])

    def test_http_200_with_an_unreadable_body_still_counts_as_sent(self):
        link = FakeLink(script=[(200, "not a dict"), (200, None)])
        sender, _, _ = make_sender(link)
        sender.run(count=2)
        self.assertEqual(sender.stats["ok"], 2)  # HTTP 200 is success; the body is informational


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
        sender = ps.Sender(link=link, sampler=sampler, ntp=ps.NtpProbe(runner=lambda: "", monotonic=lambda: 0.0),
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
                         ntp=ps.NtpProbe(runner=lambda: "System clock synchronized: yes\n", monotonic=lambda: 0.0),
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
