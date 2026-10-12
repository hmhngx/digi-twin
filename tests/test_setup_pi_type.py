"""scripts/setup_pi_type.py: dry-run by default (no request of any kind), exact
bodies, never links pi_0 into the rack tree, and --apply against a loopback fake."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import socket
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from tests.helpers import FakeDitto, ditto_pointed_at
from twin import config
from twin import pi_protocol as pp
from twin import pi_receiver as pr

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup_pi_type.py"
_spec = importlib.util.spec_from_file_location("setup_pi_type_under_test", SCRIPT)
sp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp)


def q(text):
    return urllib.parse.quote(text, safe="")


def run_main(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = sp.main(argv)
    return code, out.getvalue()


class BodyTests(unittest.TestCase):
    def test_type_body_uses_repo_policy_and_declares_every_feature_without_fake_values(self):
        body = sp.pi_type_body()
        self.assertEqual(body["policyId"], config.POLICY_ID)
        self.assertEqual(set(body["features"]), set(pp.FEATURES) | {"link"})
        for name, feature in body["features"].items():
            with self.subTest(name):
                self.assertEqual(feature, {"properties": {}})  # no seeded 0.0: nothing measured yet
        self.assertEqual(body["attributes"]["name"], "PiType")
        self.assertEqual(body["attributes"]["hardware_model"], "Raspberry Pi 3 Model B Plus Rev 1.3")
        self.assertNotIn("_parents", body["attributes"])

    def test_every_feature_unit_is_declared_in_the_type_description(self):
        text = sp.pi_type_body()["attributes"]["text_description"]
        for name, unit in pp.FEATURE_UNITS.items():
            self.assertIn(name, text)
            self.assertIn(unit, text)
        self.assertIn("last_received_epoch_ms", text)

    def test_twin_body_names_the_thing_and_hardware(self):
        body = sp.pi_twin_body()
        self.assertEqual(body["attributes"]["name"], "pi_0")
        self.assertEqual(body["attributes"]["hardware_model"], "Raspberry Pi 3 Model B Plus Rev 1.3")

    def test_plan_has_exactly_the_type_put_and_twin_create_and_no_rack_links(self):
        steps = sp.plan()
        self.assertEqual([(s["method"], s["path"]) for s in steps], [
            ("PUT", "/api/types/" + q(pr.PI_TYPE_ID)),
            ("POST", "/api/types/" + q(pr.PI_TYPE_ID) + "/create/" + q(pr.PI_THING_ID)),
        ])
        self.assertFalse(any("/children" in s["path"] for s in steps))
        self.assertTrue(pr.PI_TYPE_ID.endswith(":PiType") and pr.PI_THING_ID.endswith(":pi_0"))


class DryRunTests(unittest.TestCase):
    def test_default_is_dry_run(self):
        self.assertFalse(sp.parse_args([]).apply)
        self.assertTrue(sp.parse_args(["--apply"]).apply)

    def test_dry_run_makes_no_request_of_any_kind(self):
        def boom(*a, **k):
            raise AssertionError("network used in dry-run")

        with mock.patch("requests.request", boom), mock.patch("requests.Session.request", boom), \
                mock.patch.object(socket.socket, "connect", boom), \
                mock.patch.object(socket, "create_connection", boom):
            code, out = run_main([])
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertIn("no request was made", out)
        self.assertIn("/api/types/" + q(pr.PI_TYPE_ID), out)
        self.assertIn("--apply", out)

    def test_dry_run_prints_the_exact_bodies(self):
        _, out = run_main([])
        self.assertIn(json.dumps(sp.pi_type_body(), indent=2), out)
        self.assertIn(json.dumps(sp.pi_twin_body(), indent=2), out)


class ApplyTests(unittest.TestCase):
    def responder(self, policy_status=200):
        def respond(method, path, body):
            if path.startswith("/api/2/policies/"):
                return policy_status, b"{}"
            if method == "PUT" and path.startswith("/api/types/"):
                return 201, b"{}"
            if method == "POST" and "/create/" in path:
                return 201, b"{}"
            if method == "GET" and path.startswith("/api/2/things/"):
                return 200, json.dumps({"thingId": pr.PI_THING_ID, "features": {"link": {"properties": {}}}}).encode()
            return 500, b"unexpected"

        return respond

    def apply(self, fake):
        with ditto_pointed_at(fake):
            return run_main(["--apply"])

    def test_apply_sequence_and_bodies(self):
        fake = FakeDitto(self.responder()).start()
        self.addCleanup(fake.stop)
        code, out = self.apply(fake)
        self.assertEqual(code, 0)
        seen = [(r["method"], r["path"]) for r in fake.requests]
        self.assertEqual(seen, [
            ("GET", "/api/2/policies/" + q(config.POLICY_ID)),
            ("PUT", "/api/types/" + q(pr.PI_TYPE_ID)),
            ("POST", "/api/types/" + q(pr.PI_TYPE_ID) + "/create/" + q(pr.PI_THING_ID)),
            ("GET", "/api/2/things/" + q(pr.PI_THING_ID)),
        ])
        self.assertEqual(json.loads(fake.requests[1]["body"]), sp.pi_type_body())
        self.assertEqual(json.loads(fake.requests[2]["body"]), sp.pi_twin_body())
        self.assertNotIn("DRY RUN", out)

    def test_apply_never_touches_the_rack_tree_or_other_things(self):
        fake = FakeDitto(self.responder()).start()
        self.addCleanup(fake.stop)
        self.apply(fake)
        for r in fake.requests:
            self.assertNotIn("/children", r["path"])
            self.assertNotIn("node_", r["path"])
            self.assertNotIn("rack_", r["path"])
            self.assertIn(r["method"], ("GET", "PUT", "POST"))

    def test_missing_policy_aborts_before_any_write(self):
        fake = FakeDitto(self.responder(policy_status=404)).start()
        self.addCleanup(fake.stop)
        with self.assertRaises(SystemExit) as ctx:
            self.apply(fake)
        self.assertIn("setup_types_and_twins.py", str(ctx.exception))
        self.assertEqual([r["method"] for r in fake.requests], ["GET"])

    def test_rerun_is_idempotent_when_type_and_twin_already_exist(self):
        def respond(method, path, body):
            if path.startswith("/api/2/policies/"):
                return 200, b"{}"
            if method == "PUT" and path.startswith("/api/types/"):
                return 409, b"already exists"
            if method == "GET" and path.startswith("/api/types/"):
                return 200, b"{}"
            if method == "POST" and "/create/" in path:
                return 409, b"already exists"
            if method == "GET" and path.startswith("/api/2/things/"):
                return 200, b'{"features":{}}'
            return 500, b"unexpected"

        fake = FakeDitto(respond).start()
        self.addCleanup(fake.stop)
        code, out = self.apply(fake)
        self.assertEqual(code, 0)
        self.assertIn("already exists", out.lower())

    def test_failed_twin_creation_is_reported(self):
        def respond(method, path, body):
            if path.startswith("/api/2/policies/"):
                return 200, b"{}"
            if method == "PUT":
                return 201, b"{}"
            return 500, b"boom"

        fake = FakeDitto(respond).start()
        self.addCleanup(fake.stop)
        with self.assertRaises(RuntimeError):
            self.apply(fake)


if __name__ == "__main__":
    unittest.main()
