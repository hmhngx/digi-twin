"""scripts/analyze_accuracy.py: sender-vs-reference accuracy, reference-vs-stimulus truth, step response.

Every number is checked against logs generated from one known model (tests/accuracy_fixtures.py) with errors
injected on purpose: a constant +1.0 bias, a dropped sender window, a gap in the reference. The analyzer must
report exactly those, nothing invented. The thresholds in these tests exist only to exercise PASS / FAIL.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

from tests import accuracy_fixtures as F

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "analyze_accuracy.py"
TEMPLATE = Path(__file__).resolve().parents[1] / "scripts" / "accuracy_thresholds.template.json"
_spec = importlib.util.spec_from_file_location("analyze_accuracy_under_test", SCRIPT)
M = importlib.util.module_from_spec(_spec)
sys.modules["analyze_accuracy_under_test"] = M
_spec.loader.exec_module(M)

PHASE_ROWS = ("idle0", "cpu25", "cpu75", "mem100")


def run(receiver=None, reference=None, stimulus=None, thresholds=None, exploratory=True, **kw):
    return M.analyze(F.receiver_records() if receiver is None else receiver,
                     F.reference_records() if reference is None else reference,
                     F.stimulus_records() if stimulus is None else stimulus,
                     thresholds, exploratory=exploratory, **kw)


def rows(res, quantity, which=0):
    return {r["phase"]: r for r in res["quantities"][quantity]["comparisons"][which]["phases"]}


class ReusedCodeTests(unittest.TestCase):
    def test_the_analyzers_own_loader_and_percentile_are_imported_not_copied(self):
        self.assertEqual(M.apr.__name__, "analyze_pi_run")
        self.assertIs(M.percentile, M.apr.percentile)
        self.assertEqual(M.percentile(range(1, 101), 95), 95)  # nearest-rank, as in the analyzer


class CpuAccuracyTests(unittest.TestCase):
    def test_a_constant_plus_one_bias_is_reported_as_exactly_that(self):
        res = run(receiver=F.receiver_records(cpu_bias=1.0))
        for name, row in rows(res, "cpu").items():
            self.assertEqual(row["n"], 18, name)  # 60 s phase, first 2 windows (6 s) excluded, 3 s windows
            self.assertAlmostEqual(row["bias"], 1.0, places=9, msg=name)
            self.assertAlmostEqual(row["mae"], 1.0, places=9, msg=name)
            self.assertAlmostEqual(row["p95_abs"], 1.0, places=9, msg=name)

    def test_no_bias_means_zero_error(self):
        for row in rows(run(), "cpu").values():
            self.assertAlmostEqual(row["bias"], 0.0, places=9)
            self.assertAlmostEqual(row["mae"], 0.0, places=9)

    def test_the_first_two_windows_after_a_boundary_are_excluded(self):
        wild = {21: 999.0, 22: 999.0}  # the first two windows of the cpu25 phase
        row = rows(run(receiver=F.receiver_records(cpu_bias=1.0, cpu_override=wild)), "cpu")["cpu25"]
        self.assertEqual(row["n"], 18)
        self.assertAlmostEqual(row["bias"], 1.0, places=9)  # the wild values never entered
        straddling = rows(run(receiver=F.receiver_records(cpu_override={41: 999.0})), "cpu")  # window [120,123] spans a boundary
        self.assertEqual(straddling["cpu25"]["n"], 18)

    def test_a_dropped_sender_window_lowers_n_and_leaves_the_bias_alone(self):
        row = rows(run(receiver=F.receiver_records(cpu_bias=1.0, drop_messages=[30])), "cpu")["cpu25"]
        self.assertEqual(row["n"], 17)
        self.assertAlmostEqual(row["bias"], 1.0, places=9)

    def test_p95_is_nearest_rank_and_mae_and_bias_follow_the_injected_error_distribution(self):
        override = {k: 25.0 + (k - 22) for k in range(23, 41)}  # errors +1 ... +18 over the 18 steady windows
        row = rows(run(receiver=F.receiver_records(cpu_override=override)), "cpu")["cpu25"]
        self.assertEqual(row["n"], 18)
        self.assertAlmostEqual(row["bias"], 9.5, places=9)
        self.assertAlmostEqual(row["mae"], 9.5, places=9)
        self.assertEqual(row["p95_abs"], 18.0)  # ceil(0.95 * 18) = 18th smallest

    def test_a_reference_gap_is_reported_as_the_largest_interpolation_gap_used(self):
        res = run(reference=F.reference_records(drop_seconds=range(130, 136)))
        self.assertEqual(rows(res, "cpu")["cpu75"]["max_interpolation_gap_ms"], 7000)  # records at 129 s and 136 s
        self.assertEqual(rows(res, "cpu")["cpu25"]["max_interpolation_gap_ms"], 0)  # every edge fell on a record
        self.assertAlmostEqual(rows(res, "cpu")["cpu75"]["bias"], 0.0, places=9)  # linear counters: interpolation is exact
        self.assertEqual(res["quantities"]["cpu"]["max_interpolation_gap_ms"], 7000)

    def test_a_preregistered_gap_limit_excludes_and_counts_exactly_the_affected_windows(self):
        thresholds = dict(F.FULL_THRESHOLDS, max_cpu_edge_gap_ms=2000)
        res = run(reference=F.reference_records(drop_seconds=range(130, 136)), thresholds=thresholds, exploratory=False)
        row = rows(res, "cpu")["cpu75"]
        self.assertEqual(row["n_skipped_gap"], 3)  # windows ending at 132 s, 135 s and 138 s each have an edge in the gap
        self.assertEqual(row["n"], 15)

    def test_reference_records_with_missing_counters_are_skipped_counted_and_widen_the_gap(self):
        res = run(reference=F.reference_records(null_proc_stat=[99, 100]))
        self.assertEqual(res["reference"]["proc_stat_null"], 2)
        self.assertEqual(rows(res, "cpu")["cpu25"]["max_interpolation_gap_ms"], 3000)  # records at 98 s and 101 s bracket 99 s

    def test_window_edges_between_records_are_interpolated_linearly_and_the_gap_is_one_second(self):
        res = run(receiver=F.receiver_records(cpu_bias=1.0, cpu_edge_offset_ms=500))
        for name, row in rows(res, "cpu").items():
            self.assertEqual(row["n"], 17, name)
            self.assertEqual(row["max_interpolation_gap_ms"], 1000, name)
            self.assertAlmostEqual(row["bias"], 1.0, places=9, msg=name)

    def test_edges_outside_the_reference_coverage_are_skipped_not_extrapolated(self):
        short = F.reference_records()
        short = [r for r in short if r["type"] != "sample" or r["seq"] <= 100]  # the reference stops at 100 s
        res = run(reference=short)
        later = rows(res, "cpu")["cpu75"]  # steady windows end between 129 s and 180 s: none has a reference
        self.assertEqual(later["n"], 0)
        self.assertGreater(later["n_skipped_no_reference"], 0)
        self.assertIsNone(later["bias"])


class ReferenceVersusStimulusTests(unittest.TestCase):
    def test_cpu_reference_matches_the_stimulus_truth_when_nothing_is_wrong(self):
        for name in ("cpu25", "cpu75"):
            row = {r["phase"]: r for r in run()["quantities"]["cpu"]["reference_vs_stimulus"]}[name]
            self.assertAlmostEqual(row["ref_minus_achieved"], 0.0, places=9)
            self.assertAlmostEqual(row["ref_minus_nominal"], 0.0, places=9)

    def test_a_stimulus_that_missed_its_nominal_level_is_visible(self):
        stim = F.stimulus_records(achieved={"cpu25": 24.0})  # the workers only managed 24 % although 25 % was asked
        row = {r["phase"]: r for r in run(stimulus=stim)["quantities"]["cpu"]["reference_vs_stimulus"]}["cpu25"]
        self.assertAlmostEqual(row["achieved"], 24.0)
        self.assertAlmostEqual(row["achieved_minus_nominal"], -1.0, places=9)
        self.assertAlmostEqual(row["ref_minus_achieved"], 1.0, places=9)  # the reference saw 25 against a truth of 24

    def test_memory_reference_delta_matches_the_allocation_and_the_rss_growth(self):
        row = {r["phase"]: r for r in run()["quantities"]["memory"]["reference_vs_stimulus"]}["mem100"]
        self.assertAlmostEqual(row["ref_delta_mib"], 100.0, places=6)
        self.assertAlmostEqual(row["truth_delta_mib"], 100.0, places=6)  # VmRSS at mid-phase minus VmRSS at the start
        self.assertAlmostEqual(row["ref_minus_truth_mib"], 0.0, places=6)
        self.assertEqual(row["baseline_phase"], "idle0")

    def test_interrupted_phases_are_excluded_and_listed(self):
        stim = F.stimulus_records(interrupted=["cpu75"])
        res = run(stimulus=stim)
        self.assertNotIn("cpu75", rows(res, "cpu"))
        self.assertEqual(res["stimulus"]["phases_excluded"], [{"name": "cpu75", "status": "interrupted"}])


class OtherQuantityTests(unittest.TestCase):
    def test_memory_bias_is_reported_with_the_time_distance_to_the_reference(self):
        res = run(receiver=F.receiver_records(mem_bias=1.0))
        for name, row in rows(res, "memory").items():
            self.assertEqual(row["n"], 18, name)
            self.assertAlmostEqual(row["bias"], 1.0, places=9, msg=name)
            self.assertEqual(row["max_distance_ms"], 1, name)

    def test_instant_values_use_the_nearest_reference_and_report_how_far_it_was(self):
        res = run(receiver=F.receiver_records(mem_bias=1.0, instant_offset_ms=400))
        self.assertEqual(rows(res, "memory")["idle0"]["max_distance_ms"], 400)
        self.assertEqual(rows(res, "memory")["idle0"]["median_distance_ms"], 400)

    def test_frequency_is_compared_with_two_references_and_each_is_reported_separately(self):
        res = run(receiver=F.receiver_records(freq_bias_khz=200000))
        sysfs, vcg = rows(res, "frequency", 0), rows(res, "frequency", 1)
        self.assertAlmostEqual(sysfs["idle0"]["bias"], 200000.0)
        self.assertEqual(sysfs["idle0"]["max_distance_ms"], 2)
        self.assertAlmostEqual(vcg["idle0"]["bias"], 200000.0)
        self.assertGreater(vcg["idle0"]["max_distance_ms"], 1000)  # vcgencmd runs every 10 s
        self.assertLessEqual(vcg["idle0"]["max_distance_ms"], 5002)
        names = [c["name"] for c in res["quantities"]["frequency"]["comparisons"]]
        self.assertIn("cpuinfo_cur_freq", names[0])
        self.assertIn("vcgencmd", names[1])

    def test_an_unreadable_frequency_source_gives_n_zero_for_that_comparison_only(self):
        res = run(reference=F.reference_records(null_freq=range(0, 300)), receiver=F.receiver_records(freq_bias_khz=200000))
        self.assertEqual(rows(res, "frequency", 0)["idle0"]["n"], 0)
        self.assertIsNone(rows(res, "frequency", 0)["idle0"]["bias"])
        self.assertEqual(rows(res, "frequency", 1)["idle0"]["n"], 18)
        self.assertEqual(res["reference"]["cpuinfo_cur_freq_null"], len([r for r in F.reference_records() if r["type"] == "sample"]) - 0)

    def test_temperature_against_the_same_sysfs_file_and_against_vcgencmd(self):
        res = run(receiver=F.receiver_records(temp_bias_c=0.5))
        self.assertAlmostEqual(rows(res, "temperature", 0)["idle0"]["bias"], 0.5, places=9)
        self.assertAlmostEqual(rows(res, "temperature", 1)["idle0"]["bias"], 41.818 - 40.8, places=9)

    def test_every_comparison_says_whether_its_reference_path_differs_from_the_senders(self):
        res = run()
        flags = {q: [c["same_file_as_sender"] for c in v["comparisons"]] for q, v in res["quantities"].items()}
        self.assertEqual(flags, {"cpu": [True], "memory": [True], "frequency": [False, False], "temperature": [True, False]})
        for q, v in res["quantities"].items():
            for c in v["comparisons"]:
                self.assertTrue(c["path_note"], q)
        self.assertIn("ground truth", res["quantities"]["cpu"]["truth"].lower())
        self.assertIn("none", res["quantities"]["frequency"]["truth"].lower())
        self.assertIn("none", res["quantities"]["temperature"]["truth"].lower())


class StepResponseTests(unittest.TestCase):
    def test_cpu_step_time_is_the_first_message_past_half_the_level_on_both_clocks(self):
        ramp = {21: 5.0, 22: 10.0}  # the first two windows of cpu25 are slow; the plateau (25) starts at message 23
        res = run(receiver=F.receiver_records(cpu_override=ramp))
        row = {r["phase"]: r for r in res["quantities"]["cpu"]["step_response"]}["cpu25"]
        self.assertAlmostEqual(row["threshold"], 12.5, places=9)  # halfway between the idle plateau (0) and 25
        self.assertEqual(row["pi_clock_ms"], 9000)  # message 23 ends 9 s after the phase start
        self.assertEqual(row["twin_visible_ms"], 9000 + 1 + F.NET_MS + F.ACK_MS)  # sent 1 ms later, + network + Ditto, offset removed

    def test_a_dropped_crossing_message_moves_the_step_time_to_the_next_one(self):
        ramp = {21: 5.0, 22: 10.0}
        res = run(receiver=F.receiver_records(cpu_override=ramp, drop_messages=[23]))
        row = {r["phase"]: r for r in res["quantities"]["cpu"]["step_response"]}["cpu25"]
        self.assertEqual(row["pi_clock_ms"], 12000)

    def test_a_dry_run_has_no_twin_visible_time_and_says_so(self):
        res = run(receiver=F.receiver_records(dry_run=True))
        row = {r["phase"]: r for r in res["quantities"]["cpu"]["step_response"]}["cpu25"]
        self.assertEqual(row["pi_clock_ms"], 3000)
        self.assertIsNone(row["twin_visible_ms"])
        self.assertIn("patched_at", row["twin_note"])

    def test_memory_step_time_uses_the_memory_stamp(self):
        row = {r["phase"]: r for r in run()["quantities"]["memory"]["step_response"]}["mem100"]
        self.assertAlmostEqual(row["threshold"], (20.0 + 30.24) / 2, places=9)
        # The model allocates at the phase boundary, and the memory read of the message that ends at the boundary
        # is stamped 1 ms later: that read already sees the new level, so the step is seen 1 ms after the start.
        self.assertEqual(row["pi_clock_ms"], 1)
        self.assertEqual(row["twin_visible_ms"], 1 + F.NET_MS + F.ACK_MS)  # that message was sent at its CPU stamp + 1 ms

    def test_the_stimulus_allocation_time_is_reported_next_to_a_memory_step_so_the_ramp_is_not_blamed_on_the_twin(self):
        res = run(stimulus=F.stimulus_records(alloc_wall_s=2.0))
        row = {r["phase"]: r for r in res["quantities"]["memory"]["step_response"]}["mem100"]
        self.assertEqual(row["stimulus_ramp_ms"], 2000)
        self.assertIn("allocation took 2000 ms", M.format_report(res))
        cpu_row = {r["phase"]: r for r in res["quantities"]["cpu"]["step_response"]}["cpu25"]
        self.assertIsNone(cpu_row["stimulus_ramp_ms"])
        self.assertIsNone({r["phase"]: r for r in run()["quantities"]["memory"]["step_response"]}["mem100"]["stimulus_ramp_ms"])  # an older log: unknown

    def test_temperature_and_frequency_have_no_step_response_and_the_report_says_why(self):
        res = run()
        self.assertNotIn("step_response", res["quantities"]["temperature"])
        self.assertNotIn("step_response", res["quantities"]["frequency"])
        self.assertIn("thermal inertia", M.format_report(res))


def run_phases(phases, **kw):
    """Whole analysis for a custom phase list (receiver, reference and stimulus built from the same model)."""
    return M.analyze(F.receiver_records(phases=phases, **kw.pop("receiver", {})),
                     F.reference_records(phases=phases), F.stimulus_records(phases=phases), kw.pop("thresholds", None),
                     exploratory=kw.pop("exploratory", True), **kw)


class StepRuleTests(unittest.TestCase):
    """The step threshold is the MIDPOINT between the previous plateau and this one (a deliberate reading of
    '50 % of the phase level', which only equals 50 % of the level when the previous plateau is 0)."""

    def step(self, res, phase):
        return {r["phase"]: r for r in res["quantities"]["cpu"]["step_response"]}[phase]

    def test_a_falling_step_crosses_downwards(self):
        phases = [("idle0", "idle", 0, 60), ("cpu75", "cpu", 75, 60), ("cpu25", "cpu", 25, 60)]
        # cpu25 starts at 120 s. Messages 41 and 42 (123 s, 126 s) are still high; message 43 (129 s) is the first at 25.
        row = self.step(run_phases(phases, receiver={"cpu_override": {41: 70.0, 42: 60.0}}), "cpu25")
        self.assertAlmostEqual(row["previous_level"], 75.0, places=9)
        self.assertAlmostEqual(row["threshold"], 50.0, places=9)  # halfway down from 75 to 25
        self.assertEqual(row["pi_clock_ms"], 9000)

    def test_a_missing_previous_plateau_gives_not_applicable_never_a_silent_zero(self):
        for label, phases in (("first phase", [("c", "cpu", 50, 60)]),
                              ("previous phase shorter than the exclusion", [("short", "idle", 0, 6), ("c", "cpu", 50, 60)])):
            with self.subTest(label):
                res = run_phases(phases, thresholds=F.FULL_THRESHOLDS, exploratory=False)
                row = self.step(res, "c")
                self.assertIsNone(row["threshold"])
                self.assertIsNone(row["pi_clock_ms"])
                self.assertIn("previous plateau", row["na_reason"])
                self.assertEqual(row["verdicts"]["pi_clock"], "N/A")

    def test_a_phase_with_no_step_gives_not_applicable_instead_of_a_response_time(self):
        phases = [("idle0", "idle", 0, 60), ("a", "cpu", 50, 60), ("b", "cpu", 50, 60)]
        row = self.step(run_phases(phases), "b")
        self.assertIsNone(row["pi_clock_ms"])
        self.assertIn("no step", row["na_reason"])

    def test_the_report_states_the_real_rule_and_when_it_equals_fifty_percent(self):
        text = M.format_report(run())
        self.assertIn("midpoint", text)
        self.assertIn("equals 50 % of the level only when the previous plateau is 0", text)
        self.assertNotIn("for a step from idle, 50 % of the level", text)


class TwinVisibleSearchTests(unittest.TestCase):
    def test_a_crossing_message_whose_ditto_update_failed_does_not_hide_the_twin_time(self):
        res = run(receiver=F.receiver_records(cpu_override={21: 5.0, 22: 10.0}, unpatched=[23]))
        row = {r["phase"]: r for r in res["quantities"]["cpu"]["step_response"]}["cpu25"]
        self.assertEqual(row["pi_clock_ms"], 9000)  # message 23 crossed on the Pi clock
        self.assertEqual(row["twin_visible_ms"], 12000 + 1 + F.NET_MS + F.ACK_MS)  # message 24 is the first Ditto saw
        self.assertIn("later message", row["twin_note"])

    def test_in_a_live_run_with_no_patched_message_the_verdict_is_fail_not_not_applicable(self):
        res = run(receiver=F.receiver_records(cpu_override={21: 5.0, 22: 10.0}, unpatched=range(1, 81)),
                  thresholds=F.FULL_THRESHOLDS, exploratory=False)
        row = {r["phase"]: r for r in res["quantities"]["cpu"]["step_response"]}["cpu25"]
        self.assertIsNone(row["twin_visible_ms"])
        self.assertEqual(row["verdicts"]["twin_visible"], "FAIL")

    def test_a_dry_run_stays_not_applicable(self):
        res = run(receiver=F.receiver_records(dry_run=True), thresholds=F.FULL_THRESHOLDS, exploratory=False)
        row = {r["phase"]: r for r in res["quantities"]["cpu"]["step_response"]}["cpu25"]
        self.assertEqual(row["verdicts"]["twin_visible"], "N/A")

    def test_the_report_says_patched_at_is_the_acknowledgement_not_downstream_visibility(self):
        self.assertIn("patched_at", M.format_report(run()).split("== What this does not show ==")[1])


class StatisticsEdgeTests(unittest.TestCase):
    def test_errors_of_both_signs_cancel_in_the_bias_but_not_in_the_absolute_statistics(self):
        override = {k: 25.0 + (2.0 if k % 2 else -2.0) for k in range(23, 41)}
        row = rows(run(receiver=F.receiver_records(cpu_override=override)), "cpu")["cpu25"]
        self.assertAlmostEqual(row["bias"], 0.0, places=9)
        self.assertAlmostEqual(row["mae"], 2.0, places=9)
        self.assertAlmostEqual(row["p95_abs"], 2.0, places=9)

    def test_a_constant_negative_error_gives_a_positive_absolute_p95_and_mae(self):
        row = rows(run(receiver=F.receiver_records(cpu_bias=-3.0)), "cpu")["cpu25"]
        self.assertAlmostEqual(row["bias"], -3.0, places=9)
        self.assertAlmostEqual(row["mae"], 3.0, places=9)
        self.assertAlmostEqual(row["p95_abs"], 3.0, places=9)  # absolute, not signed

    def test_an_instant_value_exactly_on_a_phase_end_belongs_to_the_next_phase(self):
        res = run(receiver=F.receiver_records(instant_offset_ms=0))  # memory is stamped exactly on multiples of 3 s
        self.assertEqual(rows(res, "memory")["idle0"]["n"], 18)  # 6 s ... 57 s; the read at 60 s is not in idle0

    def test_the_cpu_definition_counts_iowait_as_idle_and_leaves_guest_out_of_the_total(self):
        phases = [("p", "idle", 0, 60)]
        reference = F.reference_records(phases=phases)
        for rec in reference:
            if rec["type"] == "sample":
                k = rec["seq"]  # per second: user 30, idle 40, iowait 30, guest 99  ->  busy = 30 / 100 = 30 %
                rec["proc_stat"] = {"cpu": [30 * k, 0, 0, 40 * k, 30 * k, 0, 0, 0, 99 * k, 0]}
        receiver = F.receiver_records(phases=phases, cpu_override={k: 30.0 for k in range(1, 21)})
        row = rows(M.analyze(receiver, reference, F.stimulus_records(phases=phases), None, exploratory=True), "cpu")["p"]
        self.assertGreater(row["n"], 0)
        self.assertAlmostEqual(row["bias"], 0.0, places=9)  # ignoring iowait would give -30; counting guest, about +15

    def test_stale_and_duplicate_messages_are_ignored(self):
        row = rows(run(receiver=F.receiver_records(cpu_bias=1.0, stale_at=[30])), "cpu")["cpu25"]
        self.assertEqual(row["n"], 18)
        self.assertAlmostEqual(row["bias"], 1.0, places=9)

    def test_a_counter_that_goes_backwards_is_counted_and_skipped(self):
        reference = F.reference_records()
        for rec in reference:
            if rec["type"] == "sample" and rec["seq"] == 100:
                rec["proc_stat"] = {"cpu": [0] * 10}
        res = run(reference=reference)
        self.assertEqual(res["reference"]["cpu_counter_regressions"], 1)

    def test_memory_truth_is_the_vmrss_growth_not_the_nominal_level(self):
        stim = F.stimulus_records(vmrss_extra_kib=12 * 1024)  # the process grew 12 MiB more than it was asked to allocate
        row = {r["phase"]: r for r in run(stimulus=stim)["quantities"]["memory"]["reference_vs_stimulus"]}["mem100"]
        self.assertAlmostEqual(row["truth_delta_mib"], 112.0, places=6)
        self.assertAlmostEqual(row["ref_minus_truth_mib"], -12.0, places=6)
        self.assertAlmostEqual(row["ref_minus_nominal_mib"], 0.0, places=6)

    def test_the_cpu_comparison_with_the_stimulus_is_labelled_as_including_background_load(self):
        res = run()
        self.assertIn("background", res["quantities"]["cpu"]["truth"])
        text = M.format_report(res)
        self.assertIn("background", text.split("== What this does not show ==")[1])
        self.assertIn("includes background", text)


class TemplateTextTests(unittest.TestCase):
    def test_no_comment_in_the_template_offers_a_number_as_a_value(self):
        comments = json.loads(TEMPLATE.read_text(encoding="utf-8"))["_comments"]
        for key in ("max_instant_distance_1hz_ms", "max_instant_distance_10s_ms", "max_cpu_edge_gap_ms"):
            self.assertNotRegex(comments[key], r"\b\d{4,}\b", key)  # no figure such as 5000 offered as a value


class CpuIncreaseOverIdleTests(unittest.TestCase):
    """CPU against the stimulus: the INCREASE over the preceding idle phase (cancels a steady background, as the
    memory comparison does) and, labelled, the raw numbers (which include it)."""

    def row(self, res, phase):
        return {r["phase"]: r for r in res["quantities"]["cpu"]["reference_vs_stimulus"]}[phase]

    def background(self, **kw):
        return dict(receiver=F.receiver_records(background_pct=2.0), reference=F.reference_records(background_pct=2.0), **kw)

    def test_a_steady_background_cancels_in_the_increase_but_not_in_the_raw_numbers(self):
        res = run(**self.background())
        r = self.row(res, "cpu25")
        self.assertAlmostEqual(r["ref_minus_achieved"], 2.0, places=9)  # raw: the 2 % background is in it
        self.assertEqual(r["baseline_phase"], "idle0")
        self.assertAlmostEqual(r["ref_increase"], 25.0, places=9)
        self.assertAlmostEqual(r["achieved_increase"], 25.0, places=9)
        self.assertAlmostEqual(r["ref_increase_minus_achieved_increase"], 0.0, places=9)
        self.assertEqual(r["basis"], "increase over idle0")
        idle = self.row(res, "idle0")
        self.assertAlmostEqual(idle["ref_minus_achieved"], 2.0, places=9)  # in an idle phase the raw difference IS the background
        self.assertIsNone(idle["ref_increase"])

    def test_the_verdict_follows_the_increase_when_there_is_a_preceding_idle_phase(self):
        res = run(thresholds=dict(F.FULL_THRESHOLDS, cpu_reference_vs_stimulus_abs_max_pp=0.5), exploratory=False, **self.background())
        self.assertEqual(self.row(res, "cpu25")["verdicts"]["ref_vs_achieved"], "PASS")  # raw would be 2.0 and fail
        self.assertIsNone(self.row(res, "idle0")["verdicts"]["ref_vs_achieved"])  # an idle phase is the baseline, not a test

    def test_a_stimulus_that_underdelivered_still_fails_on_the_increase(self):
        stim = F.stimulus_records(achieved={"cpu25": 24.0})
        res = run(stimulus=stim, thresholds=dict(F.FULL_THRESHOLDS, cpu_reference_vs_stimulus_abs_max_pp=0.5), exploratory=False)
        r = self.row(res, "cpu25")
        self.assertAlmostEqual(r["ref_increase_minus_achieved_increase"], 1.0, places=9)
        self.assertEqual(r["verdicts"]["ref_vs_achieved"], "FAIL")

    def test_without_a_preceding_idle_phase_the_increase_is_not_applicable_and_the_raw_numbers_are_labelled(self):
        phases = [("c", "cpu", 50, 60)]
        res = M.analyze(F.receiver_records(phases=phases, background_pct=2.0), F.reference_records(phases=phases, background_pct=2.0),
                        F.stimulus_records(phases=phases), dict(F.FULL_THRESHOLDS, cpu_reference_vs_stimulus_abs_max_pp=0.5), exploratory=False)
        r = self.row(res, "c")
        self.assertIsNone(r["ref_increase"])
        self.assertIn("no preceding idle phase", r["na_reason"])
        self.assertIn("includes background load", r["basis"])
        self.assertTrue(r["basis"].startswith("raw"))
        self.assertAlmostEqual(r["ref_minus_achieved"], 2.0, places=9)
        self.assertEqual(r["verdicts"]["ref_vs_achieved"], "FAIL")  # judged on the raw numbers, and labelled as such
        self.assertIn("N/A", M.format_report(res))

    def test_the_baseline_is_the_nearest_preceding_idle_phase_not_the_first_one(self):
        phases = [("idle0", "idle", 0, 60), ("cpu25", "cpu", 25, 60), ("rest1", "idle", 0, 60), ("cpu75", "cpu", 75, 60)]
        stim = F.stimulus_records(phases=phases, achieved={"rest1": 2.0})  # the second idle phase had 2 % of the stimulus's own wakeups
        res = M.analyze(F.receiver_records(phases=phases), F.reference_records(phases=phases), stim, None, exploratory=True)
        r = self.row(res, "cpu75")
        self.assertEqual(r["baseline_phase"], "rest1")
        self.assertAlmostEqual(r["achieved_increase"], 73.0, places=9)  # 75 - 2, not 75 - 0

    def test_the_truth_text_and_the_does_not_show_section_describe_the_increase(self):
        res = run()
        self.assertIn("increase over the preceding idle phase", res["quantities"]["cpu"]["truth"])
        self.assertIn("background", res["quantities"]["cpu"]["truth"])
        tail = M.format_report(res).split("== What this does not show ==")[1]
        self.assertIn("increase over the preceding idle phase", tail)
        self.assertIn("background that changes between", tail)

    def test_the_report_prints_both_the_increase_and_the_raw_numbers_labelled(self):
        text = M.format_report(run(**self.background()))
        self.assertIn("increase over idle0", text)
        self.assertIn("raw (includes background load)", text)


class CpuWindowLagTests(unittest.TestCase):
    def test_the_report_explains_why_a_cpu_step_cannot_be_seen_sooner_than_half_a_window(self):
        text = M.format_report(run())
        self.assertIn("averages the last 3000 ms", text)
        self.assertIn("about 1.5 s after the step", text)
        self.assertIn("a property of the sender, not a defect", text)

    def test_the_protocol_note_says_the_same(self):
        note = (Path(__file__).resolve().parents[1] / "notes" / "PI_ACCURACY_PROTOCOL_2026-10-13.md").read_text(encoding="utf-8")
        self.assertIn("about 1.5 s after the step", note)
        self.assertIn("a property of the sender, not a defect", note)


class TwoDistanceLimitTests(unittest.TestCase):
    def test_each_limit_applies_to_its_own_references_only(self):
        tight_1hz = dict(F.FULL_THRESHOLDS, max_instant_distance_1hz_ms=1, max_instant_distance_10s_ms=6000)
        res = run(receiver=F.receiver_records(instant_offset_ms=400), thresholds=tight_1hz, exploratory=False)
        for quantity, which in (("memory", 0), ("frequency", 0), ("temperature", 0)):  # the 1 Hz references
            row = rows(res, quantity, which)["cpu25"]
            self.assertEqual(row["n"], 0, (quantity, which))
            self.assertGreater(row["n_skipped_distance"], 0)
        for quantity in ("frequency", "temperature"):  # the 10 s vcgencmd references
            self.assertGreater(rows(res, quantity, 1)["cpu25"]["n"], 0, quantity)

    def test_the_ten_second_limit_does_not_touch_the_one_hertz_references(self):
        tight_10s = dict(F.FULL_THRESHOLDS, max_instant_distance_1hz_ms=6000, max_instant_distance_10s_ms=1000)
        res = run(thresholds=tight_10s, exploratory=False)
        for quantity, which in (("memory", 0), ("frequency", 0), ("temperature", 0)):
            self.assertEqual(rows(res, quantity, which)["cpu25"]["n"], 18, (quantity, which))
        for quantity in ("frequency", "temperature"):
            row = rows(res, quantity, 1)["cpu25"]
            self.assertGreater(row["n_skipped_distance"], 0, quantity)
            self.assertLess(row["n"], 18)
            self.assertEqual(row["n"] + row["n_skipped_distance"], 18)  # every candidate is accounted for

    def test_both_limits_are_required_and_the_old_single_field_is_gone(self):
        self.assertIn("max_instant_distance_1hz_ms", M.THRESHOLD_FIELDS)
        self.assertIn("max_instant_distance_10s_ms", M.THRESHOLD_FIELDS)
        self.assertNotIn("max_instant_distance_ms", M.THRESHOLD_FIELDS)
        with self.assertRaises(M.ThresholdError) as ctx:
            run(thresholds=dict(F.FULL_THRESHOLDS, max_instant_distance_10s_ms=None), exploratory=False)
        self.assertIn("max_instant_distance_10s_ms", str(ctx.exception))
        with self.assertRaises(M.ThresholdError):
            run(thresholds=dict(F.FULL_THRESHOLDS, max_instant_distance_ms=5000), exploratory=False)  # the removed name is an unknown key

    def test_the_report_names_which_limit_each_comparison_used(self):
        text = M.format_report(run(thresholds=F.FULL_THRESHOLDS, exploratory=False))
        self.assertIn("1 Hz reference", text)
        self.assertIn("10 s reference", text)


class CounterFaultTests(unittest.TestCase):
    """Edges fall at x.5 s (cpu_edge_offset_ms=500), so a window edge lies between two 1 Hz records; each phase
    then has 17 steady windows. A fault at record 91 sits where the edge at 90.5 s lies: windows 30 and 31 touch it."""

    def go(self, reference, thresholds=None):
        return run(receiver=F.receiver_records(cpu_edge_offset_ms=500), reference=reference,
                   thresholds=thresholds, exploratory=thresholds is None)

    def assert_later_data_kept(self, res):
        for name in ("idle0", "cpu75", "mem100"):
            self.assertEqual(rows(res, "cpu")[name]["n"], 17, name)
            self.assertAlmostEqual(rows(res, "cpu")[name]["bias"], 0.0, places=9, msg=name)

    def test_a_single_high_spike_costs_nothing_but_a_wider_gap(self):
        res = self.go(F.spike_counter(F.reference_records(), 91))
        self.assertEqual((res["reference"]["cpu_counter_regressions"], res["reference"]["cpu_counter_resets"]), (1, 0))
        self.assert_later_data_kept(res)
        self.assertEqual(rows(res, "cpu")["cpu25"]["n"], 17)
        self.assertAlmostEqual(rows(res, "cpu")["cpu25"]["bias"], 0.0, places=9)
        self.assertEqual(rows(res, "cpu")["cpu25"]["max_interpolation_gap_ms"], 2000)  # records 90 and 92 now bracket the edge

    def test_the_same_spike_with_a_preregistered_gap_limit_costs_exactly_the_windows_that_touch_it(self):
        res = self.go(F.spike_counter(F.reference_records(), 91), dict(F.FULL_THRESHOLDS, max_cpu_edge_gap_ms=1500))
        row = rows(res, "cpu")["cpu25"]
        self.assertEqual((row["n"], row["n_skipped_gap"]), (15, 2))  # windows 30 and 31 share the edge at 90.5 s
        self.assertEqual(rows(res, "cpu")["cpu75"]["n"], 17)

    def test_a_reset_to_a_lower_value_that_keeps_rising_costs_exactly_the_windows_that_touch_it(self):
        res = self.go(F.reset_counter(F.reference_records(), 91))
        self.assertEqual((res["reference"]["cpu_counter_regressions"], res["reference"]["cpu_counter_resets"]), (0, 1))
        row = rows(res, "cpu")["cpu25"]
        self.assertEqual((row["n"], row["n_skipped_discontinuity"]), (15, 2))
        self.assert_later_data_kept(res)  # nothing after the reset is lost, and it is still exact
        self.assertAlmostEqual(row["bias"], 0.0, places=9)

    def test_a_window_whose_two_edges_are_records_on_either_side_of_a_reset_is_unusable(self):
        res = run(reference=F.reset_counter(F.reference_records(), 93))  # edges on whole seconds: window 31 is 90 s -> 93 s
        row = rows(res, "cpu")["cpu25"]
        self.assertEqual((row["n"], row["n_skipped_discontinuity"]), (17, 1))
        self.assertAlmostEqual(row["bias"], 0.0, places=9)

    def test_a_cpu_going_offline_is_handled_like_a_reset(self):
        res = self.go(F.offline_cpu(F.reference_records(), 91, 0.25))
        self.assertEqual((res["reference"]["cpu_counter_regressions"], res["reference"]["cpu_counter_resets"]), (0, 1))
        row = rows(res, "cpu")["cpu25"]
        self.assertEqual((row["n"], row["n_skipped_discontinuity"]), (15, 2))
        self.assert_later_data_kept(res)

    def test_a_spike_on_the_first_record_and_a_bad_last_record_are_dropped_without_losing_windows(self):
        reference = F.spike_counter(F.reference_records(), 0)
        for rec in reference:
            if rec["type"] == "sample" and rec["seq"] == 243:  # the last record: a read of zeros
                rec["proc_stat"] = {"cpu": [0] * 10}
        res = self.go(reference)
        self.assertEqual(res["reference"]["cpu_counter_regressions"], 2)
        self.assertEqual({name: row["n"] for name, row in rows(res, "cpu").items()}, {"idle0": 17, "cpu25": 17, "cpu75": 17, "mem100": 17})

    def test_the_report_counts_dropped_records_resets_and_unusable_windows(self):
        text = M.format_report(self.go(F.reset_counter(F.spike_counter(F.reference_records(), 40), 91)))
        self.assertIn("dropped as outliers: 1", text)
        self.assertIn("resets: 1", text)
        self.assertIn("discontinuity", text)


class MissingWindowTests(unittest.TestCase):
    def test_a_cpu_measurement_without_window_ms_is_counted_excluded_and_reported(self):
        res = run(receiver=F.receiver_records(cpu_bias=1.0, window_missing=[25, 26, 27, 28]))
        row = rows(res, "cpu")["cpu25"]
        self.assertEqual((row["n"], row["n_skipped_no_window"]), (14, 4))
        self.assertAlmostEqual(row["bias"], 1.0, places=9)
        self.assertEqual(res["run"]["cpu_messages_without_window"], 4)
        self.assertIn("without window_ms: 4", M.format_report(res))

    def test_a_zero_or_non_numeric_window_counts_as_missing(self):
        records = F.receiver_records(cpu_bias=1.0)
        for rec in records:
            if rec["type"] == "sample" and rec["seq"] in (25, 26, 27, 28):
                rec["measurements"]["cpu_utilization"]["window_ms"] = {25: 0, 26: "3000", 27: -5, 28: True}[rec["seq"]]
        res = run(receiver=records)
        self.assertEqual(rows(res, "cpu")["cpu25"]["n_skipped_no_window"], 4)  # zero, a string, negative, and a JSON boolean
        self.assertEqual(res["run"]["cpu_messages_without_window"], 4)

    def test_when_no_message_has_a_window_the_exclusion_zone_uses_3000_ms_and_says_so(self):
        res = run(receiver=F.receiver_records(window_missing=range(1, 81)))
        self.assertEqual(res["run"]["window_ms"], 3000)
        self.assertIn("no message carried window_ms", res["run"]["window_ms_source"])
        self.assertTrue(all(r["n"] == 0 for r in rows(res, "cpu").values()))
        self.assertIn("no message carried window_ms", M.format_report(res))

    def test_the_step_response_plateau_ignores_messages_without_a_window(self):
        res = run(receiver=F.receiver_records(window_missing=range(23, 41)))  # every steady cpu25 window
        row = {r["phase"]: r for r in res["quantities"]["cpu"]["step_response"]}["cpu25"]
        self.assertIsNone(row["level_steady"])
        self.assertIn("no steady-state value", row["na_reason"])


class ThresholdTests(unittest.TestCase):
    def test_the_template_has_every_field_null_and_a_comment_for_each(self):
        template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
        fields = {k: v for k, v in template.items() if not k.startswith("_")}
        self.assertEqual(set(fields), set(M.THRESHOLD_FIELDS))
        self.assertTrue(all(v is None for v in fields.values()))
        for key in fields:
            self.assertTrue(template["_comments"].get(key), "no comment for " + key)

    def test_null_thresholds_are_refused_and_named(self):
        with self.assertRaises(M.ThresholdError) as ctx:
            run(thresholds={k: None for k in M.THRESHOLD_FIELDS}, exploratory=False)
        self.assertIn("cpu_bias_abs_max_pp", str(ctx.exception))
        with self.assertRaises(M.ThresholdError):
            run(thresholds=None, exploratory=False)

    def test_a_single_null_among_set_thresholds_is_still_refused(self):
        thresholds = dict(F.FULL_THRESHOLDS, step_response_twin_visible_max_ms=None)
        with self.assertRaises(M.ThresholdError) as ctx:
            run(thresholds=thresholds, exploratory=False)
        self.assertIn("step_response_twin_visible_max_ms", str(ctx.exception))

    def test_unknown_keys_and_bad_values_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            for label, content in (("unknown key", dict(F.FULL_THRESHOLDS, typo_max=1)),
                                   ("negative", dict(F.FULL_THRESHOLDS, cpu_mae_max_pp=-1)),
                                   ("string", dict(F.FULL_THRESHOLDS, cpu_mae_max_pp="1")),
                                   ("boolean", dict(F.FULL_THRESHOLDS, cpu_mae_max_pp=True))):
                with self.subTest(label):
                    path = Path(d) / "t.json"
                    path.write_text(json.dumps(content), encoding="utf-8")
                    with self.assertRaises(M.ThresholdError):
                        M.load_thresholds(path, exploratory=False)
            bad = Path(d) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            with self.assertRaises(M.ThresholdError):
                M.load_thresholds(bad, exploratory=True)

    def test_pass_and_fail_follow_the_thresholds_exactly(self):
        res = run(receiver=F.receiver_records(cpu_bias=1.0), thresholds=dict(F.FULL_THRESHOLDS, cpu_bias_abs_max_pp=1.5), exploratory=False)
        self.assertEqual(rows(res, "cpu")["cpu25"]["verdicts"]["bias"], "PASS")
        res = run(receiver=F.receiver_records(cpu_bias=1.0), thresholds=dict(F.FULL_THRESHOLDS, cpu_bias_abs_max_pp=0.5), exploratory=False)
        self.assertEqual(rows(res, "cpu")["cpu25"]["verdicts"]["bias"], "FAIL")
        res = run(thresholds=dict(F.FULL_THRESHOLDS, min_samples_per_phase=19), exploratory=False)
        self.assertEqual(rows(res, "cpu")["cpu25"]["verdicts"]["n"], "FAIL")  # n is 18
        res = run(thresholds=dict(F.FULL_THRESHOLDS, step_response_pi_clock_max_ms=2000), exploratory=False)
        self.assertEqual({r["phase"]: r for r in res["quantities"]["cpu"]["step_response"]}["cpu25"]["verdicts"]["pi_clock"], "FAIL")

    def test_the_reference_versus_stimulus_verdicts_follow_their_thresholds(self):
        stim = F.stimulus_records(achieved={"cpu25": 24.0}, vmrss_extra_kib=12 * 1024)  # CPU off by 1.0 pp, memory truth off by 12 MiB
        loose = dict(F.FULL_THRESHOLDS, cpu_reference_vs_stimulus_abs_max_pp=1.5, memory_reference_vs_stimulus_abs_max_mib=15.0)
        strict = dict(F.FULL_THRESHOLDS, cpu_reference_vs_stimulus_abs_max_pp=0.5, memory_reference_vs_stimulus_abs_max_mib=5.0)
        for thresholds, expected in ((loose, "PASS"), (strict, "FAIL")):
            res = run(stimulus=stim, thresholds=thresholds, exploratory=False)
            cpu = {r["phase"]: r for r in res["quantities"]["cpu"]["reference_vs_stimulus"]}["cpu25"]
            mem = {r["phase"]: r for r in res["quantities"]["memory"]["reference_vs_stimulus"]}["mem100"]
            self.assertEqual(cpu["verdicts"]["ref_vs_achieved"], expected)
            self.assertEqual(mem["verdicts"]["ref_vs_truth"], expected)

    def test_without_thresholds_there_are_no_verdicts_in_exploratory_mode(self):
        res = run(thresholds=None, exploratory=True)
        self.assertTrue(res["exploratory"])
        self.assertIsNone(rows(res, "cpu")["cpu25"]["verdicts"]["bias"])


class ReportTests(unittest.TestCase):
    def test_the_exploratory_banner_heads_the_report_and_every_section(self):
        text = M.format_report(run())
        lines = text.splitlines()
        self.assertEqual(lines[0], M.BANNER)
        self.assertEqual(M.BANNER, "EXPLORATORY: NO PRE-REGISTERED THRESHOLDS")
        sections = [i for i, line in enumerate(lines) if line.startswith("== ")]
        self.assertGreaterEqual(len(sections), 5)
        for i in sections:
            self.assertEqual(lines[i - 1], M.BANNER, lines[i])

    def test_a_preregistered_run_has_no_banner(self):
        res = run(thresholds=F.FULL_THRESHOLDS, exploratory=False)
        self.assertNotIn("EXPLORATORY", M.format_report(res))
        self.assertIsNone(res["banner"])

    def test_the_report_ends_with_what_this_does_not_show(self):
        text = M.format_report(run()).rstrip()
        lines = text.splitlines()
        header = max(i for i, line in enumerate(lines) if line.startswith("== "))
        self.assertIn("What this does not show", lines[header])
        self.assertTrue(all(line.startswith("- ") or not line.strip() for line in lines[header + 1:]))
        body = "\n".join(lines[header + 1:])
        for needle in ("absolute accuracy", "same kernel file", "Frequency and temperature", "own CPU cost", "steady state"):
            self.assertIn(needle, body)

    def test_the_report_states_the_reference_path_and_the_truth_for_every_quantity(self):
        text = M.format_report(run())
        self.assertIn("SAME file as the sender", text)
        self.assertIn("DIFFERENT file from the sender", text)
        self.assertIn("ground truth by construction", text)
        self.assertIn("no ground truth", text.lower())


class CommandLineTests(unittest.TestCase):
    def files(self, d, **kw):
        paths = {
            "receiver": F.write_jsonl(Path(d) / "receiver.jsonl", F.receiver_records(**kw)),
            "reference": F.write_jsonl(Path(d) / "reference.jsonl", F.reference_records()),
            "stimulus": F.write_jsonl(Path(d) / "stimulus.jsonl", F.stimulus_records()),
        }
        return ["--receiver", str(paths["receiver"]), "--reference", str(paths["reference"]), "--stimulus", str(paths["stimulus"])]

    def call(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = M.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_null_thresholds_are_refused_without_exploratory_and_nothing_is_printed(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, err = self.call(self.files(d) + ["--thresholds", str(TEMPLATE)])
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertIn("cpu_bias_abs_max_pp", err)
            self.assertIn("--exploratory", err)

    def test_exploratory_runs_with_the_template_and_prints_the_banner(self):
        with tempfile.TemporaryDirectory() as d:
            code, out, err = self.call(self.files(d) + ["--thresholds", str(TEMPLATE), "--exploratory"])
            self.assertEqual(code, 0, err)
            self.assertEqual(out.splitlines()[0], M.BANNER)
            self.assertGreaterEqual(out.count(M.BANNER), 6)

    def test_complete_thresholds_run_without_a_banner_and_print_the_file_hash(self):
        with tempfile.TemporaryDirectory() as d:
            th = Path(d) / "t.json"
            th.write_text(json.dumps(F.FULL_THRESHOLDS), encoding="utf-8")
            code, out, err = self.call(self.files(d) + ["--thresholds", str(th)])
            self.assertEqual(code, 0, err)
            self.assertNotIn("EXPLORATORY", out)
            self.assertIn("sha256", out)

    def test_json_output_records_the_exploratory_state_and_the_banner(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "result.json"
            code, out, err = self.call(self.files(d) + ["--thresholds", str(TEMPLATE), "--exploratory", "--json", str(target)])
            self.assertEqual(code, 0, err)
            data = json.loads(target.read_text(encoding="utf-8"))
            self.assertTrue(data["exploratory"])
            self.assertEqual(data["banner"], M.BANNER)

    def test_a_failing_verdict_gives_exit_code_1_and_a_passing_run_gives_0(self):
        with tempfile.TemporaryDirectory() as d:
            strict = Path(d) / "strict.json"
            strict.write_text(json.dumps(dict(F.FULL_THRESHOLDS, cpu_bias_abs_max_pp=0.5)), encoding="utf-8")
            code, out, err = self.call(self.files(d, cpu_bias=1.0) + ["--thresholds", str(strict)])
            self.assertEqual(code, 1, err)
            self.assertIn("FAIL", out)
            loose = Path(d) / "loose.json"
            loose.write_text(json.dumps(F.FULL_THRESHOLDS), encoding="utf-8")
            code, out, err = self.call(self.files(d) + ["--thresholds", str(loose)])
            self.assertEqual(code, 0, err)

    def test_unparsable_lines_in_the_inputs_are_counted_in_the_report(self):
        with tempfile.TemporaryDirectory() as d:
            argv = self.files(d)
            with open(argv[1], "a", encoding="utf-8") as handle:
                handle.write("{this is not json\n")
            code, out, err = self.call(argv + ["--thresholds", str(TEMPLATE), "--exploratory"])
            self.assertEqual(code, 0, err)
            self.assertIn("unparsable lines: receiver 1", out)

    def test_missing_input_files_are_a_clean_error(self):
        code, out, err = self.call(["--receiver", "nope1", "--reference", "nope2", "--stimulus", "nope3", "--thresholds", str(TEMPLATE), "--exploratory"])
        self.assertEqual(code, 2)
        self.assertIn("not found", err)


if __name__ == "__main__":
    unittest.main()
