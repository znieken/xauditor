"""End-to-end tests for multi-project routes + project validation.

Covers:
- ``GET /projects`` happy path, dotfile filtering, sort, auth
- ``POST /verifications`` ``project`` field validation
  (allowlist, traversal, missing-subdir, idempotency conflict)
- Per-project ``HOME`` propagation through to the worker call
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient

from xauditor_coder_service.app import ServiceSettings, create_app
from xauditor_coder_service.auth import AuthSettings
from xauditor_coder_service.job import STATUS_DONE, JobStore


def _settings(*, auth: AuthSettings | None = None) -> ServiceSettings:
    return ServiceSettings(
        auth=auth or AuthSettings(enable_auth=False, token=""),
        max_concurrent_jobs=4,
        job_ttl_seconds=3600.0,
        cli_command="claude",
        cli_path="/usr/bin/claude",
        cli_version="claude 0.5.7",
    )


def _payload() -> dict:
    return {
        "idempotency_key": "k1",
        "payload": {"finding": {"finding_id": "F-1"}},
        "claude_args": {},
        "request_timeout_seconds": 5,
    }


class GetProjectsRouteTests(unittest.TestCase):
    def _patch_workspace(self, layout: dict[str, list[str]] | None = None):
        """Build a real tempdir workspace + return its patch context.

        ``layout`` maps relative paths (e.g. ``"team/repo"``) to a list of
        marker names to drop inside (e.g. ``[".git"]``). Empty list = no
        markers (yielded only if the path is depth-1).
        """

        layout = layout or {}
        tmp = tempfile.TemporaryDirectory()
        for rel, markers in layout.items():
            sub = Path(tmp.name) / rel
            sub.mkdir(parents=True, exist_ok=True)
            for marker in markers:
                (sub / marker).touch()
        patch = mock.patch(
            "xauditor_coder_service.app._WORKSPACE_ROOT", new=Path(tmp.name)
        )
        return tmp, patch

    def test_lists_subdirs_sorted(self) -> None:
        tmp, patch = self._patch_workspace(
            {"othertool": [], "secmind": [], "alpha": []}
        )
        try:
            with patch:
                app = create_app(settings=_settings(), job_store=JobStore())
                with TestClient(app) as client:
                    resp = client.get("/projects")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(
                resp.json(), {"projects": ["alpha", "othertool", "secmind"]}
            )
        finally:
            tmp.cleanup()

    def test_filters_dotfile_entries(self) -> None:
        tmp, patch = self._patch_workspace(
            {"secmind": [], ".cache": [], ".git": [], "__pycache__": []}
        )
        try:
            with patch:
                app = create_app(settings=_settings(), job_store=JobStore())
                with TestClient(app) as client:
                    resp = client.get("/projects")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), {"projects": ["secmind"]})
        finally:
            tmp.cleanup()

    def test_workspace_missing_returns_empty_list(self) -> None:
        with mock.patch(
            "xauditor_coder_service.app._WORKSPACE_ROOT",
            new=Path("/does/not/exist"),
        ):
            app = create_app(settings=_settings(), job_store=JobStore())
            with TestClient(app) as client:
                resp = client.get("/projects")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"projects": []})

    def test_auth_enforced_when_enable_auth_true(self) -> None:
        settings = _settings(
            auth=AuthSettings(enable_auth=True, token="secret-abc"),
        )
        tmp, patch = self._patch_workspace({"secmind": []})
        try:
            with patch:
                app = create_app(settings=settings, job_store=JobStore())
                with TestClient(app) as client:
                    missing = client.get("/projects")
                    wrong = client.get(
                        "/projects", headers={"Authorization": "Bearer wrong"}
                    )
                    ok = client.get(
                        "/projects", headers={"Authorization": "Bearer secret-abc"}
                    )
            self.assertEqual(missing.status_code, 401)
            self.assertEqual(wrong.status_code, 401)
            self.assertEqual(ok.status_code, 200)
        finally:
            tmp.cleanup()

    def test_lists_every_directory_at_every_depth(self) -> None:
        tmp, patch = self._patch_workspace(
            {
                "flat": [],
                "team/repo": [],
                "team/another": [],
            }
        )
        try:
            with patch:
                app = create_app(settings=_settings(), job_store=JobStore())
                with TestClient(app) as client:
                    resp = client.get("/projects")
            self.assertEqual(resp.status_code, 200)
            projects = resp.json()["projects"]
            # No marker required — every directory at every depth surfaces.
            self.assertIn("flat", projects)
            self.assertIn("team", projects)
            self.assertIn("team/repo", projects)
            self.assertIn("team/another", projects)
        finally:
            tmp.cleanup()

    def test_deeply_nested_project_surfaces_no_depth_cap(self) -> None:
        tmp, patch = self._patch_workspace({"a/b/c/d/e": []})
        try:
            with patch:
                app = create_app(settings=_settings(), job_store=JobStore())
                with TestClient(app) as client:
                    resp = client.get("/projects")
            self.assertEqual(resp.status_code, 200)
            projects = resp.json()["projects"]
            # No marker required — full chain surfaces.
            for name in ("a", "a/b", "a/b/c", "a/b/c/d", "a/b/c/d/e"):
                self.assertIn(name, projects)
        finally:
            tmp.cleanup()


class PostVerificationsProjectValidationTests(unittest.TestCase):
    def _client_with_real_workspace(self, projects: list[str]):
        """Create an app whose workspace has *real* subdirectories.

        Patches ``_WORKSPACE_ROOT`` so the realpath child-of check passes
        for the listed names.
        """

        tmp = tempfile.TemporaryDirectory()
        for name in projects:
            (Path(tmp.name) / name).mkdir()
        patch = mock.patch(
            "xauditor_coder_service.app._WORKSPACE_ROOT",
            new=Path(tmp.name),
        )
        return tmp, patch

    def test_no_project_field_passes_through_legacy_mode(self) -> None:
        tmp, patch = self._client_with_real_workspace([])
        try:
            with patch:
                app = create_app(settings=_settings(), job_store=JobStore())
                with mock.patch(
                    "xauditor_coder_service.app.run_verification",
                    new=_stub_run_done(),
                ):
                    with TestClient(app) as client:
                        resp = client.post("/verifications", json=_payload())
            self.assertIn(resp.status_code, (200, 201))
        finally:
            tmp.cleanup()

    def test_traversal_project_returns_400(self) -> None:
        tmp, patch = self._client_with_real_workspace(["secmind"])
        try:
            with patch:
                app = create_app(settings=_settings(), job_store=JobStore())
                with TestClient(app) as client:
                    body = {**_payload(), "project": "../etc"}
                    resp = client.post("/verifications", json=body)
            self.assertEqual(resp.status_code, 400)
            detail = resp.json()["detail"]
            self.assertEqual(detail["error"], "project name rejected")
            self.assertEqual(detail["project"], "../etc")
        finally:
            tmp.cleanup()

    def test_disallowed_chars_in_project_returns_400(self) -> None:
        tmp, patch = self._client_with_real_workspace(["secmind"])
        try:
            with patch:
                app = create_app(settings=_settings(), job_store=JobStore())
                with TestClient(app) as client:
                    body = {**_payload(), "project": "foo bar"}
                    resp = client.post("/verifications", json=body)
            self.assertEqual(resp.status_code, 400)
        finally:
            tmp.cleanup()

    def test_nonexistent_project_returns_404(self) -> None:
        tmp, patch = self._client_with_real_workspace(["secmind"])
        try:
            with patch:
                app = create_app(settings=_settings(), job_store=JobStore())
                with TestClient(app) as client:
                    body = {**_payload(), "project": "doesnotexist"}
                    resp = client.post("/verifications", json=body)
            self.assertEqual(resp.status_code, 404)
            detail = resp.json()["detail"]
            self.assertEqual(detail["error"], "project not found")
            self.assertEqual(detail["project"], "doesnotexist")
        finally:
            tmp.cleanup()

    def test_existing_project_admits_worker(self) -> None:
        tmp, patch = self._client_with_real_workspace(["secmind"])
        try:
            with patch, mock.patch(
                "xauditor_coder_service.app.run_verification",
                new=_stub_run_done(),
            ):
                app = create_app(settings=_settings(), job_store=JobStore())
                with TestClient(app) as client:
                    body = {**_payload(), "project": "secmind"}
                    resp = client.post("/verifications", json=body)
            self.assertIn(resp.status_code, (200, 201))
        finally:
            tmp.cleanup()


class IdempotencyKeyAcrossProjectsTests(unittest.TestCase):
    def test_same_key_different_project_returns_409(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            (Path(tmp.name) / "alpha").mkdir()
            (Path(tmp.name) / "beta").mkdir()
            with mock.patch(
                "xauditor_coder_service.app._WORKSPACE_ROOT",
                new=Path(tmp.name),
            ), mock.patch(
                "xauditor_coder_service.app.run_verification",
                new=_stub_run_pending(),
            ):
                app = create_app(settings=_settings(), job_store=JobStore())
                with TestClient(app) as client:
                    first = client.post(
                        "/verifications",
                        json={**_payload(), "project": "alpha"},
                    )
                    self.assertEqual(first.status_code, 201)
                    second = client.post(
                        "/verifications",
                        json={**_payload(), "project": "beta"},
                    )
            self.assertEqual(second.status_code, 409)
            detail = second.json()["detail"]
            self.assertEqual(detail["existing_project"], "alpha")
            self.assertEqual(detail["requested_project"], "beta")
        finally:
            tmp.cleanup()

    def test_same_key_same_project_returns_200(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            (Path(tmp.name) / "alpha").mkdir()
            with mock.patch(
                "xauditor_coder_service.app._WORKSPACE_ROOT",
                new=Path(tmp.name),
            ), mock.patch(
                "xauditor_coder_service.app.run_verification",
                new=_stub_run_done(),
            ):
                app = create_app(settings=_settings(), job_store=JobStore())
                with TestClient(app) as client:
                    first = client.post(
                        "/verifications",
                        json={**_payload(), "project": "alpha"},
                    )
                    second = client.post(
                        "/verifications",
                        json={**_payload(), "project": "alpha"},
                    )
            self.assertEqual(first.status_code, 201)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(first.json()["job_id"], second.json()["job_id"])
        finally:
            tmp.cleanup()


class PerProjectHomePropagationTests(unittest.TestCase):
    def test_home_is_set_per_project(self) -> None:
        captured: dict = {}

        async def _capture_stub(
            job,
            *,
            payload,
            claude_args,
            request_timeout_seconds,
            worker_config,
            job_store,
            cwd=None,
            home=None,
        ):
            captured["cwd"] = cwd
            captured["home"] = home
            job_store.mark_terminal(
                job.job_id,
                status=STATUS_DONE,
                result={
                    "status": "Verified",
                    "analysis": "ok",
                    "reason": "ok",
                    "evidence": [],
                    "cli_exit_code": 0,
                    "cli_stderr": None,
                    "duration_ms": 1,
                },
            )

        tmp = tempfile.TemporaryDirectory()
        try:
            (Path(tmp.name) / "secmind").mkdir()
            with mock.patch(
                "xauditor_coder_service.app._WORKSPACE_ROOT",
                new=Path(tmp.name),
            ), mock.patch(
                "xauditor_coder_service.app._HOME_ROOT",
                new=Path(tmp.name) / "_home",
            ), mock.patch(
                "xauditor_coder_service.app.run_verification",
                new=_capture_stub,
            ):
                app = create_app(settings=_settings(), job_store=JobStore())
                with TestClient(app) as client:
                    body = {**_payload(), "project": "secmind"}
                    resp = client.post("/verifications", json=body)
                    job_id = resp.json()["job_id"]
                    # Drive the worker to completion via polling.
                    for _ in range(20):
                        poll = client.get(f"/verifications/{job_id}")
                        if poll.json()["status"] != "pending":
                            break
            self.assertEqual(captured["cwd"], str(Path(tmp.name) / "secmind"))
            self.assertEqual(
                captured["home"], str(Path(tmp.name) / "_home" / "secmind")
            )
            # First-use mkdir 0700 already created the directory.
            home_path = Path(captured["home"])
            self.assertTrue(home_path.is_dir())
        finally:
            tmp.cleanup()


def _stub_run_done():
    async def _stub(
        job,
        *,
        payload,
        claude_args,
        request_timeout_seconds,
        worker_config,
        job_store,
        cwd=None,
        home=None,
    ):
        job_store.mark_terminal(
            job.job_id,
            status=STATUS_DONE,
            result={
                "status": "Verified",
                "analysis": "ok",
                "reason": "ok",
                "evidence": [],
                "cli_exit_code": 0,
                "cli_stderr": None,
                "duration_ms": 1,
            },
        )

    return _stub


def _stub_run_pending():
    async def _stub(
        job,
        *,
        payload,
        claude_args,
        request_timeout_seconds,
        worker_config,
        job_store,
        cwd=None,
        home=None,
    ):
        # Leave the job pending so a duplicate idempotency_key collides.
        try:
            await __import__("asyncio").wait_for(
                job.cancel_event.wait(), timeout=10
            )
        except Exception:  # noqa: BLE001
            pass

    return _stub


class NestedProjectValidationTests(unittest.TestCase):
    """Validator-only checks for the expanded path-component grammar."""

    def _client(self, layout: dict[str, list[str]]):
        tmp = tempfile.TemporaryDirectory()
        for rel, markers in layout.items():
            sub = Path(tmp.name) / rel
            sub.mkdir(parents=True, exist_ok=True)
            for marker in markers:
                (sub / marker).touch()
        patch = mock.patch(
            "xauditor_coder_service.app._WORKSPACE_ROOT", new=Path(tmp.name)
        )
        return tmp, patch

    def _post_with_project(self, layout, project: str):
        tmp, patch = self._client(layout)
        try:
            with patch, mock.patch(
                "xauditor_coder_service.app.run_verification",
                new=_stub_run_done(),
            ):
                app = create_app(settings=_settings(), job_store=JobStore())
                with TestClient(app) as client:
                    body = {**_payload(), "project": project}
                    resp = client.post("/verifications", json=body)
                    return resp
        finally:
            tmp.cleanup()

    def test_nested_project_happy_path(self) -> None:
        resp = self._post_with_project(
            {"team/repo": [".git"]}, project="team/repo"
        )
        self.assertIn(resp.status_code, (200, 201))

    def test_traversal_segment_in_nested_name_returns_400(self) -> None:
        resp = self._post_with_project(
            {"team/repo": [".git"]}, project="team/../etc"
        )
        self.assertEqual(resp.status_code, 400)

    def test_double_slash_returns_400(self) -> None:
        resp = self._post_with_project(
            {"team/repo": [".git"]}, project="team//repo"
        )
        self.assertEqual(resp.status_code, 400)

    def test_leading_slash_returns_400(self) -> None:
        resp = self._post_with_project(
            {"team/repo": [".git"]}, project="/team/repo"
        )
        self.assertEqual(resp.status_code, 400)

    def test_trailing_slash_returns_400(self) -> None:
        resp = self._post_with_project(
            {"team/repo": [".git"]}, project="team/repo/"
        )
        self.assertEqual(resp.status_code, 400)

    def test_parent_segment_alone_returns_400(self) -> None:
        resp = self._post_with_project(
            {"team/repo": [".git"]}, project="team/.."
        )
        self.assertEqual(resp.status_code, 400)

    def test_disallowed_chars_in_component_returns_400(self) -> None:
        resp = self._post_with_project(
            {"team/repo": [".git"]}, project="team/sp ace/repo"
        )
        self.assertEqual(resp.status_code, 400)

    def test_oversized_name_returns_400(self) -> None:
        long_name = "a/" * 300 + "tail"
        resp = self._post_with_project({"team/repo": [".git"]}, project=long_name)
        self.assertEqual(resp.status_code, 400)


class NestedProjectHomePropagationTests(unittest.TestCase):
    """End-to-end: a nested project name reaches the worker as nested cwd / HOME."""

    def test_nested_cwd_and_home(self) -> None:
        captured: dict = {}

        async def _capture_stub(
            job,
            *,
            payload,
            claude_args,
            request_timeout_seconds,
            worker_config,
            job_store,
            cwd=None,
            home=None,
        ):
            captured["cwd"] = cwd
            captured["home"] = home
            job_store.mark_terminal(
                job.job_id,
                status=STATUS_DONE,
                result={
                    "status": "Verified",
                    "analysis": "ok",
                    "reason": "ok",
                    "evidence": [],
                    "cli_exit_code": 0,
                    "cli_stderr": None,
                    "duration_ms": 1,
                },
            )

        tmp = tempfile.TemporaryDirectory()
        try:
            (Path(tmp.name) / "team" / "repo" / ".git").mkdir(parents=True)
            with mock.patch(
                "xauditor_coder_service.app._WORKSPACE_ROOT",
                new=Path(tmp.name),
            ), mock.patch(
                "xauditor_coder_service.app._HOME_ROOT",
                new=Path(tmp.name) / "_home",
            ), mock.patch(
                "xauditor_coder_service.app.run_verification",
                new=_capture_stub,
            ):
                app = create_app(settings=_settings(), job_store=JobStore())
                with TestClient(app) as client:
                    body = {**_payload(), "project": "team/repo"}
                    resp = client.post("/verifications", json=body)
                    job_id = resp.json()["job_id"]
                    for _ in range(20):
                        poll = client.get(f"/verifications/{job_id}")
                        if poll.json()["status"] != "pending":
                            break
            self.assertEqual(
                captured["cwd"], str(Path(tmp.name) / "team" / "repo")
            )
            self.assertEqual(
                captured["home"],
                str(Path(tmp.name) / "_home" / "team" / "repo"),
            )
            home_path = Path(captured["home"])
            self.assertTrue(home_path.is_dir())
            self.assertTrue(home_path.parent.is_dir())  # parent created via parents=True
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
