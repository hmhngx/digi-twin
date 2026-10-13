"""pi/reference_logger.py: the independent Pi-side reference logger (raw values, Pi clock stamps, JSONL).

Runs against a fake Pi (clock, files, vcgencmd). The parsing tests use the REAL Pi captures in
tests/fixtures/pi3bplus. Tests that need a real Linux skip with an explicit reason:

  * RealLinuxTests.test_a_real_run_logs_raw_values_and_ends_cleanly
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from tests.helpers import fixture_text

SCRIPT = Path(__file__).resolve().parents[1] / "pi" / "reference_logger.py"
_spec = importlib.util.spec_from_file_location("reference_logger_under_test", SCRIPT)
M = importlib.util.module_from_spec(_spec)
sys.modules["reference_logger_under_test"] = M
_spec.loader.exec_module(M)

DEFAULT_FILES = None


def default_files():
    return {
        M.PROC_STAT: fixture_text("proc_stat_1.txt"),
        M.PROC_MEMINFO: fixture_text("meminfo.txt"),
        M.CPUINFO_CUR_FREQ: "600000\n",
        M.THERMAL_TYPE: fixture_text("thermal_type.txt"),
        M.THERMAL_TEMP: fixture_text("thermal_temp.txt"),
    }


class FakeFile:
    def __init__(self):
        self.lines, self.flushes, self.closed = [], 0, False
        self._pending = ""

    def write(self, text):
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self.lines.append(line)
        return len(text)

    def flush(self):
        self.flushes += 1

    def fileno(self):
        return 99

    def close(self):
        self.closed = True

    def records(self):
        return [json.loads(line) for line in self.lines]


class FakeBox:
    """A fake Pi: a clock that moves when files are read or the code sleeps."""

    def __init__(self, files=None, read_cost_s=0.01, vcgencmd=True):
        self.t = 0.0
        self.files = default_files() if files is None else files
        self.read_cost_s = read_cost_s
        self.cpu_time = 0.0
        self.sleeps = []
        self.hooks = []
        self.vcg_calls = []
        self.vcg_outputs = {"measure_clock": "frequency(48)=600000000\n", "measure_temp": "temp=40.8'C\n"}
        self.vcg_path = "/usr/bin/vcgencmd" if vcgencmd else None
        self.fsyncs = []

    def read_text(self, path):
        self.t += self.read_cost_s
        value = self.files.get(path)
        if value is None:
            raise FileNotFoundError(2, "No such file or directory", path)
        if isinstance(value, Exception):
            raise value
        return value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds
        for hook in list(self.hooks):
            hook(self)

    def process_time(self):
        self.cpu_time += 0.002
        return self.cpu_time

    def run_vcgencmd(self, args):
        self.vcg_calls.append(list(args))
        out = self.vcg_outputs[args[0]]
        if isinstance(out, Exception):
            raise out
        return out

    def env(self):
        return M.Env(
            now_ms=lambda: 1_791_000_000_000 + int(round(self.t * 1000)),
            mono_ns=lambda: 7_000_000_000_000 + int(round(self.t * 1e9)),
            sleep=self.sleep,
            process_time=self.process_time,
            read_text=self.read_text,
            run_vcgencmd=self.run_vcgencmd,
            vcgencmd_path=self.vcg_path,
        )


def run_logger(box, count, **kw):
    out = FakeFile()
    logger = M.ReferenceLogger(out, box.env(), fsync=box.fsyncs.append, **kw)
    reason = logger.run(count=count)
    return logger, out, reason


def samples(out):
    return [r for r in out.records() if r["type"] == "sample"]


class IndependenceTests(unittest.TestCase):
    def test_the_logger_does_not_import_the_sender_or_the_shared_protocol_code(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported |= {(node.module or "") + "." + a.name for a in node.names}
        for name in imported:
            for forbidden in ("pi_sender", "pi_protocol", "twin"):
                self.assertNotIn(forbidden, name.split("."), "imports %s" % name)

    def test_no_import_is_hidden_behind_a_dynamic_call(self):
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else "")
                self.assertNotIn(name, ("__import__", "import_module", "exec_module", "spec_from_file_location"), name)

    def test_the_frequency_reference_is_a_different_file_from_the_senders(self):
        self.assertEqual(M.CPUINFO_CUR_FREQ.rsplit("/", 1)[1], "cpuinfo_cur_freq")
        self.assertNotIn("scaling_cur_freq", M.CPUINFO_CUR_FREQ)  # the sender reads scaling_cur_freq

    def test_the_docs_do_not_overclaim_vcgencmd_independence_and_mention_the_stall_risk(self):
        doc = M.__doc__
        self.assertNotIn("more independent", doc)
        self.assertIn("not verified", doc)
        self.assertIn("6 s", doc)  # a hung vcgencmd can stall one sample this long

    def test_the_doc_comment_states_that_its_own_cost_is_unmeasured(self):
        self.assertIn("unmeasured", (M.__doc__ or "").lower())


class RecordTests(unittest.TestCase):
    def test_raw_values_from_the_real_pi_captures_are_logged_unchanged(self):
        box = FakeBox()
        _, out, reason = run_logger(box, 1)
        self.assertEqual(reason, "completed")
        s = samples(out)[0]
        self.assertEqual(s["proc_stat"]["cpu"], [12752, 7, 4611, 283923, 29386, 0, 56, 0, 0, 0])  # every field, raw
        self.assertIsNone(s["proc_stat_error"])
        self.assertEqual(s["meminfo_kib"]["MemTotal"], 926764)
        self.assertEqual(s["meminfo_kib"]["MemAvailable"], 638312)
        self.assertEqual(s["meminfo_kib"]["MemFree"], 300712)
        self.assertEqual(s["meminfo_kib"]["Buffers"], 7860)
        self.assertEqual(s["meminfo_kib"]["Cached"], 377184)
        self.assertEqual(s["cpuinfo_cur_freq_khz"], 600000)
        self.assertEqual(s["thermal_zone0_type"], "cpu-thermal")
        self.assertEqual(s["thermal_zone0_temp_millic"], 40780)  # raw millidegrees: not converted

    def test_no_derived_percentages_are_logged(self):
        _, out, _ = run_logger(FakeBox(), 2)
        for s in samples(out):
            self.assertFalse([k for k in s if "percent" in k or "utilization" in k], s.keys())

    def test_all_cpu_lines_are_kept_not_just_the_aggregate(self):
        text = "cpu  10 1 2 3 4 5 6 7 8 9\ncpu0 1 1 1 1 1 1 1 1 1 1\ncpu1 2 2 2 2 2 2 2 2 2 2\nintr 99\n"
        _, out, _ = run_logger(FakeBox(files=dict(default_files(), **{M.PROC_STAT: text})), 1)
        stat = samples(out)[0]["proc_stat"]
        self.assertEqual(sorted(stat), ["cpu", "cpu0", "cpu1"])
        self.assertEqual(stat["cpu"], [10, 1, 2, 3, 4, 5, 6, 7, 8, 9])

    def test_every_record_has_pi_wall_clock_and_monotonic_stamps_and_the_loggers_own_process_time(self):
        _, out, _ = run_logger(FakeBox(), 3)
        times = []
        for s in samples(out):
            self.assertIsInstance(s["t_epoch_ms"], int)
            self.assertIsInstance(s["t_mono_ns"], int)
            self.assertGreaterEqual(s["t_done_mono_ns"], s["t_mono_ns"])  # the stamp is taken right after the /proc/stat read
            times.append(s["process_time_s"])
        self.assertEqual(times, sorted(times))
        self.assertGreater(times[-1], times[0])
        self.assertEqual([s["seq"] for s in samples(out)], [0, 1, 2])

    def test_the_stamp_is_taken_right_after_the_proc_stat_read_not_after_the_other_sources(self):
        box = FakeBox(read_cost_s=0.1)
        _, out, _ = run_logger(box, 1)
        s = samples(out)[0]
        self.assertEqual(s["t_mono_ns"] - 7_000_000_000_000, 100_000_000)  # after ONE read (0.1 s), the others come later
        self.assertEqual(s["t_done_mono_ns"] - s["t_mono_ns"], 400_000_000)  # the four later reads: meminfo, freq, thermal type, thermal temp

    def test_start_and_end_records_describe_the_run(self):
        _, out, _ = run_logger(FakeBox(), 2)
        recs = out.records()
        self.assertEqual(recs[0]["type"], "start")
        self.assertEqual((recs[0]["interval_s"], recs[0]["vcgencmd_every_s"], recs[0]["vcgencmd_available"]), (1.0, 10.0, True))
        self.assertEqual(recs[0]["sources"]["cpuinfo_cur_freq"], M.CPUINFO_CUR_FREQ)
        self.assertEqual(recs[-1]["type"], "end")
        self.assertEqual((recs[-1]["reason"], recs[-1]["samples"]), ("completed", 2))


class MissingSourceTests(unittest.TestCase):
    def test_an_unreadable_or_missing_source_gives_null_and_an_error_string_and_the_rest_is_unaffected(self):
        files = default_files()
        files[M.CPUINFO_CUR_FREQ] = PermissionError(13, "Permission denied")
        del files[M.PROC_MEMINFO]
        _, out, reason = run_logger(FakeBox(files=files), 2)
        self.assertEqual(reason, "completed")
        s = samples(out)[0]
        self.assertIsNone(s["cpuinfo_cur_freq_khz"])
        self.assertIn("PermissionError", s["cpuinfo_cur_freq_error"])
        self.assertIsNone(s["meminfo_kib"])
        self.assertIn("FileNotFoundError", s["meminfo_error"])
        self.assertEqual(s["proc_stat"]["cpu"][0], 12752)  # untouched
        self.assertEqual(s["thermal_zone0_temp_millic"], 40780)

    def test_unparsable_content_is_null_with_an_error_not_a_crash(self):
        files = default_files()
        files[M.PROC_STAT] = "garbage\n"
        files[M.PROC_MEMINFO] = "nothing useful\n"
        files[M.CPUINFO_CUR_FREQ] = "n/a\n"
        files[M.THERMAL_TEMP] = "hot\n"
        _, out, reason = run_logger(FakeBox(files=files), 1)
        s = samples(out)[0]
        self.assertEqual(reason, "completed")
        for value, error in (("proc_stat", "proc_stat_error"), ("meminfo_kib", "meminfo_error"),
                             ("cpuinfo_cur_freq_khz", "cpuinfo_cur_freq_error"), ("thermal_zone0_temp_millic", "thermal_error")):
            self.assertIsNone(s[value], value)
            self.assertTrue(s[error], error)

    def test_a_missing_proc_stat_still_produces_a_stamped_record(self):
        files = default_files()
        del files[M.PROC_STAT]
        _, out, _ = run_logger(FakeBox(files=files), 1)
        s = samples(out)[0]
        self.assertIsNone(s["proc_stat"])
        self.assertIsInstance(s["t_epoch_ms"], int)  # the record exists and is stamped even without the counters


class VcgencmdTests(unittest.TestCase):
    def test_vcgencmd_runs_at_the_first_sample_and_every_ten_seconds_and_the_count_is_logged(self):
        box = FakeBox()
        _, out, _ = run_logger(box, 25)  # samples at ~1 s steps: seq 0, 10, 20 are due
        with_vcg = [s["seq"] for s in samples(out) if s.get("vcgencmd")]
        self.assertEqual(with_vcg, [0, 10, 20])
        self.assertEqual(len(box.vcg_calls), 6)  # measure_clock arm + measure_temp, three times
        self.assertEqual(box.vcg_calls[0], ["measure_clock", "arm"])
        v = samples(out)[0]["vcgencmd"]
        self.assertEqual((v["arm_clock_hz"], v["temp_c"]), (600_000_000, 40.8))
        self.assertEqual(v["arm_clock_raw"], "frequency(48)=600000000")  # the raw text is kept
        self.assertEqual([s["vcgencmd_calls"] for s in (samples(out)[0], samples(out)[10], samples(out)[24])], [2, 4, 6])
        end = out.records()[-1]
        self.assertEqual((end["vcgencmd_calls_total"], end["vcgencmd_runs"]), (6, 3))

    def test_a_missing_vcgencmd_is_skipped_cleanly(self):
        box = FakeBox(vcgencmd=False)
        _, out, reason = run_logger(box, 12)
        self.assertEqual(reason, "completed")
        self.assertEqual(box.vcg_calls, [])
        self.assertFalse(any(s.get("vcgencmd") for s in samples(out)))
        self.assertFalse(out.records()[0]["vcgencmd_available"])
        end = out.records()[-1]
        self.assertEqual((end["vcgencmd_calls_total"], end["vcgencmd_runs"]), (0, 0))

    def test_a_failing_or_garbled_vcgencmd_is_recorded_not_fatal(self):
        box = FakeBox()
        box.vcg_outputs["measure_clock"] = OSError("vcgencmd: device busy")
        box.vcg_outputs["measure_temp"] = "something unexpected\n"
        _, out, reason = run_logger(box, 2)
        self.assertEqual(reason, "completed")
        v = samples(out)[0]["vcgencmd"]
        self.assertIsNone(v["arm_clock_hz"])
        self.assertIn("OSError", v["arm_clock_error"])
        self.assertIsNone(v["temp_c"])
        self.assertEqual(v["temp_raw"], "something unexpected")


class SchedulingTests(unittest.TestCase):
    def test_samples_stay_on_a_monotonic_grid_without_drift(self):
        _, out, _ = run_logger(FakeBox(), 8)
        stamps = [s["t_mono_ns"] for s in samples(out)]
        self.assertEqual({b - a for a, b in zip(stamps, stamps[1:])}, {1_000_000_000})

    def test_a_slow_cycle_causes_no_catch_up_burst(self):
        box = FakeBox()
        slow = {"done": False}

        def hook(b):  # the 3rd sample takes 2.5 s longer than its slot
            if len(b.sleeps) == 2 and not slow["done"]:
                slow["done"] = True
                b.t += 2.5

        box.hooks.append(hook)
        _, out, _ = run_logger(box, 8)
        stamps = [s["t_mono_ns"] for s in samples(out)]
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        self.assertTrue(all(g >= 1_000_000_000 - 1 for g in gaps), gaps)  # never two samples closer than the interval
        self.assertEqual(sum(1 for g in gaps if g > 1_000_000_000), 1)  # one long gap, then the grid resumes

    def test_a_stop_request_ends_the_run_with_an_end_record(self):
        box = FakeBox()
        holder = {}
        out = FakeFile()
        logger = M.ReferenceLogger(out, box.env(), fsync=box.fsyncs.append)
        box.hooks.append(lambda b: logger.request_stop("SIGTERM") if b.t >= 3 else None)
        self.assertEqual(logger.run(), "SIGTERM")
        end = out.records()[-1]
        self.assertEqual((end["type"], end["reason"]), ("end", "SIGTERM"))
        self.assertLessEqual(end["samples"], 5)

    def test_duration_limits_the_run(self):
        box = FakeBox()
        out = FakeFile()
        logger = M.ReferenceLogger(out, box.env(), fsync=box.fsyncs.append)
        self.assertEqual(logger.run(duration_s=5), "completed")
        self.assertTrue(4 <= len(samples(out)) <= 6)


class DurabilityTests(unittest.TestCase):
    def test_every_line_is_flushed_and_fsync_runs_every_ten_lines_and_at_the_end(self):
        box = FakeBox()
        _, out, _ = run_logger(box, 25)
        self.assertEqual(len(out.lines), 27)  # start + 25 samples + end
        self.assertEqual(out.flushes, 27)  # flushed per line
        self.assertEqual(box.fsyncs, [99, 99, 99])  # after lines 10 and 20, and once more at the end
        for line in out.lines:
            json.loads(line)  # each line is a complete JSON object


class OutputFileTests(unittest.TestCase):
    def test_open_exclusive_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ref.jsonl"
            path.write_text("keep me\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                M.open_exclusive(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "keep me\n")

    def test_main_refuses_an_existing_file_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ref.jsonl"
            path.write_text("keep me\n", encoding="utf-8")
            box = FakeBox()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = M.main(["--out", str(path), "--duration-s", "3"], env=box.env())
            self.assertEqual(code, 2)
            self.assertEqual(path.read_text(encoding="utf-8"), "keep me\n")
            self.assertIn("exists", err.getvalue())
            self.assertEqual(box.sleeps, [])

    def test_main_writes_a_jsonl_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ref.jsonl"
            box = FakeBox()
            code = M.main(["--out", str(path), "--duration-s", "3"], env=box.env())
            self.assertEqual(code, 0)
            kinds = [json.loads(line)["type"] for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(kinds[0], "start")
            self.assertEqual(kinds[-1], "end")
            self.assertIn("sample", kinds)


@unittest.skipUnless(sys.platform.startswith("linux"), "needs Linux: real /proc and /sys")
class RealLinuxTests(unittest.TestCase):
    def test_a_real_sighup_ends_the_run_with_an_end_record(self):
        import signal
        import subprocess
        import time
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ref.jsonl"
            proc = subprocess.Popen([sys.executable, str(SCRIPT), "--out", str(path)], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True, start_new_session=True)
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if path.exists() and '"type":"sample"' in path.read_text(encoding="utf-8"):
                        break
                    time.sleep(0.1)
                else:
                    self.fail("the logger never wrote a sample")
                proc.send_signal(signal.SIGHUP)  # a dropped SSH session
                proc.wait(timeout=15)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.stderr.close()
            recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual((recs[-1]["type"], recs[-1]["reason"]), ("end", "SIGHUP"))

    def test_a_real_run_logs_raw_values_and_ends_cleanly(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ref.jsonl"
            with contextlib.redirect_stderr(io.StringIO()):
                code = M.main(["--out", str(path), "--duration-s", "2.5"])
            self.assertEqual(code, 0)
            recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            first = [r for r in recs if r["type"] == "sample"][0]
            self.assertGreater(len(first["proc_stat"]["cpu"]), 7)
            self.assertGreater(first["meminfo_kib"]["MemTotal"], 0)
            self.assertEqual(recs[-1]["reason"], "completed")
            # frequency/thermal/vcgencmd may be absent on this machine: then null plus an error string, never a crash
            if first["cpuinfo_cur_freq_khz"] is None:
                self.assertTrue(first["cpuinfo_cur_freq_error"])


if __name__ == "__main__":
    unittest.main()
