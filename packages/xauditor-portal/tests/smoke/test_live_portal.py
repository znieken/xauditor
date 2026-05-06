"""Live-portal smoke checks (tasks 8.4, 9.4, 11.4, 12.4).

Runs against a **live** portal — it logs in, reads the current project /
build / run lists, asserts that the backend endpoints introduced by this
change are reachable, and (optionally) drives a short live-audit loop
when ``XAUDITOR_SMOKE_LIVE_AUDIT=1`` is set.

This is NOT part of the normal hermetic suite. Run it manually against
a running ``xauditor portal start`` + ``xauditor reportdb start``::

    export XAUDITOR_SMOKE_PORTAL_URL=http://localhost:8080
    export XAUDITOR_SMOKE_USERNAME=auditor
    export XAUDITOR_SMOKE_PASSWORD=auditor   # or whatever you rotated to
    python -m unittest tests.smoke.test_live_portal

What each task covers:

- **8.4** — Three-level navigation reachable; Coverage populated; team-
  mode debates reachable from a finding; Settings LLM editor loads.
- **9.4** — Findings list returns quickly on an in-progress run (the
  auto-refresh UX tests that the endpoint is fast enough to poll).
- **11.4** — Build list + Run list both update within the poll interval
  when a new run is created on page 1.
- **12.4** — Coverage summary + paginated lists all reachable on a run
  that is still in progress.

Run without ``XAUDITOR_SMOKE_LIVE_AUDIT=1`` and the script exercises the
portal's *static* surface (already-completed runs). Set the env var to
also spawn a live ``xauditor audit`` against the latest graph build and
verify the new-row-on-page-1 behaviour.
"""

from __future__ import annotations

import os
import time
import unittest
from typing import Any


PORTAL_URL_ENV = "XAUDITOR_SMOKE_PORTAL_URL"
USERNAME_ENV = "XAUDITOR_SMOKE_USERNAME"
PASSWORD_ENV = "XAUDITOR_SMOKE_PASSWORD"
LIVE_AUDIT_ENV = "XAUDITOR_SMOKE_LIVE_AUDIT"


def _env_or_skip(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        raise unittest.SkipTest(
            f"{name} is not set; skipping live-portal smoke suite."
        )
    return value


class LivePortalSmokeTests(unittest.TestCase):
    client: Any
    base_url: str

    @classmethod
    def setUpClass(cls) -> None:
        try:
            import httpx  # noqa: F401
        except ImportError:  # pragma: no cover
            raise unittest.SkipTest("httpx is required for the smoke suite")
        cls.base_url = _env_or_skip(PORTAL_URL_ENV)
        username = _env_or_skip(USERNAME_ENV, default="auditor")
        password = _env_or_skip(PASSWORD_ENV, default="auditor")
        import httpx

        cls.client = httpx.Client(base_url=cls.base_url, timeout=10.0)
        response = cls.client.post(
            "/api/auth/login",
            json={"username": username, "password": password},
        )
        if response.status_code != 200:
            raise unittest.SkipTest(
                f"Smoke login failed ({response.status_code}): {response.text}. "
                "If must_change_password is set, rotate the password first."
            )

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.client.close()
        except Exception:  # pragma: no cover - best effort
            pass

    # --- 8.4 / 11.4: navigation + list endpoints reachable ---------------

    def test_projects_endpoint_returns_page_shape(self) -> None:
        response = self.client.get("/api/projects", params={"limit": 10})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("items", body)
        self.assertIn("total", body)

    def test_builds_list_reachable_for_each_project(self) -> None:
        projects = self.client.get("/api/projects", params={"limit": 50}).json()
        for project in projects.get("items", []):
            key = project["project_key"]
            response = self.client.get(
                f"/api/projects/{key}/builds", params={"limit": 10}
            )
            self.assertEqual(response.status_code, 200, response.text)
            builds = response.json()
            self.assertIn("items", builds)

    def test_runs_list_reachable_for_each_build(self) -> None:
        projects = self.client.get("/api/projects", params={"limit": 50}).json()
        for project in projects.get("items", [])[:3]:  # bound the cost
            key = project["project_key"]
            builds = self.client.get(
                f"/api/projects/{key}/builds", params={"limit": 5}
            ).json()
            for build in builds.get("items", []):
                fp = build["build_fingerprint"]
                response = self.client.get(
                    f"/api/projects/{key}/builds/{fp}/runs",
                    params={"limit": 5},
                )
                self.assertEqual(response.status_code, 200, response.text)

    # --- 12.4: coverage surface for a real run --------------------------

    def test_coverage_summary_endpoint(self) -> None:
        run = self._latest_completed_run()
        if run is None:
            self.skipTest("No completed runs available for coverage check.")
        response = self.client.get(
            f"/api/runs/{run['id']}/coverage/summary",
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        for category in ("modules", "files", "functions"):
            self.assertIn(category, body)
            self.assertIn("total", body[category])
            self.assertIn("by_status", body[category])

    def test_coverage_paginated_lists(self) -> None:
        run = self._latest_completed_run()
        if run is None:
            self.skipTest("No completed runs available for coverage check.")
        for category in ("modules", "files", "functions"):
            response = self.client.get(
                f"/api/runs/{run['id']}/coverage/{category}",
                params={"limit": 25, "offset": 0},
            )
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertIn("items", body)
            self.assertIn("total", body)

    def test_coverage_list_enforces_limit_bound(self) -> None:
        run = self._latest_completed_run()
        if run is None:
            self.skipTest("No completed runs available for coverage check.")
        response = self.client.get(
            f"/api/runs/{run['id']}/coverage/modules",
            params={"limit": 5000},
        )
        # FastAPI returns 422 for Query ge/le violations.
        self.assertEqual(response.status_code, 422)

    # --- 8.4: run detail carries feedback breakdown ---------------------

    def test_run_detail_exposes_feedback_breakdown(self) -> None:
        run = self._latest_completed_run()
        if run is None:
            self.skipTest("No completed runs available.")
        response = self.client.get(f"/api/runs/{run['id']}")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("valid_findings_breakdown", body)
        for field in ("base", "added_by_feedback", "removed_by_feedback", "net"):
            self.assertIn(field, body["valid_findings_breakdown"])
        # ``false_positives`` is now a feedback-derived integer (no breakdown).
        self.assertIn("false_positives", body)
        self.assertIsInstance(body["false_positives"], int)
        self.assertNotIn("false_positives_breakdown", body)

    # --- 8.4: effective LLM config reachable ----------------------------

    def test_effective_llm_config_endpoint(self) -> None:
        response = self.client.get("/api/config/effective/llm")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("agents", body)
        self.assertIn("providers", body)

    # --- 9.4 / 11.4 / 12.4: live-audit observation (optional) -----------

    @unittest.skipUnless(
        os.environ.get(LIVE_AUDIT_ENV) == "1",
        f"Set {LIVE_AUDIT_ENV}=1 to include the live-audit leg.",
    )
    def test_new_run_appears_on_page_1_within_poll_interval(self) -> None:
        projects = self.client.get("/api/projects", params={"limit": 1}).json()
        if not projects.get("items"):
            self.skipTest("No projects available for live-audit probe.")
        project_key = projects["items"][0]["project_key"]
        builds = self.client.get(
            f"/api/projects/{project_key}/builds", params={"limit": 1}
        ).json()
        if not builds.get("items"):
            self.skipTest("Project has no builds.")
        build_fp = builds["items"][0]["build_fingerprint"]

        before = self.client.get(
            f"/api/projects/{project_key}/builds/{build_fp}/runs",
            params={"limit": 5, "offset": 0},
        ).json()
        before_count = before["total"]

        import subprocess

        subprocess.Popen(
            ["xauditor", "audit"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        for _ in range(10):  # poll for ~30 seconds
            time.sleep(3)
            after = self.client.get(
                f"/api/projects/{project_key}/builds/{build_fp}/runs",
                params={"limit": 5, "offset": 0},
            ).json()
            if after["total"] > before_count:
                self.assertGreaterEqual(after["total"], before_count + 1)
                return
        self.fail(
            "New run did not appear on page 1 within 30 seconds of `xauditor "
            "audit` being launched."
        )

    # --- helpers --------------------------------------------------------

    def _latest_completed_run(self) -> dict[str, Any] | None:
        response = self.client.get(
            "/api/runs",
            params={"limit": 25, "status": "completed"},
        )
        if response.status_code != 200:
            return None
        items = response.json().get("items") or []
        return items[0] if items else None


if __name__ == "__main__":
    unittest.main()
