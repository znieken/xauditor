"""Tests for ``check_coder_http`` and the unified ``check_coder_runtime``.

Uses a real ephemeral ``http.server.ThreadingHTTPServer`` instead of
mocking ``httpx``, so the test exercises the same wire path as the
production code.
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

from xauditor.audit.preflight import (
    check_coder_http,
    check_coder_runtime,
)
from xauditor.config import CoderConfig
from xauditor.errors import PreflightError


class _Handler(BaseHTTPRequestHandler):
    """Configurable per-test handler.

    The class-level ``RESPONSE`` attribute controls behaviour:
      * ``None``  → 200 with a healthy JSON body
      * ``"500"`` → 500 with a JSON-ish body
      * ``"text"`` → 200 with text/plain body (not JSON)
    """

    RESPONSE: str | None = None
    VERSION: str = "claude 0.5.7"

    def log_message(self, *_args, **_kwargs) -> None:  # silence test logs
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        if self.RESPONSE == "500":
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"error"}')
            return
        if self.RESPONSE == "text":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"hello world")
            return
        body = json.dumps(
            {
                "status": "ok",
                "claude_cli_version": self.VERSION,
                "max_concurrent_jobs": 4,
                "in_flight": 2,
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextlib.contextmanager
def _serving(handler_response: str | None = None, version: str = "claude 0.5.7"):
    """Yield ``(host, port)`` for a healthy ephemeral test server."""

    handler_cls = type(
        "_HandlerInstance",
        (_Handler,),
        {"RESPONSE": handler_response, "VERSION": version},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


class CheckCoderHttpDisabledTests(unittest.TestCase):
    def test_disabled_returns_none(self) -> None:
        cfg = CoderConfig(enabled=False, transport="http", endpoint="http://x")
        self.assertIsNone(check_coder_http(cfg))


class CheckCoderHttpSuccessTests(unittest.TestCase):
    def test_health_probe_captures_version(self) -> None:
        with _serving(version="claude 1.2.3") as (host, port):
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                endpoint=f"http://{host}:{port}",
                preflight_timeout_seconds=3,
            )
            result = check_coder_http(cfg)
        assert result is not None
        self.assertEqual(result.cli_command, ("http",))
        self.assertEqual(result.resolved_path, f"http://{host}:{port}")
        self.assertEqual(result.version, "claude 1.2.3")

    def test_unified_dispatcher_routes_to_http(self) -> None:
        with _serving() as (host, port):
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                endpoint=f"http://{host}:{port}",
                preflight_timeout_seconds=3,
            )
            result = check_coder_runtime(cfg)
        assert result is not None
        self.assertEqual(result.cli_command, ("http",))


class CheckCoderHttpFailureTests(unittest.TestCase):
    def test_missing_endpoint_raises(self) -> None:
        cfg = CoderConfig(enabled=True, transport="http", endpoint="")
        with self.assertRaises(PreflightError) as ctx:
            check_coder_http(cfg)
        self.assertIn("coder.endpoint", str(ctx.exception))

    def test_connect_refused_raises_with_actionable_message(self) -> None:
        # Bind a port, close it, then try to probe — guaranteed connection refused.
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        host, port = server.server_address
        server.server_close()
        cfg = CoderConfig(
            enabled=True,
            transport="http",
            endpoint=f"http://{host}:{port}",
            preflight_timeout_seconds=2,
        )
        with self.assertRaises(PreflightError) as ctx:
            check_coder_http(cfg)
        message = str(ctx.exception)
        self.assertIn("coder.endpoint", message)
        self.assertIn(f"http://{host}:{port}", message)
        # Recommends a way out
        self.assertTrue(
            "subprocess" in message or "sidecar" in message.lower(),
            f"expected actionable hint, got: {message}",
        )

    def test_500_response_raises(self) -> None:
        with _serving(handler_response="500") as (host, port):
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                endpoint=f"http://{host}:{port}",
                preflight_timeout_seconds=3,
            )
            with self.assertRaises(PreflightError) as ctx:
                check_coder_http(cfg)
        self.assertIn("HTTP 500", str(ctx.exception))

    def test_non_json_response_raises(self) -> None:
        with _serving(handler_response="text") as (host, port):
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                endpoint=f"http://{host}:{port}",
                preflight_timeout_seconds=3,
            )
            with self.assertRaises(PreflightError) as ctx:
                check_coder_http(cfg)
        self.assertIn("not JSON", str(ctx.exception))


class CheckCoderRuntimeDispatchTests(unittest.TestCase):
    def test_subprocess_transport_uses_cli_check(self) -> None:
        # Subprocess path is already covered by tests/test_coder_preflight.py.
        # Here we just confirm the dispatcher routes to the CLI variant when
        # transport != "http", surfacing the same kind of error.
        cfg = CoderConfig(
            enabled=True,
            transport="subprocess",
            cli_command=("definitely-not-installed-claude",),
        )
        with self.assertRaises(PreflightError):
            check_coder_runtime(cfg, env={"PATH": "/empty"})


if __name__ == "__main__":
    unittest.main()
