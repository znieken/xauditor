"""Multi-project coder service: resolver + /projects probe + payload tests.

Covers the new code paths added by the ``multi-project-coder-service``
change without touching live containers — uses a stub ``ThreadingHTTPServer``
for the HTTP probes and synthetic ``CoderConfig`` instances for the
config-side resolver. These tests would otherwise live in `test_coder_http_preflight.py`
and `test_coder_http_transport.py` but are split out for grep-ability while
the multi-project surface area is still settling.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.coder import (
    HttpCoderTransport,
    _classify_transport_error,
    _log_coder_transport_failure,
)
from xauditor.audit.preflight import (
    check_coder_projects,
    resolve_coder_project,
)
from xauditor.config import CoderConfig, _resolve_coder_workspace
from xauditor.errors import PreflightError
from xauditor.models import CODER_STATUS_FAIL


# --------------------------------------------------------------------------
# resolve_coder_project
# --------------------------------------------------------------------------


class ResolveCoderProjectTests(unittest.TestCase):
    def test_subprocess_transport_returns_none(self) -> None:
        cfg = CoderConfig(enabled=True, transport="subprocess")
        self.assertIsNone(
            resolve_coder_project(cfg, repo_root=Path("/tmp/secmind"))
        )

    def test_disabled_returns_none(self) -> None:
        cfg = CoderConfig(enabled=False, transport="http")
        self.assertIsNone(
            resolve_coder_project(cfg, repo_root=Path("/tmp/secmind"))
        )

    def test_explicit_project_name_wins(self) -> None:
        cfg = CoderConfig(
            enabled=True,
            transport="http",
            workspace_root="/tmp/ws",
            project_name="secmind",
            effective_workspace_root="/tmp/ws",
            effective_project_name="secmind",
        )
        # repo_root basename is different — override SHALL still win.
        self.assertEqual(
            resolve_coder_project(cfg, repo_root=Path("/tmp/worktrees/secmind-feat-x")),
            "secmind",
        )

    def test_basename_fallback_when_project_unset(self) -> None:
        cfg = CoderConfig(
            enabled=True,
            transport="http",
            workspace_root="/tmp/ws",
            effective_workspace_root="/tmp/ws",
        )
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "secmind"
            project_dir.mkdir()
            self.assertEqual(
                resolve_coder_project(cfg, repo_root=project_dir),
                "secmind",
            )

    def test_realpath_resolution_through_symlink(self) -> None:
        cfg = CoderConfig(
            enabled=True,
            transport="http",
            workspace_root="/tmp/ws",
            effective_workspace_root="/tmp/ws",
        )
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "real-secmind"
            target.mkdir()
            link = Path(tmp) / "alias"
            link.symlink_to(target)
            self.assertEqual(
                resolve_coder_project(cfg, repo_root=link),
                "real-secmind",
            )

    def test_relative_path_when_repo_root_inside_workspace_root(self) -> None:
        """Nested repo under workspace_root yields the `/`-joined rel path."""

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            project_dir = workspace / "team" / "repo"
            project_dir.mkdir(parents=True)
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                workspace_root=str(workspace),
                effective_workspace_root=str(workspace),
            )
            self.assertEqual(
                resolve_coder_project(cfg, repo_root=project_dir),
                "team/repo",
            )

    def test_deeply_nested_repo_root_yields_full_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            project_dir = workspace / "org" / "team" / "sub" / "repo"
            project_dir.mkdir(parents=True)
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                workspace_root=str(workspace),
                effective_workspace_root=str(workspace),
            )
            self.assertEqual(
                resolve_coder_project(cfg, repo_root=project_dir),
                "org/team/sub/repo",
            )

    def test_repo_root_equal_to_workspace_root_falls_back_to_basename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "myworkspace"
            workspace.mkdir()
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                workspace_root=str(workspace),
                effective_workspace_root=str(workspace),
            )
            # repo_root == workspace_root: relative path would be ".";
            # fall back to basename so the preflight error is intelligible.
            self.assertEqual(
                resolve_coder_project(cfg, repo_root=workspace),
                "myworkspace",
            )


# --------------------------------------------------------------------------
# Deprecation shim — _resolve_coder_workspace
# --------------------------------------------------------------------------


class ResolveCoderWorkspaceShimTests(unittest.TestCase):
    def test_workspace_only_passes_through(self) -> None:
        ws, name = _resolve_coder_workspace(
            workspace_root="/tmp/ws",
            project_name="secmind",
            repo_mount_path="",
        )
        self.assertEqual(ws, "/tmp/ws")
        self.assertEqual(name, "secmind")

    def test_repo_mount_path_only_drives_shim(self) -> None:
        ws, name = _resolve_coder_workspace(
            workspace_root="",
            project_name="",
            repo_mount_path="/home/user/git/secmind",
        )
        self.assertEqual(ws, "/home/user/git")
        self.assertEqual(name, "secmind")

    def test_both_set_prefers_workspace_root(self) -> None:
        ws, name = _resolve_coder_workspace(
            workspace_root="/tmp/ws",
            project_name="",
            repo_mount_path="/home/user/git/secmind",
        )
        self.assertEqual(ws, "/tmp/ws")
        # project_name unset → audit-startup will derive; shim doesn't
        # populate it from repo_mount_path when workspace_root is the
        # source of truth.
        self.assertEqual(name, "")

    def test_neither_set_returns_empty(self) -> None:
        ws, name = _resolve_coder_workspace(
            workspace_root="",
            project_name="",
            repo_mount_path="",
        )
        self.assertEqual((ws, name), ("", ""))


# --------------------------------------------------------------------------
# check_coder_projects
# --------------------------------------------------------------------------


class _ProjectsHandler(BaseHTTPRequestHandler):
    BEHAVIOR: str = "ok"
    AVAILABLE: list[str] = ["secmind", "othertool"]
    SEEN_AUTH: list[str | None] = []

    def log_message(self, *_args, **_kwargs) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/projects":
            self.send_response(404)
            self.end_headers()
            return
        type(self).SEEN_AUTH.append(self.headers.get("Authorization"))
        if self.BEHAVIOR == "ok":
            body = json.dumps({"projects": list(self.AVAILABLE)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.BEHAVIOR == "401":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"unauthorized")
            return
        if self.BEHAVIOR == "500":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"boom")
            return
        if self.BEHAVIOR == "malformed":
            body = b'{"not_projects": "x"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return


def _serve_projects(behavior: str = "ok"):
    handler = type(
        "_ProjectsInstance",
        (_ProjectsHandler,),
        {"BEHAVIOR": behavior, "SEEN_AUTH": [], "AVAILABLE": ["secmind", "othertool"]},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, handler


class CheckCoderProjectsTests(unittest.TestCase):
    def _config(self, port: int) -> CoderConfig:
        return CoderConfig(
            enabled=True,
            transport="http",
            endpoint=f"http://127.0.0.1:{port}",
            preflight_timeout_seconds=2,
            workspace_root="/tmp/ws",
            effective_workspace_root="/tmp/ws",
        )

    def test_skipped_when_workspace_root_unset(self) -> None:
        # Legacy single-repo deployment: workspace_root empty →
        # check_coder_projects is a no-op even if endpoint is broken.
        cfg = CoderConfig(
            enabled=True,
            transport="http",
            endpoint="http://127.0.0.1:1",
            workspace_root="",
            effective_workspace_root="",
        )
        check_coder_projects(cfg, project="anything")

    def test_skipped_when_project_path_resolves_locally(self) -> None:
        """Local-FS trust check: when ``<workspace_root>/<project>`` is a
        real directory under workspace_root on the audit host, skip the
        ``/projects`` listing probe entirely.

        Regression test for an HGFS-bound user whose `/projects` walk
        truncated before reaching deep paths
        (``FortiAIGate/license/src``), causing pre-flight to fail even
        though the directory existed under
        ``coder.workspace_root=/home/znie/gitlab``. The walk's listing
        is for diagnostics; membership can be confirmed locally.
        """

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            project_dir = workspace / "FortiAIGate" / "license" / "src"
            project_dir.mkdir(parents=True)
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                # Endpoint is intentionally unreachable; trust check
                # SHALL fire BEFORE any HTTP attempt.
                endpoint="http://127.0.0.1:1",
                preflight_timeout_seconds=1,
                workspace_root=str(workspace),
                effective_workspace_root=str(workspace),
            )
            check_coder_projects(cfg, project="FortiAIGate/license/src")

    def test_falls_back_to_http_probe_when_local_path_missing(self) -> None:
        """If the project path is NOT visible on the audit host's FS
        (e.g., remote coder deployment), the HTTP probe still fires and
        validates membership the old way."""

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            # No FortiAIGate subdir under the workspace.
            server, thread, _h = _serve_projects("ok")
            try:
                cfg = CoderConfig(
                    enabled=True,
                    transport="http",
                    endpoint=f"http://127.0.0.1:{server.server_address[1]}",
                    preflight_timeout_seconds=2,
                    workspace_root=str(workspace),
                    effective_workspace_root=str(workspace),
                )
                with self.assertRaises(PreflightError):
                    check_coder_projects(cfg, project="FortiAIGate")
            finally:
                server.shutdown()
                thread.join(timeout=2)
                server.server_close()

    def test_skipped_when_subprocess_transport(self) -> None:
        cfg = CoderConfig(
            enabled=True,
            transport="subprocess",
            workspace_root="/tmp/ws",
            effective_workspace_root="/tmp/ws",
        )
        check_coder_projects(cfg, project="anything")

    def test_resolved_project_present_passes(self) -> None:
        server, thread, _h = _serve_projects("ok")
        try:
            cfg = self._config(server.server_address[1])
            check_coder_projects(cfg, project="secmind")
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_resolved_project_missing_raises_preflight_error(self) -> None:
        server, thread, _h = _serve_projects("ok")
        try:
            cfg = self._config(server.server_address[1])
            with self.assertRaises(PreflightError) as ctx:
                check_coder_projects(cfg, project="newproj")
            msg = str(ctx.exception)
            self.assertIn('"newproj"', msg)
            self.assertIn("secmind", msg)
            self.assertIn("othertool", msg)
            self.assertIn("coder.project_name", msg)
            self.assertIn("mkdir", msg)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_401_maps_to_auth_preflight_error(self) -> None:
        server, thread, _h = _serve_projects("401")
        try:
            cfg = self._config(server.server_address[1])
            with self.assertRaises(PreflightError) as ctx:
                check_coder_projects(cfg, project="secmind")
            self.assertIn("auth-gated", str(ctx.exception))
            self.assertIn("coder.endpoint_token", str(ctx.exception))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_500_maps_to_generic_preflight_error(self) -> None:
        server, thread, _h = _serve_projects("500")
        try:
            cfg = self._config(server.server_address[1])
            with self.assertRaises(PreflightError) as ctx:
                check_coder_projects(cfg, project="secmind")
            self.assertIn("HTTP 500", str(ctx.exception))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_malformed_body_raises_preflight_error(self) -> None:
        server, thread, _h = _serve_projects("malformed")
        try:
            cfg = self._config(server.server_address[1])
            with self.assertRaises(PreflightError) as ctx:
                check_coder_projects(cfg, project="secmind")
            self.assertIn("`projects` array", str(ctx.exception))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


# --------------------------------------------------------------------------
# HttpCoderTransport: project field in body + new error codes
# --------------------------------------------------------------------------


class _RecordingHandler(BaseHTTPRequestHandler):
    LAST_BODY: dict | None = None
    RESPONSE: tuple[int, bytes] = (200, b'{"job_id": "j1", "status_url": "/verifications/j1"}')

    def log_message(self, *_args, **_kwargs) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        try:
            type(self).LAST_BODY = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, json.JSONDecodeError):
            type(self).LAST_BODY = {}
        code, payload = self.RESPONSE
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        # Job is already done — happy path.
        body = json.dumps(
            {
                "job_id": "j1",
                "status": "done",
                "result": {
                    "status": "Verified",
                    "analysis": "ok",
                    "reason": "ok",
                    "evidence": [],
                },
                "created_at": "2026-05-01T00:00:00+00:00",
                "completed_at": "2026-05-01T00:00:01+00:00",
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve_recording(response: tuple[int, bytes] = (201, b'{"job_id": "j1", "status_url": "/verifications/j1"}')):
    handler = type(
        "_RecordingInstance",
        (_RecordingHandler,),
        {"LAST_BODY": None, "RESPONSE": response},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, handler


class HttpTransportProjectFieldTests(unittest.TestCase):
    def test_project_included_in_post_body(self) -> None:
        server, thread, handler = _serve_recording()
        try:
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                endpoint=f"http://{server.server_address[0]}:{server.server_address[1]}",
                request_timeout_seconds=5,
                poll_interval_seconds=0.1,
                effective_project_name="secmind",
            )
            transport = HttpCoderTransport(cfg, run_id="r")
            try:
                transport.invoke({"finding": {"finding_id": "F-1"}})
            finally:
                transport.close()
            self.assertEqual(handler.LAST_BODY.get("project"), "secmind")
            self.assertEqual(handler.LAST_BODY.get("idempotency_key"), "r::F-1")
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_project_empty_string_when_unset(self) -> None:
        server, thread, handler = _serve_recording()
        try:
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                endpoint=f"http://{server.server_address[0]}:{server.server_address[1]}",
                request_timeout_seconds=5,
                poll_interval_seconds=0.1,
            )
            transport = HttpCoderTransport(cfg, run_id="r")
            try:
                transport.invoke({"finding": {"finding_id": "F-1"}})
            finally:
                transport.close()
            self.assertEqual(handler.LAST_BODY.get("project"), "")
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_post_400_yields_fail_with_categorical_reason(self) -> None:
        server, thread, _h = _serve_recording(
            (400, b'{"error": "project name rejected", "project": "../etc"}')
        )
        try:
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                endpoint=f"http://127.0.0.1:{server.server_address[1]}",
                request_timeout_seconds=2,
                poll_interval_seconds=0.1,
                effective_project_name="../etc",
            )
            transport = HttpCoderTransport(cfg, run_id="r")
            try:
                result = transport.invoke({"finding": {"finding_id": "F-1"}})
            finally:
                transport.close()
            self.assertEqual(result.status, CODER_STATUS_FAIL)
            self.assertIn("project name rejected", result.reason)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_post_404_yields_fail_with_project_no_longer_available(self) -> None:
        server, thread, _h = _serve_recording(
            (404, b'{"error": "project not found", "project": "secmind"}')
        )
        try:
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                endpoint=f"http://127.0.0.1:{server.server_address[1]}",
                request_timeout_seconds=2,
                poll_interval_seconds=0.1,
                effective_project_name="secmind",
            )
            transport = HttpCoderTransport(cfg, run_id="r")
            try:
                result = transport.invoke({"finding": {"finding_id": "F-1"}})
            finally:
                transport.close()
            self.assertEqual(result.status, CODER_STATUS_FAIL)
            self.assertEqual(result.reason, "project no longer available")
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_post_409_yields_fail_with_idempotency_conflict(self) -> None:
        server, thread, _h = _serve_recording(
            (409, b'{"error": "idempotency_key bound to a different project"}')
        )
        try:
            cfg = CoderConfig(
                enabled=True,
                transport="http",
                endpoint=f"http://127.0.0.1:{server.server_address[1]}",
                request_timeout_seconds=2,
                poll_interval_seconds=0.1,
                effective_project_name="secmind",
            )
            transport = HttpCoderTransport(cfg, run_id="r")
            try:
                result = transport.invoke({"finding": {"finding_id": "F-1"}})
            finally:
                transport.close()
            self.assertEqual(result.status, CODER_STATUS_FAIL)
            self.assertIn("idempotency conflict", result.reason)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


# --------------------------------------------------------------------------
# _classify_transport_error
# --------------------------------------------------------------------------


class ClassifyTransportErrorTests(unittest.TestCase):
    def test_known_categories(self) -> None:
        cases = {
            "transport error: connect failed (foo)": "connection_refused",
            "transport error: submit timed out (foo)": "timeout",
            "transport error: poll timed out (foo)": "timeout",
            "transport error: timed out after 5s": "timeout",
            "project no longer available": "project_not_found",
            "transport error: idempotency conflict (HTTP 409: ...)": "idempotency_conflict",
            "transport error: submit HTTP 401: nope": "auth",
            "transport error: poll HTTP 403: nope": "auth",
            "transport error: submit HTTP 503: oops": "http_5xx",
            "transport error: poll HTTP 502: oops": "http_5xx",
            "transport error: submit HTTP 400: bad": "http_4xx",
            "transport error: poll HTTP 404: nope": "http_4xx",
            "transport error: job not found (service likely restarted)": "http_4xx",
            "transport error: submit response not JSON": "parse_error",
            "transport error: invalid JSON": "parse_error",
            "transport error: malformed JSON": "parse_error",
            "transport error: no JSON object in stdout": "parse_error",
            "transport error: empty stdout": "parse_error",
            "transport error: cli not found (foo)": "subprocess_exit",
            "transport error: spawn failed (foo)": "subprocess_exit",
            "transport error: exit 7": "subprocess_exit",
        }
        for reason, expected in cases.items():
            self.assertEqual(_classify_transport_error(reason), expected, reason)

    def test_unknown_falls_back_to_subprocess_crashed(self) -> None:
        self.assertEqual(
            _classify_transport_error("something unexpected"),
            "subprocess_crashed",
        )


# --------------------------------------------------------------------------
# _log_coder_transport_failure
# --------------------------------------------------------------------------


class _CapturingLogger:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict]] = []

    def error_kv(self, message: str, **details: object) -> None:
        self.entries.append((message, dict(details)))


class LogCoderTransportFailureTests(unittest.TestCase):
    def test_emits_one_structured_entry(self) -> None:
        logger = _CapturingLogger()
        _log_coder_transport_failure(
            logger=logger,
            run_id="20260501-010203",
            finding_id="F-0042",
            project="secmind",
            endpoint="http://127.0.0.1:8090",
            error_kind="http_5xx",
            error_detail="503 Service Unavailable",
        )
        self.assertEqual(len(logger.entries), 1)
        msg, fields = logger.entries[0]
        self.assertEqual(msg, "coder.transport_failure")
        self.assertEqual(fields["run_id"], "20260501-010203")
        self.assertEqual(fields["finding_id"], "F-0042")
        self.assertEqual(fields["project"], "secmind")
        self.assertEqual(fields["endpoint"], "http://127.0.0.1:8090")
        self.assertEqual(fields["error_kind"], "http_5xx")
        self.assertEqual(fields["error_detail"], "503 Service Unavailable")

    def test_empty_fields_become_dash_literal(self) -> None:
        logger = _CapturingLogger()
        _log_coder_transport_failure(
            logger=logger,
            run_id="",
            finding_id="",
            project="",
            endpoint="",
            error_kind="subprocess_crashed",
            error_detail="OOM",
        )
        _msg, fields = logger.entries[0]
        self.assertEqual(fields["run_id"], "-")
        self.assertEqual(fields["finding_id"], "-")
        self.assertEqual(fields["project"], "-")
        self.assertEqual(fields["endpoint"], "-")

    def test_long_detail_truncated(self) -> None:
        logger = _CapturingLogger()
        _log_coder_transport_failure(
            logger=logger,
            run_id="r",
            finding_id="f",
            project="p",
            endpoint="e",
            error_kind="parse_error",
            error_detail="x" * 500,
        )
        _msg, fields = logger.entries[0]
        self.assertLessEqual(len(fields["error_detail"]), 200)
        self.assertTrue(fields["error_detail"].endswith("..."))

    def test_none_logger_is_no_op(self) -> None:
        # Should not raise.
        _log_coder_transport_failure(
            logger=None,
            run_id="r",
            finding_id="f",
            project="p",
            endpoint="e",
            error_kind="timeout",
            error_detail="...",
        )


if __name__ == "__main__":
    unittest.main()
