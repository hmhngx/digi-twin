"""Receiver decision logic without any sockets: ordering, dedup, Ditto body,
failure handling and the run log. A recording callable stands in for the
Ditto PATCH here; tests/test_pi_ditto_patch.py exercises the real HTTP path.
"""
from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import good_message
from twin import pi_protocol as pp
from twin import pi_receiver as pr

THING = "hnguyen.clustertwin:pi_0"
SESSION_A = "aaaaaaaaaaaaaaaa"
SESSION_B = "bbbbbbbbbbbbbbbb"


class Recorder:
    """Stands in for the Ditto PATCH callable: records calls, returns a status."""

    def __init__(self, status=204, exc=None):
        self.calls = []
        self.status = status
        self.exc = exc

    def __call__(self, thing_id, body):
        self.calls.append((thing_id, copy.deepcopy(body)))
        if self.exc is not None:
            raise self.exc
        return self.status


class ListLog:
    def __init__(self):
        self.records = []

    def write(self, record):
        self.records.append(copy.deepcopy(record))

    def of_type(self, kind):
        return [r for r in self.records if r["type"] == kind]


def make_clock(start=5000):
    state = {"now": start}

    def clock():
        state["now"] += 1
        return state["now"]

    return clock


def telemetry(seq=1, session=SESSION_A, **overrides):
    return pp.validate_telemetry(good_message(seq=seq, session_id=session, **overrides))


def make_core(patch=None, dry_run=False, clock=None, run_log=None, lines=None):
    patch = patch if patch is not None else Recorder()
    log = ListLog() if run_log is None else run_log
    lines = [] if lines is None else lines
    core = pr.PiReceiver(
        patch_fn=patch,
        thing_id=THING,
        run_log=log,
        clock_ms=clock or make_clock(),
        dry_run=dry_run,
        log=lines.append,
    )
    return core, patch, log, lines


class ApplyAndPatchBodyTests(unittest.TestCase):
    def test_applied_message_patches_once_with_features_and_link(self):
        core, patch, _, _ = make_core()
        reply = core.handle_telemetry(telemetry(seq=41), received_at_ms=1760214000200)
        self.assertEqual(reply["status"], "applied")
        self.assertEqual(reply["ditto"], "ok")
        self.assertEqual(reply["received_at_ms"], 1760214000200)
        self.assertEqual(reply["v"], 1)
        self.assertEqual(len(patch.calls), 1)
        thing_id, body = patch.calls[0]
        self.assertEqual(thing_id, THING)
        self.assertEqual(
            body,
            {
                "cpu_utilization": {"properties": {"value": 1.24}},
                "memory_utilization": {"properties": {"value": 31.12}},
                "cpu_temperature_c": {"properties": {"value": 40.78}},
                "cpu_frequency_khz": {"properties": {"value": 600000}},
                "link": {
                    "properties": {
                        "session_id": SESSION_A,
                        "last_seq": 41,
                        "last_received_epoch_ms": 1760214000200,
                    }
                },
            },
        )

    def test_omitted_measurement_is_absent_from_the_patch_not_zero(self):
        msg = good_message(seq=2, session_id=SESSION_A)
        del msg["measurements"]["cpu_temperature_c"]
        del msg["measurements"]["cpu_utilization"]
        core, patch, _, _ = make_core()
        core.handle_telemetry(pp.validate_telemetry(msg), received_at_ms=10)
        body = patch.calls[0][1]
        self.assertEqual(set(body), {"memory_utilization", "cpu_frequency_khz", "link"})

    def test_heartbeat_with_no_measurements_still_updates_link(self):
        core, patch, _, _ = make_core()
        core.handle_telemetry(telemetry(seq=3, measurements={}), received_at_ms=77)
        body = patch.calls[0][1]
        self.assertEqual(list(body), ["link"])
        self.assertEqual(body["link"]["properties"]["last_seq"], 3)
        self.assertEqual(body["link"]["properties"]["last_received_epoch_ms"], 77)

    def test_link_uses_receive_time_not_patch_time(self):
        core, patch, _, _ = make_core(clock=make_clock(9000))
        core.handle_telemetry(telemetry(seq=1), received_at_ms=1234)
        self.assertEqual(patch.calls[0][1]["link"]["properties"]["last_received_epoch_ms"], 1234)

    def test_link_advances_across_messages(self):
        core, patch, _, _ = make_core()
        core.handle_telemetry(telemetry(seq=1), received_at_ms=100)
        core.handle_telemetry(telemetry(seq=2), received_at_ms=200)
        seqs = [c[1]["link"]["properties"]["last_seq"] for c in patch.calls]
        times = [c[1]["link"]["properties"]["last_received_epoch_ms"] for c in patch.calls]
        self.assertEqual(seqs, [1, 2])
        self.assertEqual(times, [100, 200])

    def test_patch_body_never_carries_measured_at_or_window(self):
        core, patch, _, _ = make_core()
        core.handle_telemetry(telemetry(seq=1), received_at_ms=1)
        text = json.dumps(patch.calls[0][1])
        self.assertNotIn("measured_at_ms", text)
        self.assertNotIn("window_ms", text)


class OrderingAndDedupTests(unittest.TestCase):
    def test_duplicate_is_not_applied_twice(self):
        core, patch, _, _ = make_core()
        first = core.handle_telemetry(telemetry(seq=5), received_at_ms=1)
        again = core.handle_telemetry(telemetry(seq=5), received_at_ms=2)
        self.assertEqual(first["status"], "applied")
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["ditto"], "skipped")
        self.assertEqual(len(patch.calls), 1)

    def test_late_older_message_is_stale_and_never_moves_ditto_backwards(self):
        core, patch, _, _ = make_core()
        core.handle_telemetry(telemetry(seq=5), received_at_ms=1)
        late = core.handle_telemetry(telemetry(seq=3), received_at_ms=2)
        self.assertEqual(late["status"], "stale")
        self.assertEqual(late["ditto"], "skipped")
        self.assertEqual(len(patch.calls), 1)
        # delivering that late message again is now a duplicate
        self.assertEqual(core.handle_telemetry(telemetry(seq=3), received_at_ms=3)["status"], "duplicate")
        # and newer data still applies
        self.assertEqual(core.handle_telemetry(telemetry(seq=6), received_at_ms=4)["status"], "applied")
        self.assertEqual(len(patch.calls), 2)

    def test_gap_is_applied_and_counted_in_the_run_log(self):
        core, patch, log, _ = make_core()
        core.handle_telemetry(telemetry(seq=1), received_at_ms=1)
        core.handle_telemetry(telemetry(seq=4), received_at_ms=2)
        missed = [r["missed"] for r in log.of_type("sample")]
        # the first message of a session says nothing about loss (the receiver may have started
        # mid-run), so its gap is unknown; a later gap of seq 2 and 3 is counted
        self.assertEqual(missed, [None, 2])
        self.assertEqual(len(patch.calls), 2)

    def test_a_session_that_starts_at_seq_zero_has_no_missed_messages(self):
        core, _, log, _ = make_core()
        core.handle_telemetry(telemetry(seq=0), received_at_ms=1)
        self.assertEqual(log.of_type("sample")[0]["missed"], 0)

    def test_a_message_older_than_the_seen_window_at_the_max_seq_is_never_applied(self):
        core, patch, _, _ = make_core()
        core.handle_telemetry(telemetry(seq=2000), received_at_ms=1)
        for seq in range(1100):  # 1100 distinct late arrivals push seq 2000 out of the window
            self.assertEqual(core.handle_telemetry(telemetry(seq=seq), received_at_ms=2)["status"], "stale")
        again = core.handle_telemetry(telemetry(seq=2000), received_at_ms=3)
        self.assertNotEqual(again["status"], "applied")
        self.assertEqual(len(patch.calls), 1)

    def test_state_lives_in_memory_so_a_restart_forgets_sessions(self):
        # Documented limitation (spec sections 7 and 9): after a receiver restart an old session
        # looks new. Anyone holding the token could forge fresh data anyway.
        first, patch1, _, _ = make_core()
        first.handle_telemetry(telemetry(seq=10, session=SESSION_A), received_at_ms=1)
        restarted, patch2, _, _ = make_core()
        reply = restarted.handle_telemetry(telemetry(seq=10, session=SESSION_A), received_at_ms=2)
        self.assertEqual(reply["status"], "applied")
        self.assertEqual(len(patch2.calls), 1)

    def test_identical_messages_arriving_at_the_same_time_apply_exactly_once(self):
        import threading

        core, patch, _, _ = make_core()
        statuses = []

        def send():
            statuses.append(core.handle_telemetry(telemetry(seq=7), received_at_ms=5)["status"])

        threads = [threading.Thread(target=send) for _ in range(20)]
        [t.start() for t in threads]
        [t.join(10) for t in threads]
        self.assertEqual(sorted(statuses), ["applied"] + ["duplicate"] * 19)
        self.assertEqual(len(patch.calls), 1)

    def test_console_output_is_written_outside_the_lock_so_a_stalled_console_blocks_nobody(self):
        import threading

        release = threading.Event()
        patch = Recorder()

        def stuck_console(line):
            release.wait(5)  # e.g. a Windows console paused by a click (QuickEdit)

        core = pr.PiReceiver(patch_fn=patch, thing_id=THING, run_log=ListLog(), clock_ms=make_clock(),
                             dry_run=False, log=stuck_console)
        first = threading.Thread(target=lambda: core.handle_telemetry(telemetry(seq=1, session=SESSION_A), 1))
        second = threading.Thread(target=lambda: core.handle_telemetry(telemetry(seq=1, session=SESSION_B), 2))
        first.start()
        second.start()
        deadline = __import__("time").monotonic() + 3
        while len(patch.calls) < 2 and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.01)
        both_patched = len(patch.calls) == 2
        release.set()
        first.join(5)
        second.join(5)
        self.assertTrue(both_patched, "the second request was blocked behind the first one's console write")

    def test_new_session_becomes_current_and_old_session_stragglers_are_stale_session(self):
        core, patch, _, _ = make_core()
        core.handle_telemetry(telemetry(seq=10, session=SESSION_A), received_at_ms=1)
        switched = core.handle_telemetry(telemetry(seq=0, session=SESSION_B), received_at_ms=2)
        self.assertEqual(switched["status"], "applied")
        straggler = core.handle_telemetry(telemetry(seq=11, session=SESSION_A), received_at_ms=3)
        self.assertEqual(straggler["status"], "stale_session")
        self.assertEqual(straggler["ditto"], "skipped")
        self.assertEqual(len(patch.calls), 2)
        self.assertEqual(patch.calls[1][1]["link"]["properties"]["session_id"], SESSION_B)

    def test_same_seq_in_different_sessions_are_independent(self):
        core, patch, _, _ = make_core()
        self.assertEqual(core.handle_telemetry(telemetry(seq=1, session=SESSION_A), 1)["status"], "applied")
        self.assertEqual(core.handle_telemetry(telemetry(seq=1, session=SESSION_B), 2)["status"], "applied")

    def test_seen_window_is_bounded(self):
        core, patch, _, _ = make_core()
        for seq in range(1100):
            core.handle_telemetry(telemetry(seq=seq), received_at_ms=seq)
        # seq 0 fell out of the 1024-entry window: it can no longer be told from a late arrival
        self.assertEqual(core.handle_telemetry(telemetry(seq=0), received_at_ms=2000)["status"], "stale")
        self.assertEqual(core.handle_telemetry(telemetry(seq=1099), received_at_ms=2001)["status"], "duplicate")

    def test_many_sessions_do_not_grow_state_without_bound(self):
        core, _, _, _ = make_core()
        for i in range(200):
            core.handle_telemetry(telemetry(seq=0, session="s%015d" % i), received_at_ms=i)
        self.assertLessEqual(core.known_session_count(), 16)


class FailureHandlingTests(unittest.TestCase):
    def test_ditto_http_error_is_reported_and_does_not_raise(self):
        core, patch, log, lines = make_core(patch=Recorder(status=500))
        reply = core.handle_telemetry(telemetry(seq=1), received_at_ms=1)
        self.assertEqual(reply["status"], "applied")
        self.assertEqual(reply["ditto"], "failed")
        self.assertIsNone(log.of_type("sample")[0]["patched_at_ms"])
        self.assertTrue(any("HTTP 500" in line for line in lines))

    def test_ditto_404_tells_the_operator_to_run_setup(self):
        core, _, _, lines = make_core(patch=Recorder(status=404))
        core.handle_telemetry(telemetry(seq=1), received_at_ms=1)
        self.assertTrue(any("setup_pi_type.py" in line for line in lines))

    def test_ditto_exception_is_reported_and_does_not_raise(self):
        core, _, log, lines = make_core(patch=Recorder(exc=ConnectionError("boom")))
        reply = core.handle_telemetry(telemetry(seq=1), received_at_ms=1)
        self.assertEqual(reply["ditto"], "failed")
        self.assertIsNone(log.of_type("sample")[0]["patched_at_ms"])
        self.assertTrue(any("ConnectionError" in line for line in lines))

    def test_failed_patch_still_consumes_the_sequence_number(self):
        core, patch, _, _ = make_core(patch=Recorder(status=500))
        core.handle_telemetry(telemetry(seq=1), received_at_ms=1)
        again = core.handle_telemetry(telemetry(seq=1), received_at_ms=2)
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(len(patch.calls), 1)

    def test_dry_run_never_calls_ditto(self):
        core, patch, log, _ = make_core(dry_run=True)
        reply = core.handle_telemetry(telemetry(seq=1), received_at_ms=1)
        self.assertEqual(reply["status"], "applied")
        self.assertEqual(reply["ditto"], "disabled")
        self.assertEqual(patch.calls, [])
        self.assertIsNone(log.of_type("sample")[0]["patched_at_ms"])

    def test_rejected_features_are_reported_and_logged(self):
        msg = good_message(seq=1, session_id=SESSION_A)
        msg["measurements"]["battery_percent"] = {"value": 50, "measured_at_ms": 1}
        core, patch, log, _ = make_core()
        reply = core.handle_telemetry(pp.validate_telemetry(msg), received_at_ms=1)
        self.assertEqual(reply["rejected_features"], ["battery_percent"])
        self.assertNotIn("battery_percent", patch.calls[0][1])
        self.assertEqual(log.of_type("sample")[0]["rejected_features"], ["battery_percent"])


class RunLogTests(unittest.TestCase):
    def test_sample_record_has_per_sample_receive_time_and_patch_time(self):
        clock = make_clock(8000)  # first clock() call returns 8001
        core, _, log, _ = make_core(clock=clock)
        core.handle_telemetry(telemetry(seq=41), received_at_ms=1760214000200)
        rec = log.of_type("sample")[0]
        self.assertEqual(rec["session_id"], SESSION_A)
        self.assertEqual(rec["seq"], 41)
        self.assertEqual(rec["status"], "applied")
        self.assertEqual(rec["sent_at_ms"], 1760214000123)
        self.assertEqual(rec["received_at_ms"], 1760214000200)
        self.assertEqual(rec["patched_at_ms"], 8001)  # stamped after the PATCH returned
        self.assertEqual(rec["ditto"], "ok")
        self.assertIs(rec["ntp_synchronized"], True)
        self.assertEqual(rec["measurements"]["cpu_utilization"]["window_ms"], 3004)
        self.assertEqual(rec["measurements"]["cpu_utilization"]["measured_at_ms"], 1760214000118)

    def test_probe_offsets_are_computed_with_the_shared_formula(self):
        core, _, log, _ = make_core()
        msg = good_message(
            seq=1,
            session_id=SESSION_A,
            probes=[{"t1": 10000, "t2": 10105, "t3": 10106, "t4": 10011}],
        )
        core.handle_telemetry(pp.validate_telemetry(msg), received_at_ms=1)
        probe = log.of_type("sample")[0]["probes"][0]
        self.assertEqual(probe, {"t1": 10000, "t2": 10105, "t3": 10106, "t4": 10011,
                                 "offset_ms": 100.0, "delay_ms": 10})

    def test_duplicates_and_stale_are_logged_but_duplicate_probes_are_not_relogged(self):
        core, _, log, _ = make_core()
        core.handle_telemetry(telemetry(seq=5), received_at_ms=1)
        core.handle_telemetry(telemetry(seq=5), received_at_ms=2)
        core.handle_telemetry(telemetry(seq=3), received_at_ms=3)
        samples = log.of_type("sample")
        self.assertEqual([s["status"] for s in samples], ["applied", "duplicate", "stale"])
        self.assertEqual(len(samples[0]["probes"]), 1)
        self.assertEqual(samples[1]["probes"], [])
        self.assertEqual(len(samples[2]["probes"]), 1)

    def test_probe_reply_stamps_t3_last_and_writes_nothing_before_returning(self):
        core, _, log, _ = make_core(clock=make_clock(700))
        reply = core.probe_reply(t1=111, t2=650)
        self.assertEqual(reply, {"v": 1, "t1": 111, "t2": 650, "t3": 701})
        # file I/O between stamping t3 and sending the reply would land in the return leg
        self.assertEqual(log.records, [])
        core.log_probe_served(reply)
        self.assertEqual(log.of_type("probe_served"), [{"type": "probe_served", "t1": 111, "t2": 650, "t3": 701}])

    def test_start_record(self):
        log = ListLog()
        core, _, _, _ = make_core(run_log=log, dry_run=True)
        core.log_start()
        rec = log.of_type("start")[0]
        self.assertEqual(rec["thing_id"], THING)
        self.assertIs(rec["dry_run"], True)
        self.assertEqual(rec["protocol"], 1)
        self.assertIsInstance(rec["started_at_ms"], int)


class JsonlRunLogTests(unittest.TestCase):
    def test_each_record_is_one_flushed_json_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "run.jsonl"
            log = pr.JsonlRunLog(path)
            log.write({"type": "x", "n": 1})
            log.write({"type": "y", "n": 2})
            # readable before close(): proves it is flushed
            lines = path.read_text(encoding="utf-8").splitlines()
            log.close()
        self.assertEqual([json.loads(l) for l in lines], [{"type": "x", "n": 1}, {"type": "y", "n": 2}])


class TokenLoadingTests(unittest.TestCase):
    def test_missing_or_short_token_is_refused(self):
        for env in ({}, {"PI_SHARED_TOKEN": ""}, {"PI_SHARED_TOKEN": "short"}, {"PI_SHARED_TOKEN": "x" * 15}):
            with self.subTest(env=env):
                with self.assertRaises(SystemExit):
                    pr.load_token(env)

    def test_token_with_spaces_control_or_non_ascii_characters_is_refused(self):
        for bad in ("a b " * 8, "na" + chr(0xEF) + "ve-token-0123456789abc", "tab" + chr(9) + "-0123456789abcdefg"):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit) as ctx:
                    pr.load_token({"PI_SHARED_TOKEN": bad})
                self.assertNotIn(bad, str(ctx.exception))

    def test_good_token_is_returned_as_bytes(self):
        self.assertEqual(pr.load_token({"PI_SHARED_TOKEN": "t" * 16}), b"t" * 16)

    def test_refusal_message_does_not_echo_the_token(self):
        try:
            pr.load_token({"PI_SHARED_TOKEN": "SECRET-short"})
        except SystemExit as exc:
            self.assertNotIn("SECRET-short", str(exc))
        else:
            self.fail("expected SystemExit")


class ConstantsTests(unittest.TestCase):
    def test_ids_come_from_repo_config_and_pi_0_is_outside_node_ids(self):
        from twin import config

        self.assertEqual(pr.PI_THING_ID, config.NAMESPACE + ":pi_0")
        self.assertEqual(pr.PI_TYPE_ID, config.NAMESPACE + ":PiType")
        self.assertNotIn(pr.PI_THING_ID, config.NODE_IDS)
        self.assertNotIn(pr.PI_THING_ID, sum(config.RACK_NODES.values(), []))


if __name__ == "__main__":
    unittest.main()
