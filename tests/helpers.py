"""Shared test helpers (stdlib only)."""
from __future__ import annotations

from pathlib import Path

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "pi3bplus"


def fixture_text(name: str, crlf: bool = False) -> str:
    """Read a copied Pi capture. crlf=True re-encodes line endings the way a
    Windows checkout with core.autocrlf=true would hand them to the tests."""
    text = (FIXTURE_DIR / name).read_bytes().decode("utf-8")
    text = text.replace("\r\n", "\n")
    return text.replace("\n", "\r\n") if crlf else text


def good_message(**overrides):
    msg = {
        "v": 1,
        "session_id": "9f3a1c5e7b2d4a60",
        "seq": 41,
        "sent_at_ms": 1760214000123,
        "ntp_synchronized": True,
        "measurements": {
            "cpu_utilization": {"value": 1.24, "measured_at_ms": 1760214000118, "window_ms": 3004},
            "memory_utilization": {"value": 31.12, "measured_at_ms": 1760214000119},
            "cpu_temperature_c": {"value": 40.78, "measured_at_ms": 1760214000120},
            "cpu_frequency_khz": {"value": 600000, "measured_at_ms": 1760214000121},
        },
        "probes": [{"t1": 1760213990000, "t2": 1760213990105, "t3": 1760213990106, "t4": 1760213990011}],
    }
    msg.update(overrides)
    return msg


# ---------------------------------------------------------------------------
# Loopback-only fake Ditto, for asserting the exact requests the repo's real
# twin/ditto_client.py sends. Nothing here leaves 127.0.0.1.
# ---------------------------------------------------------------------------
import contextlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


class FakeDitto:
    """Records every request. `responder(method, path, body) -> (status, bytes)`
    decides the reply; the default answers 204 like Ditto does for a PATCH."""

    def __init__(self, responder=None, delay: float = 0.0):
        self.requests = []
        self.delay = delay
        self.responder = responder or (lambda method, path, body: (204, b""))
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "FakeDitto"
            sys_version = ""

            def log_message(self, fmt, *args):  # keep test output quiet
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                if outer.delay:
                    time.sleep(outer.delay)
                status, payload = outer.responder(self.command, self.path, body)
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                if payload:
                    self.send_header("Content-Type", "application/json")
                self.end_headers()
                if payload:
                    self.wfile.write(payload)

            do_GET = do_PUT = do_POST = do_PATCH = do_DELETE = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True
        )

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@contextlib.contextmanager
def ditto_pointed_at(fake: "FakeDitto"):
    """Point twin.ditto_client at the fake (both Ditto and Extended API URLs) and
    keep `requests` from routing loopback traffic through any system proxy."""
    from twin import ditto_client

    env = {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
    with mock.patch.dict(os.environ, env), \
            mock.patch.object(ditto_client, "DITTO_HTTP_URL", fake.base_url), \
            mock.patch.object(ditto_client, "EXTENDED_API_URL", fake.base_url):
        yield
