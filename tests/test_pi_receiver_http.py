"""Receiver HTTP layer over real loopback sockets: auth, size and content
checks, bad payloads, logging hygiene, startup rules."""
from __future__ import annotations

import contextlib
import hmac
import http.client
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import good_message
from tests.test_pi_receiver_core import ListLog, Recorder, make_clock
from twin import pi_protocol as pp
from twin import pi_receiver as pr

THING = "hnguyen.clustertwin:pi_0"
TOKEN = "unit-test-token-0123456789abcdef"
WRONG_SAME_LEN = "unit-test-token-0123456789abcdeX"


class ServerCase(unittest.TestCase):
    def start(self, patch=None, run_log=None, dry_run=False, request_timeout=0.5, **server_kwargs):
        self.patch = patch if patch is not None else Recorder()
        self.run_log = run_log if run_log is not None else ListLog()
        self.lines = []
        self.core = core = pr.PiReceiver(
            patch_fn=self.patch,
            thing_id=THING,
            run_log=self.run_log,
            clock_ms=make_clock(1000),
            dry_run=dry_run,
            log=self.lines.append,
        )
        self.server = pr.make_server(
            core, TOKEN.encode(), "127.0.0.1", 0, log=self.lines.append, request_timeout=request_timeout,
            **server_kwargs
        )
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True
        )
        self.thread.start()
        self.port = self.server.server_address[1]
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def wait_for(self, predicate, timeout=2.0):
        """The server logs its console line just AFTER writing the reply, so a client that
        has already returned can get here first. Poll instead of assuming an order."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def post(self, path, payload, token=TOKEN, content_type="application/json", extra=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        headers = {"Content-Type": content_type}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        headers.update(extra or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read()
            return resp.status, resp, data
        finally:
            conn.close()

    def raw(self, payload: bytes, read_timeout=4.0):
        """Send raw bytes, return (status_code, header_text, body)."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=read_timeout)
        try:
            sock.sendall(payload)
            chunks = []
            try:
                while True:
                    part = sock.recv(65536)
                    if not part:
                        break
                    chunks.append(part)
            except OSError:
                # timeout, or the server reset the connection after replying without
                # reading a body (WinError 10053/10054): keep whatever arrived
                pass
        finally:
            sock.close()
        data = b"".join(chunks)
        if not data:
            return None, "", b""
        head, _, body = data.partition(b"\r\n\r\n")
        parts = head.split(b" ", 2)
        # garbage input may get a status-line-less (HTTP/0.9 style) error page: status None
        status = int(parts[1]) if parts[0].startswith(b"HTTP/") and len(parts) > 1 else None
        return status, head.decode("latin-1"), body


class TelemetryRoundTripTests(ServerCase):
    def test_telemetry_ok(self):
        self.start()
        status, resp, data = self.post("/v1/telemetry", good_message())
        self.assertEqual(status, 200)
        reply = json.loads(data)
        self.assertEqual(reply["status"], "applied")
        self.assertEqual(reply["ditto"], "ok")
        self.assertEqual(reply["rejected_features"], [])
        self.assertEqual(len(self.patch.calls), 1)
        self.assertEqual(resp.getheader("Content-Type"), "application/json")
        self.assertEqual(resp.getheader("Connection"), "close")

    def test_response_does_not_advertise_python_version(self):
        self.start()
        _, resp, _ = self.post("/v1/telemetry", good_message())
        self.assertNotIn("Python", resp.getheader("Server") or "")

    def test_charset_parameter_and_bearer_case_are_tolerated(self):
        self.start()
        status, _, _ = self.post(
            "/v1/telemetry",
            good_message(),
            content_type="application/json; charset=utf-8",
            extra={"Authorization": "bearer " + TOKEN},
        )
        self.assertEqual(status, 200)

    def test_query_string_does_not_change_routing(self):
        self.start()
        status, _, _ = self.post("/v1/telemetry?x=1", good_message())
        self.assertEqual(status, 200)

    def test_probe_roundtrip(self):
        self.start()
        status, _, data = self.post("/v1/probe", {"v": 1, "t1": 123456})
        self.assertEqual(status, 200)
        reply = json.loads(data)
        self.assertEqual(reply["t1"], 123456)
        self.assertIsInstance(reply["t2"], int)
        self.assertIsInstance(reply["t3"], int)
        self.assertLessEqual(reply["t2"], reply["t3"])

    def test_bad_probe_body_is_400(self):
        self.start()
        status, _, _ = self.post("/v1/probe", {"v": 1})
        self.assertEqual(status, 400)

    def test_concurrent_posts_are_all_applied_once(self):
        self.start()
        results = []

        def one(i):
            msg = good_message(seq=0, session_id="sess%012d" % i)
            results.append(self.post("/v1/telemetry", msg)[0])

        threads = [threading.Thread(target=one, args=(i,)) for i in range(20)]
        [t.start() for t in threads]
        [t.join(10) for t in threads]
        self.assertEqual(results, [200] * 20)
        self.assertEqual(len(self.patch.calls), 20)


class AuthTests(ServerCase):
    def test_missing_header_is_401(self):
        self.start()
        status, resp, _ = self.post("/v1/telemetry", good_message(), token=None)
        self.assertEqual(status, 401)
        self.assertEqual(resp.getheader("WWW-Authenticate"), "Bearer")
        self.assertEqual(self.patch.calls, [])

    def test_wrong_tokens_are_401(self):
        self.start()
        for token in (WRONG_SAME_LEN, TOKEN[:-1], TOKEN + "x", "", "x"):
            with self.subTest(token=len(token)):
                status, _, _ = self.post("/v1/telemetry", good_message(), token=token)
                self.assertEqual(status, 401)
        self.assertEqual(self.patch.calls, [])

    def test_wrong_scheme_is_401(self):
        self.start()
        status, _, _ = self.post(
            "/v1/telemetry", good_message(), token=None, extra={"Authorization": "Basic " + TOKEN}
        )
        self.assertEqual(status, 401)
        status, _, _ = self.post(
            "/v1/telemetry", good_message(), token=None, extra={"Authorization": TOKEN}
        )
        self.assertEqual(status, 401)

    def test_probe_endpoint_also_requires_auth(self):
        self.start()
        status, _, data = self.post("/v1/probe", {"v": 1, "t1": 1}, token=WRONG_SAME_LEN)
        self.assertEqual(status, 401)
        self.assertNotIn(b"t2", data)

    def test_comparison_goes_through_hmac_compare_digest_on_bytes(self):
        self.start()
        with mock.patch.object(pr.hmac, "compare_digest", wraps=hmac.compare_digest) as spy:
            self.post("/v1/telemetry", good_message())
        self.assertTrue(spy.called)
        for call in spy.call_args_list:
            self.assertTrue(all(isinstance(a, bytes) for a in call.args))

    def test_auth_is_checked_before_the_body_is_read(self):
        self.start()
        request = (
            b"POST /v1/telemetry HTTP/1.0\r\nContent-Type: application/json\r\n"
            b"Content-Length: 5000000\r\n\r\n"
        )
        started = time.monotonic()
        status, _, _ = self.raw(request)
        self.assertEqual(status, 401)
        self.assertLess(time.monotonic() - started, 2.0)  # did not wait for 5 MB that never comes


class EarlyRejectionDeliveryTests(ServerCase):
    """A request rejected before its body is read (401/404/415/413...) must still
    deliver the status to a client whose body arrives late. Without draining the
    unread body before closing, the close becomes a TCP reset on Windows
    (WinError 10053) and the client never sees the 401 -- found by a flaky run."""

    def send_with_late_body(self, request_head: bytes, body: bytes, delay=0.25):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=4.0)
        chunks = []
        try:
            sock.sendall(request_head)
            time.sleep(delay)  # the server has replied (and would have closed) by now
            try:
                sock.sendall(body)
            except OSError:
                pass
            try:
                while True:
                    part = sock.recv(65536)
                    if not part:
                        break
                    chunks.append(part)
            except OSError:
                pass
        finally:
            sock.close()
        data = b"".join(chunks)
        if not data.startswith(b"HTTP/"):
            return None
        return int(data.split(b" ", 2)[1])

    def head(self, path=b"/v1/telemetry", token=TOKEN, ctype=b"application/json", length=600):
        lines = [b"POST " + path + b" HTTP/1.0", b"Content-Type: " + ctype, b"Content-Length: %d" % length]
        if token is not None:
            lines.append(b"Authorization: Bearer " + token.encode())
        return b"\r\n".join(lines) + b"\r\n\r\n"

    def test_every_early_rejection_still_reaches_the_client(self):
        self.start(request_timeout=2.0)
        body = b"x" * 600
        cases = {
            "401 wrong token": (self.head(token=WRONG_SAME_LEN), 401),
            "401 no token": (self.head(token=None), 401),
            "404 unknown path": (self.head(path=b"/v1/nope"), 404),
            "415 content type": (self.head(ctype=b"text/plain"), 415),
            "413 too large": (self.head(length=pp.MAX_BODY_BYTES + 1), 413),
        }
        for label, (head, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.send_with_late_body(head, body), expected)
        self.assertEqual(self.patch.calls, [])


class SizeAndFormatTests(ServerCase):
    def auth_headers(self, extra=b""):
        return b"Authorization: Bearer " + TOKEN.encode() + b"\r\n" + extra

    def test_declared_oversize_is_413_without_reading_the_body(self):
        self.start()
        request = (
            b"POST /v1/telemetry HTTP/1.0\r\nContent-Type: application/json\r\n"
            + self.auth_headers(b"Content-Length: %d\r\n" % (pp.MAX_BODY_BYTES + 1))
            + b"\r\n"
        )
        status, _, _ = self.raw(request)
        self.assertEqual(status, 413)
        self.assertEqual(self.patch.calls, [])

    def test_body_exactly_at_the_limit_is_accepted(self):
        self.start()
        msg = good_message()
        msg["pad"] = ""
        base = len(json.dumps(msg).encode())
        msg["pad"] = "x" * (pp.MAX_BODY_BYTES - base)
        body = json.dumps(msg).encode()
        self.assertEqual(len(body), pp.MAX_BODY_BYTES)
        status, _, _ = self.post("/v1/telemetry", body)
        self.assertEqual(status, 200)

    def test_missing_content_length_is_411(self):
        self.start()
        request = b"POST /v1/telemetry HTTP/1.0\r\nContent-Type: application/json\r\n" + self.auth_headers() + b"\r\n"
        self.assertEqual(self.raw(request)[0], 411)

    def test_chunked_is_411(self):
        self.start()
        request = (
            b"POST /v1/telemetry HTTP/1.1\r\nContent-Type: application/json\r\n"
            + self.auth_headers(b"Transfer-Encoding: chunked\r\n")
            + b"\r\n0\r\n\r\n"
        )
        self.assertEqual(self.raw(request)[0], 411)

    def test_garbage_content_length_is_411(self):
        self.start()
        for value in (b"abc", b"-5", b"1.5", b""):
            with self.subTest(value=value):
                request = (
                    b"POST /v1/telemetry HTTP/1.0\r\nContent-Type: application/json\r\n"
                    + self.auth_headers(b"Content-Length: " + value + b"\r\n")
                    + b"\r\n"
                )
                self.assertEqual(self.raw(request)[0], 411)

    def test_wrong_content_type_is_415(self):
        self.start()
        for ctype in ("text/plain", "application/x-www-form-urlencoded", "application/jsonx"):
            with self.subTest(ctype=ctype):
                status, _, _ = self.post("/v1/telemetry", good_message(), content_type=ctype)
                self.assertEqual(status, 415)

    def test_invalid_json_and_bad_schema_are_400_and_do_not_echo_input(self):
        self.start()
        for payload in (b"{not json", b"[]", b'{"v":1}', b'{"v":1,"session_id":"NOPE-SECRET-ECHO"}'):
            with self.subTest(payload=payload):
                status, _, data = self.post("/v1/telemetry", payload)
                self.assertEqual(status, 400)
                self.assertNotIn(b"NOPE-SECRET-ECHO", data)
        self.assertEqual(self.patch.calls, [])

    def test_nan_in_body_is_400(self):
        self.start()
        raw = json.dumps(good_message()).replace("1.24", "NaN").encode()
        self.assertEqual(self.post("/v1/telemetry", raw)[0], 400)

    def test_unknown_path_404_and_other_methods_405(self):
        self.start()
        self.assertEqual(self.post("/v1/nope", good_message())[0], 404)
        for method in ("GET", "PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
                conn.request(method, "/v1/telemetry", headers={"Authorization": "Bearer " + TOKEN})
                resp = conn.getresponse()
                resp.read()
                self.assertEqual(resp.status, 405)
                self.assertEqual(resp.getheader("Allow"), "POST")
                conn.close()

    def test_server_survives_garbage_and_truncated_requests(self):
        self.start()
        self.raw(b"\x00\x01\x02 not http at all\r\n\r\n")
        self.raw(b"POST /v1/telemetry HTTP/1.0\r\n" + b"X: " + b"a" * 70000 + b"\r\n\r\n")
        # declares 100 bytes, sends 5, then goes quiet: the handler must give up
        self.raw(
            b"POST /v1/telemetry HTTP/1.0\r\nContent-Type: application/json\r\n"
            + self.auth_headers(b"Content-Length: 100\r\n")
            + b"\r\n12345",
            read_timeout=2.0,
        )
        status, _, _ = self.post("/v1/telemetry", good_message())
        self.assertEqual(status, 200)


class ConnectionLimitTests(ServerCase):
    """An unauthenticated LAN host must not be able to hold handler threads for long."""

    def test_slow_drip_is_cut_off_at_the_receive_deadline_not_per_byte(self):
        # one byte every 0.2 s keeps resetting a per-recv timeout, so only a TOTAL deadline helps
        self.start(request_timeout=5.0, receive_deadline=0.6)
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        started = time.monotonic()
        closed_after = None
        try:
            sock.sendall(b"POST /v1/telemetry HTTP/1.0\r\n")
            while time.monotonic() - started < 4.0 and closed_after is None:
                try:
                    sock.sendall(b"X")
                    sock.settimeout(0.2)
                    if sock.recv(1) == b"":
                        closed_after = time.monotonic() - started
                except socket.timeout:
                    pass
                except OSError:
                    closed_after = time.monotonic() - started
        finally:
            sock.close()
        self.assertIsNotNone(closed_after, "the connection was never cut off")
        self.assertLess(closed_after, 2.0)

    def test_a_draining_rejection_cannot_hold_its_slot_past_the_deadline(self):
        self.start(request_timeout=5.0, receive_deadline=0.6, max_connections=1)
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        stop = threading.Event()

        def drip():
            try:
                sock.sendall(b"POST /v1/telemetry HTTP/1.0\r\nContent-Type: application/json\r\n"
                             b"Content-Length: 60000\r\n\r\n")  # no token: 401 at once, then drains
                while not stop.is_set():
                    sock.sendall(b"X")
                    time.sleep(0.2)
            except OSError:
                pass

        thread = threading.Thread(target=drip, daemon=True)
        thread.start()
        try:
            time.sleep(1.2)  # well past the 0.6 s deadline while the drip is still going
            ok = self.wait_for(lambda: self._try_post() == 200, timeout=2.0)
        finally:
            stop.set()
            sock.close()
            thread.join(2)
        self.assertTrue(ok, "the only slot stayed occupied by a connection that was draining")

    def _try_post(self):
        try:
            return self.post("/v1/telemetry", good_message())[0]
        except OSError:
            return None

    def test_connections_beyond_the_cap_are_shed_and_slots_come_back(self):
        self.start(request_timeout=5.0, receive_deadline=5.0, max_connections=2)
        idle = [socket.create_connection(("127.0.0.1", self.port)) for _ in range(2)]
        time.sleep(0.3)  # let the server accept and start both handlers
        third = socket.create_connection(("127.0.0.1", self.port), timeout=1.5)
        try:
            try:
                shed = third.recv(10) == b""  # the server closed it at once
            except socket.timeout:
                shed = False                  # left hanging: it was accepted and is waiting for a request
            except OSError:
                shed = True                   # reset by the server also counts as shed
        finally:
            third.close()
        self.assertTrue(shed, "the third connection was accepted instead of being shed")
        for s in idle:
            s.close()
        self.assertTrue(self.wait_for(lambda: self._try_post() == 200, timeout=3.0))


class RobustnessTests(ServerCase):
    def test_five_thousand_digit_content_length_gets_one_clean_reply_and_a_quiet_stderr(self):
        self.start()
        head = b"POST /v1/telemetry HTTP/1.0\r\nContent-Type: application/json\r\nContent-Length: " + b"9" * 5000 + b"\r\n"
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            unauth = self.raw(head + b"\r\n")
            auth = self.raw(head + b"Authorization: Bearer " + TOKEN.encode() + b"\r\n\r\n")
            get = self.raw(b"GET /v1/telemetry HTTP/1.0\r\nContent-Length: " + b"9" * 5000 + b"\r\n\r\n")
        self.assertEqual((unauth[0], auth[0], get[0]), (401, 413, 405))
        for label, response in (("unauth", unauth), ("auth", auth), ("get", get)):
            self.assertNotIn(b"HTTP/1.", response[2], label + ": a second response was appended")
        self.assertEqual(errors.getvalue(), "")

    def test_unexpected_errors_log_the_exception_type_only(self):
        self.start()
        errors = io.StringIO()
        try:
            raise RuntimeError("boom from 203.0.113.9")
        except RuntimeError:
            with contextlib.redirect_stderr(errors):
                self.server.handle_error(None, ("203.0.113.9", 4242))
        self.assertEqual(errors.getvalue(), "")  # socketserver would print a traceback and the client address
        self.assertTrue(any("RuntimeError" in l for l in self.lines))
        self.assertFalse(any("203.0.113.9" in l for l in self.lines))

    def test_a_client_that_goes_away_is_a_warning_not_an_error(self):
        self.start()
        with mock.patch.object(self.core, "handle_telemetry", side_effect=ConnectionAbortedError("gone")):
            status, _, _ = self.raw(
                b"POST /v1/telemetry HTTP/1.0\r\nContent-Type: application/json\r\nAuthorization: Bearer "
                + TOKEN.encode() + b"\r\nContent-Length: %d\r\n\r\n" % len(json.dumps(good_message()).encode())
                + json.dumps(good_message()).encode()
            )
        self.assertTrue(self.wait_for(lambda: any("client" in l.lower() for l in self.lines)))
        self.assertFalse(any(l.startswith("ERROR") for l in self.lines))

    def test_every_other_method_is_405_not_501(self):
        self.start()
        for method in (b"TRACE", b"PROPFIND", b"FOO"):
            with self.subTest(method=method):
                status, head, _ = self.raw(method + b" /v1/telemetry HTTP/1.0\r\n\r\n")
                self.assertEqual(status, 405)
                self.assertIn("Allow: POST", head)

    def test_reply_is_one_tcp_segment_and_nagle_is_off(self):
        # headers and body in two small writes can stall on the peer's delayed ACK (~40 ms on
        # Linux) and that wait would land inside the probe's return leg
        self.start()
        self.assertTrue(self.server.RequestHandlerClass.disable_nagle_algorithm)
        sent = []
        real_send = socket.socket.send

        def spy(sock, data, *args):
            sent.append(bytes(data))
            return real_send(sock, data, *args)

        with mock.patch.object(socket.socket, "send", spy):
            self.post("/v1/telemetry", good_message())
        replies = [d for d in sent if d.startswith(b"HTTP/")]
        self.assertEqual(len(replies), 1)
        self.assertIn(b'"status":"applied"', replies[0])  # status line, headers AND body in one send

    def test_startup_does_no_reverse_dns_lookup(self):
        with mock.patch.object(socket, "getfqdn", side_effect=AssertionError("reverse lookup at bind")):
            self.start()
        self.assertTrue(self.server.server_address)


class LoggingHygieneTests(ServerCase):
    def test_token_authorization_and_client_address_never_reach_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.jsonl"
            log = pr.JsonlRunLog(path)
            self.start(run_log=log)
            self.post("/v1/telemetry", good_message())
            self.post("/v1/telemetry", good_message(seq=2), token=WRONG_SAME_LEN)
            self.post("/v1/telemetry", b"{broken")
            self.post("/v1/probe", {"v": 1, "t1": 5})
            self.raw(b"POST /v1/telemetry HTTP/1.0\r\nAuthorization: Bearer " + TOKEN.encode() + b"\r\n\r\n")
            self.assertTrue(self.wait_for(lambda: len([l for l in self.lines if l.startswith("POST ")]) >= 5))
            log.close()
            file_text = path.read_text(encoding="utf-8")
        everything = "\n".join(self.lines) + file_text
        self.assertTrue(self.lines, "expected some console log lines")
        for secret in (TOKEN, WRONG_SAME_LEN, "Authorization", "Bearer", "127.0.0.1"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, everything)

    def test_console_lines_show_method_path_status_only(self):
        self.start()
        self.post("/v1/telemetry", good_message())
        self.assertTrue(self.wait_for(lambda: any(l.startswith("POST /v1/telemetry -> 200") for l in self.lines)))


def parse_quietly(argv):
    """parse_args, with argparse's usage/error text kept off the test output."""
    with contextlib.redirect_stderr(io.StringIO()):
        return pr.parse_args(argv)


class StartupRuleTests(unittest.TestCase):
    def test_bind_is_required(self):
        with self.assertRaises(SystemExit):
            parse_quietly([])

    def test_defaults_and_flags(self):
        args = parse_quietly(["--bind", "127.0.0.1"])
        self.assertEqual(args.bind, "127.0.0.1")
        self.assertEqual(args.port, 8765)
        self.assertFalse(args.dry_run)
        self.assertEqual(args.ditto_timeout, 3.0)
        args = parse_quietly(["--bind", "10.0.0.5", "--port", "9000", "--dry-run", "--run-log", "x.jsonl"])
        self.assertEqual((args.port, args.dry_run, args.run_log), (9000, True, "x.jsonl"))

    def test_there_is_no_token_argument(self):
        with self.assertRaises(SystemExit):
            parse_quietly(["--bind", "127.0.0.1", "--token", "abc"])

    def test_bad_ports_and_timeouts_are_refused(self):
        for extra in (["--port", "0"], ["--port", "70000"], ["--port", "x"], ["--ditto-timeout", "0"],
                      ["--ditto-timeout", "-1"], ["--ditto-timeout", "soon"]):
            with self.subTest(extra=extra):
                with self.assertRaises(SystemExit):
                    parse_quietly(["--bind", "127.0.0.1"] + extra)

    def test_wildcard_bind_gets_a_warning_specific_bind_does_not(self):
        self.assertIn("all network interfaces", pr.bind_warning("0.0.0.0"))
        self.assertIn("all network interfaces", pr.bind_warning("::"))
        self.assertIsNone(pr.bind_warning("192.0.2.10"))
        self.assertIsNone(pr.bind_warning("127.0.0.1"))

    def test_default_run_log_path_is_under_git_ignored_notes_underscore_dir(self):
        path = pr.default_run_log_path(now_ms=1760214000000)
        self.assertEqual(path.parent.name, "_pi_runs")
        self.assertEqual(path.parent.parent.name, "notes")
        self.assertTrue(path.name.startswith("receiver_20251011T"))
        self.assertTrue(path.name.endswith("Z.jsonl"))


if __name__ == "__main__":
    unittest.main()
