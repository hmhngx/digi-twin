"""scripts/analyze_pi_run.py on a synthetic run whose true answers are known.

Synthetic truth (all hand-derived; see the comments at each constant):
  receiver clock = Pi clock + THETA            (receiver ahead by 250 ms)
  age at send    = 5 ms after the CPU stamp    (sent_at = m + 5)
  forward net    = 20 ms                       => received_at = m + 5 + 20 + 250 = m + 275
  Ditto PATCH    = 12 ms                       => patched_at  = received_at + 12
so a correct analysis must recover net = 20, ditto = 12, and for a measurement
stamped m+k: end-to-end = 37 - k, age at send = 5 - k.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "analyze_pi_run.py"
_spec = importlib.util.spec_from_file_location("analyze_pi_run_under_test", SCRIPT)
ap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ap)

P0 = 500_000_000_000
THETA, FWD, AGE, DITTO_MS = 250, 20, 5, 12
FEATURE_ORDER = ("cpu_utilization", "memory_utilization", "cpu_temperature_c", "cpu_frequency_khz")


def make_probe(t1, theta, fwd, back, proc=1):
    t2 = t1 + theta + fwd
    t3 = t2 + proc
    t4 = t3 - theta + back
    return {"t1": t1, "t2": t2, "t3": t3, "t4": t4}


GOOD_PROBE = make_probe(P0 - 1000, THETA, 20, 20)    # offset 250.0, delay 40
BAD_PROBE = make_probe(P0 + 10000, THETA, 100, 20)   # asymmetric: offset 290.0, delay 120
NEGATIVE_PROBE = {"t1": P0 + 20000, "t2": P0 + 20100, "t3": P0 + 20600, "t4": P0 + 20010}  # delay -490


def base_ms(seq):
    return P0 + 3000 * seq + (12000 if seq >= 8 else 0)  # silence of ~15 s between seq 7 and 8


def sample(seq, status="applied", ditto="ok", skip=(), missed=0, probes=(), ntp=True, received_extra=0):
    m = base_ms(seq)
    measurements = {}
    for k, name in enumerate(FEATURE_ORDER):
        if name in skip:
            continue
        entry = {"value": 1.0 + k, "measured_at_ms": m + k}
        if name == "cpu_utilization":
            entry["window_ms"] = 3000
        measurements[name] = entry
    received = m + AGE + FWD + THETA + received_extra
    return {
        "type": "sample", "session_id": "synthetic0000001", "seq": seq, "status": status,
        "missed": missed if status == "applied" else None,
        "sent_at_ms": m + AGE, "received_at_ms": received,
        "patched_at_ms": received + DITTO_MS if ditto == "ok" else None,
        "ditto": ditto, "ntp_synchronized": ntp, "measurements": measurements,
        "rejected_features": [], "probes": [dict(p) for p in probes],
    }


def synthetic_records():
    return [
        {"type": "start", "started_at_ms": P0, "protocol": 1, "thing_id": "x:pi_0", "dry_run": False},
        sample(0, skip=("cpu_utilization",), probes=[GOOD_PROBE]),
        sample(1, ntp=False),
        sample(2, ditto="failed", ntp=None),
        sample(3, skip=("cpu_temperature_c",)),
        sample(3, status="duplicate", ditto="skipped", received_extra=50),
        sample(4),
        sample(6, missed=1, probes=[BAD_PROBE, NEGATIVE_PROBE, GOOD_PROBE]),  # GOOD_PROBE re-sent: must be de-duplicated
        sample(5, status="stale", ditto="skipped", received_extra=3000 * 1 + 10),
        sample(7),
        sample(8),
        sample(9),
        sample(10),
    ]


def write_jsonl(path, records, trailing_garbage=None):
    text = "".join(json.dumps(r) + "\n" for r in records)
    if trailing_garbage:
        text += trailing_garbage
    Path(path).write_text(text, encoding="utf-8")


class StatsTests(unittest.TestCase):
    def test_nearest_rank_percentiles(self):
        values = list(range(1, 11))
        self.assertEqual(ap.percentile(values, 50), 5)
        self.assertEqual(ap.percentile(values, 95), 10)
        self.assertEqual(ap.percentile(values, 99), 10)
        self.assertEqual(ap.percentile(values, 10), 1)
        self.assertEqual(ap.percentile([7], 50), 7)
        self.assertIsNone(ap.percentile([], 50))

    def test_summarize(self):
        self.assertEqual(ap.summarize([]), {"n": 0})
        s = ap.summarize([4, 2, 6, 8])
        self.assertEqual((s["n"], s["min"], s["max"], s["mean"], s["p50"]), (4, 2, 8, 5.0, 4))
        self.assertAlmostEqual(s["stdev"], 2.581989, places=3)  # sample stdev: sqrt(20 / 3)
        self.assertEqual(ap.summarize([7])["stdev"], 0.0)


class ProbeSelectionTests(unittest.TestCase):
    def setUp(self):
        records = synthetic_records()
        self.samples = [r for r in records if r["type"] == "sample"]

    def test_collect_probes_dedups_recomputes_and_excludes_negative_delay(self):
        valid, negative = ap.collect_probes(self.samples)
        self.assertEqual(negative, 1)
        self.assertEqual(len(valid), 2)
        by_delay = sorted(valid, key=lambda p: p["delay_ms"])
        self.assertEqual((by_delay[0]["offset_ms"], by_delay[0]["delay_ms"]), (250.0, 40))
        self.assertEqual((by_delay[1]["offset_ms"], by_delay[1]["delay_ms"]), (290.0, 120))

    def test_probe_values_are_recomputed_not_trusted_from_the_log(self):
        lying = dict(GOOD_PROBE, offset_ms=9999.0, delay_ms=1)
        valid, _ = ap.collect_probes([{"probes": [lying]}])
        self.assertEqual((valid[0]["offset_ms"], valid[0]["delay_ms"]), (250.0, 40))

    def test_min_delay_probe_wins_inside_the_window(self):
        valid, _ = ap.collect_probes(self.samples)
        offset, delay, fallback = ap.pick_offset(valid, at_ms=P0 + 9275, window_ms=300_000)
        self.assertEqual((offset, delay, fallback), (250.0, 40, False))  # not the nearer-in-time bad probe

    def test_narrow_window_only_sees_nearby_probes(self):
        valid, _ = ap.collect_probes(self.samples)
        self.assertEqual(ap.pick_offset(valid, at_ms=P0 + 9275, window_ms=5000), (290.0, 120, False))

    def test_outside_every_window_falls_back_to_the_best_probe_and_says_so(self):
        valid, _ = ap.collect_probes(self.samples)
        offset, delay, fallback = ap.pick_offset(valid, at_ms=P0 + 50_000_000, window_ms=1000)
        self.assertEqual((offset, delay, fallback), (250.0, 40, True))

    def test_no_probes_means_no_offset(self):
        self.assertIsNone(ap.pick_offset([], at_ms=1, window_ms=1000))

    def test_drift_needs_three_minutes_of_buckets_and_recovers_a_known_slope(self):
        base = 60_000 * 1_000_000
        probes = []
        for k in range(5):  # offset rises 1 ms per 60 s = 1/60 ms/s = 16.667 ppm
            p = make_probe(base + 60_000 * k - 1000, 100 + k, 5, 5, proc=0)
            probes.append(p)
        valid, _ = ap.collect_probes([{"probes": probes}])
        self.assertAlmostEqual(ap.estimate_drift_ppm(valid), 1000.0 / 60.0, places=2)
        self.assertIsNone(ap.estimate_drift_ppm(valid[:2]))
        self.assertIsNone(ap.estimate_drift_ppm([]))


class AnalyzeSyntheticRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = ap.analyze(synthetic_records())

    def test_run_and_sequence_health(self):
        r = self.result
        self.assertEqual(r["run"]["samples_logged"], 12)
        self.assertEqual(r["run"]["sessions"], 1)
        seq = r["sequence"]
        self.assertEqual((seq["applied"], seq["duplicate"], seq["stale"], seq["stale_session"]), (10, 1, 1, 0))
        self.assertEqual(seq["missed_total"], 1)

    def test_ditto_outcomes(self):
        d = self.result["ditto"]
        self.assertEqual((d["ok"], d["failed"], d["skipped"], d["disabled"]), (9, 1, 2, 0))

    def test_clock_section(self):
        c = self.result["clock"]
        self.assertEqual(c["probes_valid"], 2)
        self.assertEqual(c["probes_negative_delay"], 1)
        self.assertEqual((c["best_probe"]["offset_ms"], c["best_probe"]["delay_ms"]), (250.0, 40))
        self.assertEqual((c["delay_ms"]["min"], c["delay_ms"]["max"], c["delay_ms"]["p50"]), (40, 120, 40))
        self.assertIsNone(c["drift_ppm"])
        self.assertEqual(c["ntp"], {"true": 8, "false": 1, "unknown": 1})

    def test_latency_recovers_the_synthetic_truth(self):
        lat = self.result["latency"]
        self.assertEqual(lat["net_ms"]["n"], 10)
        self.assertEqual((lat["net_ms"]["min"], lat["net_ms"]["max"]), (20, 20))
        self.assertEqual(lat["ditto_ms"]["n"], 9)
        self.assertEqual((lat["ditto_ms"]["min"], lat["ditto_ms"]["max"]), (12, 12))
        by = lat["end_to_end_by_feature_ms"]
        self.assertEqual((by["cpu_utilization"]["n"], by["cpu_utilization"]["max"]), (8, 37))
        self.assertEqual((by["memory_utilization"]["n"], by["memory_utilization"]["max"]), (9, 36))
        self.assertEqual((by["cpu_temperature_c"]["n"], by["cpu_temperature_c"]["max"]), (8, 35))
        self.assertEqual((by["cpu_frequency_khz"]["n"], by["cpu_frequency_khz"]["max"]), (9, 34))
        self.assertEqual(lat["end_to_end_ms"]["n"], 34)
        age = lat["age_at_send_ms"]
        self.assertEqual((age["n"], age["min"], age["max"]), (38, 2, 5))
        self.assertEqual(lat["offset_fallback_samples"], 0)
        self.assertEqual(lat["samples_without_offset"], 0)

    def test_latency_uses_only_applied_samples(self):
        # the stale and duplicate records would have skewed net/ditto counts if included
        self.assertEqual(self.result["latency"]["net_ms"]["n"], 10)

    def test_accuracy_is_reported_as_not_measured(self):
        self.assertIn("not measured", self.result["accuracy"]["status"])

    def test_detectability_finds_the_fifteen_second_silence(self):
        det = self.result["detectability"]
        self.assertEqual(det["threshold_s"], 9.0)
        self.assertEqual(len(det["silence_episodes"]), 1)
        self.assertEqual(det["silence_episodes"][0]["gap_s"], 15.0)
        self.assertEqual(det["longest_gap_s"], 15.0)
        self.assertEqual(det["per_feature_longest_missing_run"], {
            "cpu_utilization": 1, "memory_utilization": 0, "cpu_temperature_c": 1, "cpu_frequency_khz": 0})

    def test_detectability_threshold_is_a_parameter(self):
        r = ap.analyze(synthetic_records(), silence_threshold_s=5.0)
        self.assertEqual([e["gap_s"] for e in r["detectability"]["silence_episodes"]], [6.0, 15.0])

    def test_consistency(self):
        con = self.result["consistency"]
        self.assertEqual(con["bundle_complete_fraction"], 0.8)
        skew = con["intra_message_skew_ms"]
        self.assertEqual((skew["n"], skew["min"], skew["max"], skew["p50"]), (10, 2, 3, 3))
        interval = con["cpu_interval_ms"]
        self.assertEqual((interval["n"], interval["min"], interval["p50"], interval["max"]), (8, 3000, 3000, 15000))
        self.assertEqual(con["cpu_window_ms"]["n"], 9)

    def test_missing_probes_means_latency_net_is_not_invented(self):
        records = [r for r in synthetic_records()]
        for r in records:
            if r["type"] == "sample":
                r["probes"] = []
        lat = ap.analyze(records)["latency"]
        self.assertEqual(lat["net_ms"], {"n": 0})
        self.assertEqual(lat["end_to_end_ms"], {"n": 0})
        self.assertEqual(lat["samples_without_offset"], 10)
        self.assertEqual(lat["ditto_ms"]["n"], 9)        # single-clock stages need no offset
        self.assertEqual(lat["age_at_send_ms"]["n"], 38)

    def test_empty_and_dry_run_inputs_do_not_crash(self):
        r = ap.analyze([])
        self.assertEqual(r["run"]["samples_logged"], 0)
        dry = [dict(sample(0), ditto="disabled", patched_at_ms=None)]
        r = ap.analyze(dry)
        self.assertEqual(r["ditto"]["disabled"], 1)
        self.assertEqual(r["latency"]["ditto_ms"], {"n": 0})


class FileAndCliTests(unittest.TestCase):
    def test_load_run_skips_and_counts_unparsable_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.jsonl"
            write_jsonl(path, synthetic_records(), trailing_garbage='{"type":"sample","se')
            records, bad = ap.load_run(path)
        self.assertEqual(bad, 1)
        self.assertEqual(len(records), len(synthetic_records()))

    def test_report_text_has_every_section_and_the_headline_numbers(self):
        text = ap.format_report(ap.analyze(synthetic_records()))
        for heading in ("Latency", "Accuracy", "Detectability", "Consistency", "Clock"):
            self.assertIn(heading, text)
        self.assertIn("not measured", text)
        self.assertIn("15.0", text)
        self.assertIn("hotspot", text.lower())  # the measurement-condition reminder

    def test_report_states_the_drift_sign_convention(self):
        result = ap.analyze(synthetic_records())
        result["clock"]["drift_ppm"] = 16.7
        text = ap.format_report(result)
        self.assertIn("16.7 ppm", text)
        self.assertIn("gaining", text)  # positive = the receiver's clock gains on the Pi's

    def test_main_writes_json_and_prints_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run.jsonl"
            out = Path(tmp) / "result.json"
            write_jsonl(run, synthetic_records())
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = ap.main([str(run), "--json", str(out)])
            data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(data["sequence"]["applied"], 10)
        self.assertIn("Latency", buf.getvalue())

    def test_main_options_reach_the_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run.jsonl"
            out = Path(tmp) / "result.json"
            write_jsonl(run, synthetic_records())
            with contextlib.redirect_stdout(io.StringIO()):
                ap.main([str(run), "--silence-threshold-s", "5", "--offset-window-s", "1", "--json", str(out)])
            data = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(len(data["detectability"]["silence_episodes"]), 2)
        self.assertEqual(data["detectability"]["threshold_s"], 5.0)

    def test_main_missing_or_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()):
                    ap.main([str(Path(tmp) / "nope.jsonl")])
            empty = Path(tmp) / "empty.jsonl"
            empty.write_text("", encoding="utf-8")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = ap.main([str(empty)])
        self.assertEqual(code, 1)
        self.assertIn("no samples", buf.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
