from __future__ import annotations

import unittest

from xauditor_portal.app import create_app


EXPECTED_PATHS = {
    "/api/health",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/change-password",
    "/api/auth/me",
    "/api/runs",
    "/api/runs/{run_id}",
    "/api/runs/{run_id}/progress",
    "/api/runs/{run_id}/coverage",
    "/api/runs/{run_id}/coverage/summary",
    "/api/runs/{run_id}/coverage/modules",
    "/api/runs/{run_id}/coverage/files",
    "/api/runs/{run_id}/coverage/functions",
    "/api/runs/{run_id}/debates",
    "/api/runs/{run_id}/subagents",
    "/api/runs/{run_id}/findings",
    "/api/runs/{run_id}/findings/{finding_id}/debate",
    "/api/runs/{run_id}/feedback-export",
    "/api/findings/{finding_id}",
    "/api/findings/{finding_id}/feedback",
    "/api/config/effective",
    "/api/config/effective/llm",
    "/api/config/snapshot",
    "/api/config/snapshots",
    "/api/projects",
    "/api/projects/{project_key}/builds",
    "/api/projects/{project_key}/builds/{build_fingerprint}/runs",
    "/api/users",
    "/api/users/{user_id}",
    "/api/users/{user_id}/reset-password",
}


class AppFactoryTests(unittest.TestCase):
    def test_create_app_registers_expected_routes(self) -> None:
        app = create_app()
        paths = {
            getattr(r, "path", None)
            for r in app.routes
            if getattr(r, "methods", None)
        }
        missing = EXPECTED_PATHS - paths
        self.assertFalse(
            missing, f"Expected routes missing from app: {sorted(missing)}"
        )

    def test_openapi_schema_is_served_from_api_prefix(self) -> None:
        app = create_app()
        paths = {getattr(r, "path", None) for r in app.routes}
        self.assertIn("/api/openapi.json", paths)
        self.assertIn("/api/docs", paths)


if __name__ == "__main__":
    unittest.main()
