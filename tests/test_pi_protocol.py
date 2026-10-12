"""Tests for twin/pi_protocol.py: Linux parsers, offset math, message validation.

Numbers asserted for the fixtures were recomputed from the raw files on
2026-10-11 (see notes/PI_TELEMETRY_SPEC_2026-10-11.md section 5):
CPU 5/403 = 1.24 %, memory 31.12 %, temperature 40.78 C.
"""
from __future__ import annotations

import copy
import os
import time
import unittest

from tests.helpers import fixture_text, good_message
from twin import pi_protocol as pp

TIMEDATECTL_TEXT = (
    "               Local time: Sun 2026-10-11 16:32:46 EDT\n"
    "           Universal time: Sun 2026-10-11 20:32:46 UTC\n"
    "                 RTC time: n/a\n"
    "                Time zone: America/New_York (EDT, -0400)\n"
    "System clock synchronized: yes\n"
    "              NTP service: active\n"
    "          RTC in local TZ: no\n"
)


class ProcStatParsingTests(unittest.TestCase):
    def test_fixture_tick_totals(self):
        t1 = pp.parse_proc_stat(fixture_text("proc_stat_1.txt"))
        t2 = pp.parse_proc_stat(fixture_text("proc_stat_2.txt"))
        # total = user+nice+system+idle+iowait+irq+softirq+steal, done by hand:
        # 12752+7+4611+283923+29386+0+56+0 = 330735 ; second sample = 331138
        self.assertEqual(t1.total, 330735)
        self.assertEqual(t2.total, 331138)
        # idle column + iowait column
        self.assertEqual(t1.idle, 283923 + 29386)
        self.assertEqual(t2.idle, 284321 + 29386)

    def test_fixture_cpu_percent_is_1_24(self):
        t1 = pp.parse_proc_stat(fixture_text("proc_stat_1.txt"))
        t2 = pp.parse_proc_stat(fixture_text("proc_stat_2.txt"))
        # delta total 403, delta idle+iowait 398, busy 5 -> 500/403 = 1.2407...
        self.assertEqual(t2.total - t1.total, 403)
        self.assertEqual(pp.cpu_percent(t1, t2), 1.24)

    def test_crlf_checkout_of_fixtures_parses_the_same(self):
        t1 = pp.parse_proc_stat(fixture_text("proc_stat_1.txt", crlf=True))
        t2 = pp.parse_proc_stat(fixture_text("proc_stat_2.txt", crlf=True))
        self.assertEqual(pp.cpu_percent(t1, t2), 1.24)

    def test_lone_first_sample_has_no_baseline(self):
        t1 = pp.parse_proc_stat(fixture_text("proc_stat_1.txt"))
        self.assertIsNone(pp.cpu_percent(None, t1))

    def test_zero_delta_is_unavailable_not_zero(self):
        t1 = pp.parse_proc_stat(fixture_text("proc_stat_1.txt"))
        self.assertIsNone(pp.cpu_percent(t1, t1))

    def test_counters_going_backwards_are_unavailable(self):
        t1 = pp.parse_proc_stat(fixture_text("proc_stat_1.txt"))
        t2 = pp.parse_proc_stat(fixture_text("proc_stat_2.txt"))
        self.assertIsNone(pp.cpu_percent(t2, t1))

    def test_idle_growing_faster_than_total_is_unavailable_not_clamped(self):
        prev = pp.CpuTicks(total=1000, idle=900)
        cur = pp.CpuTicks(total=1010, idle=930)  # idle +30 > total +10 -> busy would be -20
        self.assertIsNone(pp.cpu_percent(prev, cur))

    def test_fully_busy_interval_is_100(self):
        self.assertEqual(pp.cpu_percent(pp.CpuTicks(1000, 900), pp.CpuTicks(1100, 900)), 100.0)

    def test_per_core_line_is_not_the_aggregate(self):
        self.assertIsNone(pp.parse_proc_stat("cpu0 1 2 3 4 5 6 7 8 0 0\n"))

    def test_fewer_than_eight_columns_is_unavailable(self):
        self.assertIsNone(pp.parse_proc_stat("cpu  1 2 3 4 5 6 7\n"))

    def test_guest_columns_are_not_added_to_total(self):
        with_guest = pp.parse_proc_stat("cpu  10 0 10 80 0 0 0 0 5000 7000\n")
        self.assertEqual(with_guest.total, 100)
        self.assertEqual(with_guest.idle, 80)

    def test_garbage_is_unavailable(self):
        for bad in ("", "\n", "cpu  a b c d e f g h\n", "intr 1 2 3\n", "cpu\n"):
            with self.subTest(bad=bad):
                self.assertIsNone(pp.parse_proc_stat(bad))

    def test_negative_column_is_unavailable(self):
        self.assertIsNone(pp.parse_proc_stat("cpu  10 0 -10 80 0 0 0 0\n"))


class MeminfoParsingTests(unittest.TestCase):
    def test_fixture_is_31_12_percent(self):
        # (926764 - 638312) / 926764 = 288452 / 926764 = 0.311246... -> 31.12
        self.assertEqual(pp.parse_meminfo_used_percent(fixture_text("meminfo.txt")), 31.12)

    def test_crlf_checkout(self):
        self.assertEqual(pp.parse_meminfo_used_percent(fixture_text("meminfo.txt", crlf=True)), 31.12)

    def test_missing_memavailable_is_unavailable_no_memfree_fallback(self):
        text = "MemTotal:  1000 kB\nMemFree:   100 kB\n"
        self.assertIsNone(pp.parse_meminfo_used_percent(text))

    def test_missing_memtotal_is_unavailable(self):
        self.assertIsNone(pp.parse_meminfo_used_percent("MemAvailable:  100 kB\n"))

    def test_zero_or_negative_total_is_unavailable(self):
        self.assertIsNone(pp.parse_meminfo_used_percent("MemTotal: 0 kB\nMemAvailable: 0 kB\n"))

    def test_available_above_total_is_unavailable(self):
        self.assertIsNone(pp.parse_meminfo_used_percent("MemTotal: 100 kB\nMemAvailable: 200 kB\n"))

    def test_non_numeric_is_unavailable(self):
        self.assertIsNone(pp.parse_meminfo_used_percent("MemTotal: lots kB\nMemAvailable: 1 kB\n"))

    def test_empty_is_unavailable(self):
        self.assertIsNone(pp.parse_meminfo_used_percent(""))

    def test_all_memory_free_is_zero_not_none(self):
        self.assertEqual(pp.parse_meminfo_used_percent("MemTotal: 1000 kB\nMemAvailable: 1000 kB\n"), 0.0)


class ThermalParsingTests(unittest.TestCase):
    def test_fixture_is_40_78(self):
        self.assertEqual(
            pp.parse_thermal_temp_c(fixture_text("thermal_type.txt"), fixture_text("thermal_temp.txt")),
            40.78,
        )

    def test_crlf_checkout(self):
        self.assertEqual(
            pp.parse_thermal_temp_c(
                fixture_text("thermal_type.txt", crlf=True), fixture_text("thermal_temp.txt", crlf=True)
            ),
            40.78,
        )

    def test_unexpected_zone_type_is_unavailable(self):
        self.assertIsNone(pp.parse_thermal_temp_c("acpitz\n", "40780\n"))

    def test_non_integer_temp_is_unavailable(self):
        for bad in ("", "\n", "40.78\n", "hot\n", "0x10\n"):
            with self.subTest(bad=bad):
                self.assertIsNone(pp.parse_thermal_temp_c("cpu-thermal\n", bad))

    def test_negative_millidegrees_are_kept(self):
        self.assertEqual(pp.parse_thermal_temp_c("cpu-thermal\n", "-5000\n"), -5.0)


class CpuFreqAndNtpParsingTests(unittest.TestCase):
    def test_idle_frequency_literal_from_device_capture(self):
        self.assertEqual(pp.parse_cpu_freq_khz("600000\n"), 600000)

    def test_bad_frequencies_are_unavailable(self):
        for bad in ("", "abc", "0", "-5", "1.5", "6e5"):
            with self.subTest(bad=bad):
                self.assertIsNone(pp.parse_cpu_freq_khz(bad))

    def test_ntp_yes_no_unknown(self):
        self.assertIs(pp.parse_ntp_synchronized(TIMEDATECTL_TEXT), True)
        self.assertIs(pp.parse_ntp_synchronized(TIMEDATECTL_TEXT.replace(": yes", ": no")), False)
        self.assertIsNone(pp.parse_ntp_synchronized(""))
        self.assertIsNone(pp.parse_ntp_synchronized("System clock synchronized: maybe\n"))
        self.assertIsNone(pp.parse_ntp_synchronized("NTP service: active\n"))


class FeatureRangeTests(unittest.TestCase):
    def test_in_range(self):
        self.assertTrue(pp.feature_value_ok("cpu_utilization", 0))
        self.assertTrue(pp.feature_value_ok("cpu_utilization", 100.0))
        self.assertTrue(pp.feature_value_ok("memory_utilization", 31.12))
        self.assertTrue(pp.feature_value_ok("cpu_temperature_c", -50.0))
        self.assertTrue(pp.feature_value_ok("cpu_temperature_c", 150))
        self.assertTrue(pp.feature_value_ok("cpu_frequency_khz", 600000))

    def test_out_of_range_or_wrong_type(self):
        self.assertFalse(pp.feature_value_ok("cpu_utilization", -0.01))
        self.assertFalse(pp.feature_value_ok("cpu_utilization", 100.01))
        self.assertFalse(pp.feature_value_ok("cpu_temperature_c", 150.01))
        self.assertFalse(pp.feature_value_ok("cpu_temperature_c", -50.01))
        self.assertFalse(pp.feature_value_ok("cpu_frequency_khz", 0))
        self.assertFalse(pp.feature_value_ok("cpu_frequency_khz", 10_000_001))
        self.assertFalse(pp.feature_value_ok("cpu_utilization", True))
        self.assertFalse(pp.feature_value_ok("cpu_utilization", "5"))
        self.assertFalse(pp.feature_value_ok("cpu_utilization", None))
        self.assertFalse(pp.feature_value_ok("cpu_utilization", float("nan")))
        self.assertFalse(pp.feature_value_ok("cpu_utilization", float("inf")))
        self.assertFalse(pp.feature_value_ok("battery_percent", 50))

    def test_frequency_must_be_integer(self):
        self.assertFalse(pp.feature_value_ok("cpu_frequency_khz", 600000.5))

    def test_every_feature_declares_a_unit(self):
        self.assertEqual(set(pp.FEATURE_UNITS), set(pp.FEATURES))
        self.assertEqual(pp.FEATURE_UNITS["cpu_utilization"], "percent (0-100)")
        self.assertEqual(pp.FEATURE_UNITS["memory_utilization"], "percent (0-100)")
        self.assertEqual(pp.FEATURE_UNITS["cpu_temperature_c"], "degrees Celsius")
        self.assertEqual(pp.FEATURE_UNITS["cpu_frequency_khz"], "kilohertz")


class OffsetProbeMathTests(unittest.TestCase):
    """offset = ((t2-t1)+(t3-t4))/2 = receiver clock MINUS Pi clock;
    delay = (t4-t1)-(t3-t2). Cases hand-computed in the spec, section 4.1."""

    def test_receiver_100ms_ahead_symmetric_5ms(self):
        # (10105-10000)=105, (10106-10011)=95 -> 200/2 = +100 ; (11)-(1) = 10
        self.assertEqual(pp.compute_offset_delay(10000, 10105, 10106, 10011), (100.0, 10))

    def test_receiver_250ms_behind_asymmetric_path_has_known_error(self):
        # (49770-50000)=-230, (49772-50062)=-290 -> -520/2 = -260 ; (62)-(2) = 60
        # true offset is -250; error = (d_f - d_b)/2 = (20-40)/2 = -10 -> -260
        self.assertEqual(pp.compute_offset_delay(50000, 49770, 49772, 50062), (-260.0, 60))

    def test_equal_clocks_symmetric(self):
        self.assertEqual(pp.compute_offset_delay(1000, 1004, 1004, 1008), (0.0, 8))

    def test_half_millisecond_result(self):
        self.assertEqual(pp.compute_offset_delay(0, 4, 4, 5), (1.5, 5))

    def test_sign_positive_means_receiver_ahead_and_converts_pi_time(self):
        offset, _ = pp.compute_offset_delay(10000, 10105, 10106, 10011)
        pi_time = 20000
        self.assertEqual(pi_time + offset, 20100.0)  # t_receiver = t_pi + offset

    def test_negative_delay_is_returned_not_hidden(self):
        # round trip of 5 ms but the receiver claims 490 ms between t2 and t3
        # (a clock step mid-probe): the caller must be able to see delay < 0
        _, delay = pp.compute_offset_delay(1000, 1010, 1500, 1005)
        self.assertEqual(delay, (1005 - 1000) - (1500 - 1010))
        self.assertEqual(delay, -485)


class NowMsTests(unittest.TestCase):
    def test_now_ms_is_int_epoch_utc_milliseconds(self):
        value = pp.now_ms()
        self.assertIsInstance(value, int)
        self.assertLess(abs(value - int(time.time() * 1000)), 1000)

    def test_time_ns_agrees_with_the_wall_clock(self):
        self.assertLess(abs(pp.time_ns() // 1_000_000 - time.time_ns() // 1_000_000), 50)
        self.assertIsInstance(pp.time_ns(), int)

    @unittest.skipUnless(os.name == "nt", "Windows-only: Python 3.12 time.time_ns() follows the system timer tick")
    def test_windows_clock_is_not_quantized_to_the_system_timer_tick(self):
        # time.time_ns() steps every 1 ms at best and every 15.6 ms when no process holds the
        # timer at 1 ms; the precise API must give far more distinct values in a short window.
        seen, end = set(), time.perf_counter() + 0.03
        while time.perf_counter() < end:
            seen.add(pp.time_ns())
        self.assertGreater(len(seen), 1000)


class TokenShapeTests(unittest.TestCase):
    def test_printable_ascii_without_spaces_is_ok(self):
        self.assertTrue(pp.token_ok("Zx9-_.~aB" * 4))

    def test_everything_else_is_not(self):
        for bad in ("", "has space in it", "tab" + chr(9) + "here", "new" + chr(10) + "line",
                    "na" + chr(0xEF) + "ve-token-0123456789", "ctl" + chr(1) + "char", "trailing ", " leading"):
            with self.subTest(bad=bad):
                self.assertFalse(pp.token_ok(bad))


class HugeIntegerTests(unittest.TestCase):
    """Authenticated senders may send absurd numbers; these must be ordinary protocol
    outcomes (400 / feature dropped), never an exception that surfaces as HTTP 500."""

    def test_integers_beyond_two_to_the_53_are_rejected_at_message_level(self):
        limit = 2 ** 53
        pp.validate_telemetry(good_message(seq=limit))                 # boundary accepted
        for field in ("seq", "sent_at_ms"):
            with self.subTest(field):
                with self.assertRaises(pp.ProtocolError):
                    pp.validate_telemetry(good_message(**{field: limit + 1}))
                with self.assertRaises(pp.ProtocolError):
                    pp.validate_telemetry(good_message(**{field: 10 ** 400}))

    def test_huge_probe_timestamps_are_rejected_not_raised(self):
        for t in (2 ** 53 + 1, 10 ** 400):
            with self.subTest(t=t):
                with self.assertRaises(pp.ProtocolError):
                    pp.validate_telemetry(good_message(probes=[{"t1": t, "t2": 2, "t3": 3, "t4": 4}]))
                with self.assertRaises(pp.ProtocolError):
                    pp.validate_probe_request({"v": 1, "t1": t})
                with self.assertRaises(pp.ProtocolError):
                    pp.validate_probe_reply({"v": 1, "t1": 1, "t2": t, "t3": 3}, 1)

    def test_huge_measurement_values_drop_only_that_measurement(self):
        msg = good_message()
        msg["measurements"]["cpu_utilization"]["value"] = 10 ** 400
        msg["measurements"]["cpu_frequency_khz"]["value"] = 10 ** 400
        msg["measurements"]["memory_utilization"]["measured_at_ms"] = 10 ** 400
        t = pp.validate_telemetry(msg)
        self.assertEqual(sorted(t.rejected_features), ["cpu_frequency_khz", "cpu_utilization", "memory_utilization"])
        self.assertEqual(set(t.measurements), {"cpu_temperature_c"})
        self.assertFalse(pp.feature_value_ok("cpu_utilization", 10 ** 400))
        self.assertFalse(pp.feature_value_ok("cpu_temperature_c", -(10 ** 400)))
        self.assertFalse(pp.feature_value_ok("cpu_frequency_khz", 10 ** 400))

    def test_a_five_thousand_digit_integer_in_the_body_is_a_protocol_error(self):
        with self.assertRaises(pp.ProtocolError):
            pp.parse_json_body(b'{"seq": ' + b"9" * 5000 + b"}")


class JsonBodyTests(unittest.TestCase):
    def test_parses_object(self):
        self.assertEqual(pp.parse_json_body(b'{"a": 1}'), {"a": 1})

    def test_rejects_nan_and_infinity_literals(self):
        for raw in (b'{"x": NaN}', b'{"x": Infinity}', b'{"x": -Infinity}'):
            with self.subTest(raw=raw):
                with self.assertRaises(pp.ProtocolError):
                    pp.parse_json_body(raw)

    def test_rejects_non_utf8_and_non_json_and_non_object(self):
        for raw in (b"\xff\xfe\x00", b"not json", b"", b"[]", b'"str"', b"42", b"null"):
            with self.subTest(raw=raw):
                with self.assertRaises(pp.ProtocolError):
                    pp.parse_json_body(raw)

    def test_pathological_nesting_is_a_protocol_error_not_a_crash(self):
        for raw in (b"[" * 5000, b"[" * 5000 + b"]" * 5000, b'{"a":' * 3000 + b"1" + b"}" * 3000):
            with self.subTest(len=len(raw)):
                with self.assertRaises(pp.ProtocolError):
                    pp.parse_json_body(raw)


class ProbeMessageTests(unittest.TestCase):
    def test_probe_request_ok(self):
        self.assertEqual(pp.validate_probe_request({"v": 1, "t1": 1760214000000}), 1760214000000)

    def test_probe_request_rejections(self):
        for bad in ({}, {"v": 1}, {"v": 2, "t1": 5}, {"v": 1, "t1": -1}, {"v": 1, "t1": 1.5},
                    {"v": 1, "t1": True}, {"v": 1, "t1": "5"}, [], None):
            with self.subTest(bad=bad):
                with self.assertRaises(pp.ProtocolError):
                    pp.validate_probe_request(bad)

    def test_probe_reply_ok_returns_t2_t3(self):
        self.assertEqual(pp.validate_probe_reply({"v": 1, "t1": 7, "t2": 11, "t3": 12}, 7), (11, 12))

    def test_probe_reply_must_echo_t1_and_carry_integer_t2_t3(self):
        for bad in ({"v": 1, "t1": 8, "t2": 11, "t3": 12}, {"v": 1, "t1": 7, "t2": 11},
                    {"v": 1, "t1": 7, "t2": 11.5, "t3": 12}, {"v": 1, "t1": 7, "t2": True, "t3": 12},
                    {"v": 2, "t1": 7, "t2": 11, "t3": 12}, {}, [], None):
            with self.subTest(bad=bad):
                with self.assertRaises(pp.ProtocolError):
                    pp.validate_probe_reply(bad, 7)


class ValidateTelemetryTests(unittest.TestCase):
    def test_good_message(self):
        t = pp.validate_telemetry(good_message())
        self.assertEqual(t.session_id, "9f3a1c5e7b2d4a60")
        self.assertEqual(t.seq, 41)
        self.assertEqual(t.sent_at_ms, 1760214000123)
        self.assertIs(t.ntp_synchronized, True)
        self.assertEqual(set(t.measurements), set(pp.FEATURES))
        self.assertEqual(t.measurements["cpu_utilization"]["value"], 1.24)
        self.assertEqual(t.measurements["cpu_utilization"]["window_ms"], 3004)
        self.assertEqual(t.measurements["cpu_frequency_khz"]["measured_at_ms"], 1760214000121)
        self.assertEqual(t.rejected_features, [])
        self.assertEqual(len(t.probes), 1)

    def test_heartbeat_with_empty_measurements_is_valid(self):
        t = pp.validate_telemetry(good_message(measurements={}))
        self.assertEqual(t.measurements, {})

    def test_ntp_absent_is_unknown_and_non_bool_is_rejected(self):
        msg = good_message()
        del msg["ntp_synchronized"]
        self.assertIsNone(pp.validate_telemetry(msg).ntp_synchronized)
        self.assertIsNone(pp.validate_telemetry(good_message(ntp_synchronized=None)).ntp_synchronized)
        with self.assertRaises(pp.ProtocolError):
            pp.validate_telemetry(good_message(ntp_synchronized="yes"))

    def test_probes_optional(self):
        msg = good_message()
        del msg["probes"]
        self.assertEqual(pp.validate_telemetry(msg).probes, [])

    def test_extra_top_level_keys_are_ignored(self):
        pp.validate_telemetry(good_message(whatever="x"))

    def test_message_level_rejections(self):
        bad_cases = {
            "wrong version": good_message(v=2),
            "version missing": {k: v for k, v in good_message().items() if k != "v"},
            "version bool": good_message(v=True),
            "session missing": {k: v for k, v in good_message().items() if k != "session_id"},
            "session too short": good_message(session_id="abc"),
            "session bad chars": good_message(session_id="has space!!"),
            "session too long": good_message(session_id="a" * 65),
            "session not str": good_message(session_id=12345678),
            "seq negative": good_message(seq=-1),
            "seq bool": good_message(seq=True),
            "seq float": good_message(seq=1.0),
            "seq str": good_message(seq="1"),
            "seq missing": {k: v for k, v in good_message().items() if k != "seq"},
            "sent_at negative": good_message(sent_at_ms=-1),
            "sent_at float": good_message(sent_at_ms=1.5),
            "sent_at missing": {k: v for k, v in good_message().items() if k != "sent_at_ms"},
            "measurements list": good_message(measurements=[]),
            "measurements missing": {k: v for k, v in good_message().items() if k != "measurements"},
            "probes not list": good_message(probes={}),
            "too many probes": good_message(probes=[{"t1": 1, "t2": 2, "t3": 3, "t4": 4}] * 9),
            "probe missing t4": good_message(probes=[{"t1": 1, "t2": 2, "t3": 3}]),
            "probe float": good_message(probes=[{"t1": 1, "t2": 2, "t3": 3.5, "t4": 4}]),
            "probe bool": good_message(probes=[{"t1": 1, "t2": True, "t3": 3, "t4": 4}]),
            "probe not dict": good_message(probes=[1]),
        }
        for label, msg in bad_cases.items():
            with self.subTest(label):
                with self.assertRaises(pp.ProtocolError):
                    pp.validate_telemetry(msg)

    def test_not_an_object_is_rejected(self):
        for bad in ([], "x", 1, None):
            with self.subTest(bad=bad):
                with self.assertRaises(pp.ProtocolError):
                    pp.validate_telemetry(bad)

    def test_unknown_feature_is_dropped_others_kept(self):
        msg = good_message()
        msg["measurements"]["battery_percent"] = {"value": 50, "measured_at_ms": 1760214000000}
        t = pp.validate_telemetry(msg)
        self.assertNotIn("battery_percent", t.measurements)
        self.assertEqual(set(t.measurements), set(pp.FEATURES))
        self.assertEqual(t.rejected_features, ["battery_percent"])

    def test_feature_level_rejections_drop_only_that_feature(self):
        def with_cpu(entry):
            msg = good_message()
            msg["measurements"]["cpu_utilization"] = entry
            return msg

        bad_entries = {
            "bool value": {"value": True, "measured_at_ms": 1760214000118},
            "string value": {"value": "1.2", "measured_at_ms": 1760214000118},
            "null value": {"value": None, "measured_at_ms": 1760214000118},
            "over 100": {"value": 100.5, "measured_at_ms": 1760214000118},
            "negative": {"value": -0.5, "measured_at_ms": 1760214000118},
            "value missing": {"measured_at_ms": 1760214000118},
            "measured_at missing": {"value": 1.0},
            "measured_at float": {"value": 1.0, "measured_at_ms": 1.5},
            "measured_at bool": {"value": 1.0, "measured_at_ms": True},
            "measured_at negative": {"value": 1.0, "measured_at_ms": -1},
            "window zero": {"value": 1.0, "measured_at_ms": 1760214000118, "window_ms": 0},
            "window float": {"value": 1.0, "measured_at_ms": 1760214000118, "window_ms": 2.5},
            "not an object": 5,
        }
        for label, entry in bad_entries.items():
            with self.subTest(label):
                t = pp.validate_telemetry(with_cpu(entry))
                self.assertNotIn("cpu_utilization", t.measurements)
                self.assertEqual(t.rejected_features, ["cpu_utilization"])
                self.assertIn("memory_utilization", t.measurements)

    def test_temperature_and_frequency_range_rejections(self):
        msg = good_message()
        msg["measurements"]["cpu_temperature_c"]["value"] = 400
        msg["measurements"]["cpu_frequency_khz"]["value"] = 0
        t = pp.validate_telemetry(msg)
        self.assertEqual(sorted(t.rejected_features), ["cpu_frequency_khz", "cpu_temperature_c"])

    def test_extra_keys_inside_a_measurement_are_not_propagated(self):
        msg = good_message()
        msg["measurements"]["memory_utilization"]["evil"] = "x"
        t = pp.validate_telemetry(msg)
        self.assertEqual(set(t.measurements["memory_utilization"]), {"value", "measured_at_ms"})

    def test_rejected_feature_names_are_truncated(self):
        msg = good_message()
        msg["measurements"]["x" * 500] = {"value": 1, "measured_at_ms": 1}
        t = pp.validate_telemetry(msg)
        self.assertEqual(len(t.rejected_features), 1)
        self.assertLessEqual(len(t.rejected_features[0]), 64)

    def test_validate_does_not_mutate_input(self):
        msg = good_message()
        before = copy.deepcopy(msg)
        pp.validate_telemetry(msg)
        self.assertEqual(msg, before)


if __name__ == "__main__":
    unittest.main()
