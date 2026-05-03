"""Verify ``GET /api/auth/me`` includes the user's role.

Uses ``fastapi.testclient.TestClient`` with ``current_user`` overridden so
the test does not require a live database.
"""

from __future__ import annotations

import unittest
import uuid

from fastapi.testclient import TestClient

from xauditor_portal.app import create_app
from xauditor_portal.auth import current_user
from xauditor_portal.auth.roles import Role
from xauditor_portal.db.models.auth import User


def _user(role: str) -> User:
    return User(
        id=uuid.uuid4(),
        username="alice",
        password_hash="x",
        must_change_password=False,
        role=role,
    )


def _build_client(role: str) -> TestClient:
    app = create_app()
    app.dependency_overrides[current_user] = lambda: _user(role)
    return TestClient(app)


class AuthMeRoleTests(unittest.TestCase):
    def test_me_response_includes_admin_role(self) -> None:
        with _build_client(Role.ADMIN.value) as client:
            response = client.get("/api/auth/me")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["role"], Role.ADMIN.value)
        self.assertEqual(body["username"], "alice")
        self.assertFalse(body["must_change_password"])

    def test_me_response_includes_auditor_role(self) -> None:
        with _build_client(Role.AUDITOR.value) as client:
            response = client.get("/api/auth/me")
        self.assertEqual(response.json()["role"], Role.AUDITOR.value)

    def test_me_response_includes_viewer_role(self) -> None:
        with _build_client(Role.VIEWER.value) as client:
            response = client.get("/api/auth/me")
        self.assertEqual(response.json()["role"], Role.VIEWER.value)


if __name__ == "__main__":
    unittest.main()
