"""Tests for ``HttpCoderTransport`` — the xauditor-side HTTP client.

We use a real ephemeral ``ThreadingHTTPServer`` configured per test, so
each scenario exercises the full `httpx` round-trip rather than mocked
internals. Faster than spawning the actual coder microservice (no
FastAPI / uvicorn import) but still end-to-end on the wire.
"""

from __future__ import annotations

import contextlib
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.coder import HttpCoderTransport
from xauditor.config import CoderConfig
from xauditor.models import (
    CODER_STATUS_FAIL,
    CODER_STATUS_NOT_VERIFIED,
    CODER_STATUS_VERIFIED,
)


class _StubServiceHandler(BaseHTTPRequestHandler):
    """Configurable in-memory coder-service stub."""

    JOBS: dict[str, dict] = {}
    BEHAVIOR: str = "verified"  # "verified" | "not_verified" | "submit_500" | "poll_404" | "submit_no_jobid"
    SEEN_AUTH: list[str | None] = []

    def log_message(self, *_args, **_kwargs) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/verifications":
            self.send_response(404)
            self.end_headers()
            return
        type(self).SEEN_AUTH.append(self.headers.get("Authorization"))
        if self.BEHAVIOR == "submit_500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        idem = body.get("idempotency_key", "anon")
        job_id = f"job-{idem}"
        if self.BEHAVIOR == "verified":
            type(self).JOBS[job_id] = {
                "status": "done",
                "result": {
                    "status": "Verified",
                    "analysis": "ok",
                    "reason": "sanitizer at src/foo.py",
                    "evidence": [
                        {
                            "file_path": "src/foo.py",
                            "function_name": "sanitize",
                            "snippet": "def sanitize(): pass",
                            "language": "python",
                            "role": "sanitizer",
                        }
                    ],
                    "cli_exit_code": 0,
                    "cli_stderr": None,
                    "duration_ms": 5,
                },
            }
        elif self.BEHAVIOR == "not_verified":
            type(self).JOBS[job_id] = {
                "status": "done",
                "result": {
                    "status": "Not Verified",
                    "analysis": "guard exists",
                    "reason": "capability check at src/bar.py",
                    "evidence": [],
                    "cli_exit_code": 0,
                    "cli_stderr": None,
                    "duration_ms": 7,
                },
            }
        elif self.BEHAVIOR == "submit_no_jobid":
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{}')
            return
        body_json = json.dumps({"job_id": job_id, "status_url": f"/verifications/{job_id}"}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_json)))
        self.end_headers()
        self.wfile.write(body_json)

    def do_GET(self) -> None:  # noqa: N802
        if not self.path.startswith("/verifications/"):
            self.send_response(404)
            self.end_headers()
            return
        type(self).SEEN_AUTH.append(self.headers.get("Authorization"))
        job_id = self.path[len("/verifications/"):]
        if self.BEHAVIOR == "poll_404":
            self.send_response(404)
            self.end_headers()
            return
        state = type(self).JOBS.get(job_id)
        if state is None:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(
            {
                "job_id": job_id,
                "status": state["status"],
                "result": state.get("result"),
                "error": state.get("error"),
                "created_at": "2026-04-28T00:00:00+00:00",
                "completed_at": "2026-04-28T00:00:01+00:00",
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextlib.contextmanager
def _serving(behavior: str = "verified"):
    handler_cls = type(
        "_StubInstance",
        (_StubServiceHandler,),
        {"BEHAVIOR": behavior, "JOBS": {}, "SEEN_AUTH": []},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address, handler_cls
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _payload(finding_id: str = "F-0001") -> dict:
    return {
        "finding": {
            "finding_id": finding_id,
            "finding_name": "Command Injection",
        },
        "path": {"call_chain": ["A", "B"]},
    }


class HttpTransportHappyPathTests(unittest.TestCase):
    def test_verified_round_trip(self) -> None:
        with _serving("verified") as ((host, port), _handler):
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                endpoint=f"http://{host}:{port}",
                request_timeout_seconds=5,
                poll_interval_seconds=0.1,
            )
            transport = HttpCoderTransport(cfg, run_id="test-run")
            try:
                result = transport.invoke(_payload())
            finally:
                transport.close()
        self.assertEqual(result.status, CODER_STATUS_VERIFIED)
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.evidence[0].role, "sanitizer")

    def test_not_verified_round_trip(self) -> None:
        with _serving("not_verified") as ((host, port), _handler):
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                endpoint=f"http://{host}:{port}",
                request_timeout_seconds=5,
                poll_interval_seconds=0.1,
            )
            transport = HttpCoderTransport(cfg, run_id="test-run")
            try:
                result = transport.invoke(_payload())
            finally:
                transport.close()
        self.assertEqual(result.status, CODER_STATUS_NOT_VERIFIED)


class HttpTransportAuthHeaderTests(unittest.TestCase):
    def test_authorization_sent_when_enable_auth(self) -> None:
        with _serving("verified") as ((host, port), handler):
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                endpoint=f"http://{host}:{port}",
                enable_auth=True,
                endpoint_token="secret-abc",
                request_timeout_seconds=5,
                poll_interval_seconds=0.1,
            )
            transport = HttpCoderTransport(cfg, run_id="test-run")
            try:
                transport.invoke(_payload())
            finally:
                transport.close()
        self.assertTrue(handler.SEEN_AUTH)
        for header in handler.SEEN_AUTH:
            self.assertEqual(header, "Bearer secret-abc")

    def test_authorization_not_sent_when_enable_auth_false(self) -> None:
        with _serving("verified") as ((host, port), handler):
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                endpoint=f"http://{host}:{port}",
                enable_auth=False,
                endpoint_token="secret-abc",  # configured but ignored
                request_timeout_seconds=5,
                poll_interval_seconds=0.1,
            )
            transport = HttpCoderTransport(cfg, run_id="test-run")
            try:
                transport.invoke(_payload())
            finally:
                transport.close()
        self.assertTrue(handler.SEEN_AUTH)
        for header in handler.SEEN_AUTH:
            self.assertIsNone(header)


class HttpTransportErrorMappingTests(unittest.TestCase):
    def test_submit_500_yields_fail(self) -> None:
        with _serving("submit_500") as ((host, port), _handler):
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                endpoint=f"http://{host}:{port}",
                request_timeout_seconds=5,
                poll_interval_seconds=0.1,
            )
            transport = HttpCoderTransport(cfg, run_id="r")
            try:
                result = transport.invoke(_payload())
            finally:
                transport.close()
        self.assertEqual(result.status, CODER_STATUS_FAIL)
        self.assertIn("HTTP 500", result.reason)

    def test_submit_missing_jobid_yields_fail(self) -> None:
        with _serving("submit_no_jobid") as ((host, port), _handler):
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                endpoint=f"http://{host}:{port}",
                request_timeout_seconds=5,
                poll_interval_seconds=0.1,
            )
            transport = HttpCoderTransport(cfg, run_id="r")
            try:
                result = transport.invoke(_payload())
            finally:
                transport.close()
        self.assertEqual(result.status, CODER_STATUS_FAIL)
        self.assertIn("missing job_id", result.reason)

    def test_poll_404_yields_fail(self) -> None:
        with _serving("poll_404") as ((host, port), _handler):
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                endpoint=f"http://{host}:{port}",
                request_timeout_seconds=5,
                poll_interval_seconds=0.1,
            )
            transport = HttpCoderTransport(cfg, run_id="r")
            try:
                result = transport.invoke(_payload())
            finally:
                transport.close()
        self.assertEqual(result.status, CODER_STATUS_FAIL)
        self.assertIn("job not found", result.reason)

    def test_connect_refused_yields_fail(self) -> None:
        # Bind + close to get a guaranteed-refused address.
        server = ThreadingHTTPServer(("127.0.0.1", 0), _StubServiceHandler)
        host, port = server.server_address
        server.server_close()
        cfg = CoderConfig(
            enabled=True,
            transport="http",
            endpoint=f"http://{host}:{port}",
            request_timeout_seconds=2,
            poll_interval_seconds=0.1,
        )
        transport = HttpCoderTransport(cfg, run_id="r")
        try:
            result = transport.invoke(_payload())
        finally:
            transport.close()
        self.assertEqual(result.status, CODER_STATUS_FAIL)
        self.assertIn("connect failed", result.reason)


if __name__ == "__main__":
    unittest.main()
