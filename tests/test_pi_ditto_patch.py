"""The receiver's real Ditto PATCH, sent through the repo's own
twin/ditto_client.py to a loopback fake Ditto that records what arrives.
Asserts the exact path, content type and body (spec section 8)."""
from __future__ import annotations

import json
import socket
import time
import unittest
import urllib.parse

from tests.helpers import FakeDitto, ditto_pointed_at, good_message
from tests.test_pi_receiver_core import ListLog, make_clock
from twin import pi_protocol as pp
from twin import pi_receiver as pr


def core_with(timeout=3.0, log=None):
    lines = []
    core = pr.PiReceiver(
        patch_fn=pr.make_ditto_patch(timeout=timeout),
        thing_id=pr.PI_THING_ID,
        run_log=log if log is not None else ListLog(),
        clock_ms=make_clock(2000),
        dry_run=False,
        log=lines.append,
    )
    return core, lines


def msg(seq=1, session="aaaaaaaaaaaaaaaa", **kw):
    return pp.validate_telemetry(good_message(seq=seq, session_id=session, **kw))


class DittoPatchWireTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDitto().start()
        self.addCleanup(self.fake.stop)
        ctx = ditto_pointed_at(self.fake)
        ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)

    def test_exact_path_content_type_and_body(self):
        core, _ = core_with()
        reply = core.handle_telemetry(msg(seq=41), received_at_ms=1760214000200)
        self.assertEqual((reply["status"], reply["ditto"]), ("applied", "ok"))
        self.assertEqual(len(self.fake.requests), 1)
        req = self.fake.requests[0]

        self.assertEqual(req["method"], "PATCH")
        # Independent oracle: build the path with urllib directly, not with the repo's enc().
        self.assertTrue(pr.PI_THING_ID.endswith(":pi_0"))
        expected_path = "/api/2/things/" + urllib.parse.quote(pr.PI_THING_ID, safe="") + "/features"
        self.assertEqual(req["path"], expected_path)
        self.assertTrue(req["path"].endswith("%3Api_0/features"), req["path"])
        self.assertEqual(req["headers"]["content-type"], "application/merge-patch+json")
        self.assertTrue(req["headers"]["authorization"].startswith("Basic "))
        self.assertEqual(
            json.loads(req["body"]),
            {
                "cpu_utilization": {"properties": {"value": 1.24}},
                "memory_utilization": {"properties": {"value": 31.12}},
                "cpu_temperature_c": {"properties": {"value": 40.78}},
                "cpu_frequency_khz": {"properties": {"value": 600000}},
                "link": {
                    "properties": {
                        "session_id": "aaaaaaaaaaaaaaaa",
                        "last_seq": 41,
                        "last_received_epoch_ms": 1760214000200,
                    }
                },
            },
        )

    def test_link_feature_updates_on_every_applied_message_and_only_those(self):
        core, _ = core_with()
        core.handle_telemetry(msg(seq=1), received_at_ms=1000)
        core.handle_telemetry(msg(seq=2), received_at_ms=4000)
        core.handle_telemetry(msg(seq=2), received_at_ms=4001)  # duplicate: no request
        core.handle_telemetry(msg(seq=1), received_at_ms=4002)  # duplicate: no request
        links = [json.loads(r["body"])["link"]["properties"] for r in self.fake.requests]
        self.assertEqual([l["last_seq"] for l in links], [1, 2])
        self.assertEqual([l["last_received_epoch_ms"] for l in links], [1000, 4000])

    def test_omitted_measurement_is_not_in_the_wire_body(self):
        raw = good_message(seq=1)
        del raw["measurements"]["cpu_utilization"]
        core, _ = core_with()
        core.handle_telemetry(pp.validate_telemetry(raw), received_at_ms=1)
        body = json.loads(self.fake.requests[0]["body"])
        self.assertNotIn("cpu_utilization", body)
        self.assertIn("link", body)

    def test_no_request_for_the_rest_of_the_ditto_api(self):
        core, _ = core_with()
        core.handle_telemetry(msg(seq=1), received_at_ms=1)
        self.assertEqual({r["method"] for r in self.fake.requests}, {"PATCH"})

    def test_ditto_404_is_failed_not_fatal(self):
        self.fake.responder = lambda m, p, b: (404, b'{"status":404}')
        core, lines = core_with()
        reply = core.handle_telemetry(msg(seq=1), received_at_ms=1)
        self.assertEqual(reply["ditto"], "failed")
        self.assertTrue(any("setup_pi_type.py" in l for l in lines))
        self.fake.responder = lambda m, p, b: (204, b"")
        self.assertEqual(core.handle_telemetry(msg(seq=2), received_at_ms=2)["ditto"], "ok")

    def test_ditto_500_is_failed(self):
        self.fake.responder = lambda m, p, b: (500, b"oops")
        core, _ = core_with()
        self.assertEqual(core.handle_telemetry(msg(seq=1), received_at_ms=1)["ditto"], "failed")

    def test_slow_ditto_is_cut_off_by_the_short_timeout(self):
        self.fake.delay = 1.5
        core, _ = core_with(timeout=0.3)
        started = time.monotonic()
        reply = core.handle_telemetry(msg(seq=1), received_at_ms=1)
        self.assertEqual(reply["ditto"], "failed")
        self.assertLess(time.monotonic() - started, 1.2)

    def test_default_timeout_is_short(self):
        self.assertLessEqual(pr.DITTO_TIMEOUT_SEC, 5.0)
        self.assertEqual(pr.DITTO_TIMEOUT_SEC, 3.0)


class DittoUnreachableTests(unittest.TestCase):
    def test_connection_refused_is_failed_not_fatal(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()  # nothing listens on this port now

        class Dead:
            base_url = "http://127.0.0.1:%d" % port

        with ditto_pointed_at(Dead):
            core, lines = core_with(timeout=0.5)
            started = time.monotonic()
            reply = core.handle_telemetry(msg(seq=1), received_at_ms=1)
        self.assertEqual(reply["ditto"], "failed")
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertTrue(any("failed" in l.lower() for l in lines))


if __name__ == "__main__":
    unittest.main()
