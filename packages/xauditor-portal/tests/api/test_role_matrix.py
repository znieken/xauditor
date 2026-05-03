"""Role-matrix coverage: every gated endpoint, every role, expected status.

This is a contract-level test. Each role is exercised against every
endpoint that declares a role-aware dependency, and the expected status
code is asserted. The matrix is one place so adding a new role-gated
endpoint forces the maintainer to think about every role.

Endpoints that read data succeed for every authenticated, password-
rotated role. Endpoints that write feedback succeed for admin and
auditor and 403 for viewer. Endpoints that manage users succeed for
admin only and 403 for everyone else.

The test stubs DB work — the goal is the role gate, not the SQL path.
"""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from xauditor_portal.app import create_app
from xauditor_portal.auth import current_user, get_session
from xauditor_portal.auth.roles import Role
from xauditor_portal.db.models.auth import User


def _user(role: str) -> User:
    user = User(
        id=uuid.uuid4(),
        username="actor",
        password_hash="x",
        must_change_password=False,
        role=role,
    )
    user.created_at = datetime.now(timezone.utc)
    user.updated_at = user.created_at
    return user


def _client(role: str | None) -> TestClient:
    app = create_app()
    if role is not None:
        app.dependency_overrides[current_user] = lambda: _user(role)

    # Build a session whose `.execute()` returns a result whose `.scalars()`
    # / `.all()` return empty lists. This lets handlers progress past the
    # role gate even though we are not exercising the SQL path here.
    empty_scalars = MagicMock()
    empty_scalars.all = MagicMock(return_value=[])
    empty_scalars.first = MagicMock(return_value=None)
    empty_scalars.one_or_none = MagicMock(return_value=None)
    empty_scalars.scalar_one_or_none = MagicMock(return_value=None)
    empty_result = MagicMock()
    empty_result.scalars = MagicMock(return_value=empty_scalars)
    empty_result.all = MagicMock(return_value=[])

    session = MagicMock()
    session.execute = AsyncMock(return_value=empty_result)
    session.scalar = AsyncMock(return_value=None)
    session.get = AsyncMock(return_value=None)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.delete = AsyncMock()
    session.flush = AsyncMock()
    session.rollback = AsyncMock()

    async def _provider() -> Any:
        yield session

    app.dependency_overrides[get_session] = _provider
    # Don't bubble 500s — for this contract test we only care about
    # whether the role gate let the call through.
    return TestClient(app, raise_server_exceptions=False)


# Endpoints under test, with the body/query needed to reach the gate.
# We assert the *gate* status; we don't care whether the handler later
# returns 404 / 422 / 200 once the role is satisfied — that means the
# gate let the call through.

_ADMIN_ONLY = [
    ("GET", "/api/users", None),
    ("POST", "/api/users", {
        "username": "newcomer",
        "initial_password": "AbcDef123456",
        "role": "viewer",
    }),
    ("PATCH", f"/api/users/{uuid.uuid4()}", {"role": "viewer"}),
    ("POST", f"/api/users/{uuid.uuid4()}/reset-password", {"new_password": "AbcDef123456"}),
    ("DELETE", f"/api/users/{uuid.uuid4()}", None),
    ("PUT", "/api/config/snapshot", {"body": {"logging": {"level": "info"}}}),
]

_WRITER_ONLY = [
    # Feedback writes — admin/auditor pass, viewer 403.
    ("POST", f"/api/findings/{uuid.uuid4()}/feedback",
     {"label": "true_positive", "researcher_note": None}),
    ("PATCH", f"/api/findings/{uuid.uuid4()}/feedback",
     {"label": "false_positive", "researcher_note": None}),
]


class AdminOnlyEndpointsTests(unittest.TestCase):
    def test_viewer_is_403(self) -> None:
        with _client(Role.VIEWER.value) as c:
            for method, url, body in _ADMIN_ONLY:
                r = c.request(method, url, json=body)
                self.assertEqual(
                    r.status_code, 403,
                    f"viewer on {method} {url} got {r.status_code}",
                )

    def test_auditor_is_403(self) -> None:
        with _client(Role.AUDITOR.value) as c:
            for method, url, body in _ADMIN_ONLY:
                r = c.request(method, url, json=body)
                self.assertEqual(
                    r.status_code, 403,
                    f"auditor on {method} {url} got {r.status_code}",
                )

    def test_admin_passes_the_gate(self) -> None:
        with _client(Role.ADMIN.value) as c:
            for method, url, body in _ADMIN_ONLY:
                r = c.request(method, url, json=body)
                self.assertNotIn(
                    r.status_code, (401, 403),
                    f"admin on {method} {url} got auth/role error {r.status_code}",
                )

    def test_unauthenticated_is_401(self) -> None:
        with _client(role=None) as c:
            for method, url, body in _ADMIN_ONLY:
                r = c.request(method, url, json=body)
                self.assertEqual(
                    r.status_code, 401,
                    f"unauthenticated on {method} {url} got {r.status_code}",
                )


class WriterOnlyEndpointsTests(unittest.TestCase):
    def test_viewer_is_403(self) -> None:
        with _client(Role.VIEWER.value) as c:
            for method, url, body in _WRITER_ONLY:
                r = c.request(method, url, json=body)
                self.assertEqual(
                    r.status_code, 403,
                    f"viewer on {method} {url} got {r.status_code}",
                )

    def test_auditor_passes_the_gate(self) -> None:
        with _client(Role.AUDITOR.value) as c:
            for method, url, body in _WRITER_ONLY:
                r = c.request(method, url, json=body)
                self.assertNotIn(
                    r.status_code, (401, 403),
                    f"auditor on {method} {url} got auth/role error {r.status_code}",
                )

    def test_admin_passes_the_gate(self) -> None:
        with _client(Role.ADMIN.value) as c:
            for method, url, body in _WRITER_ONLY:
                r = c.request(method, url, json=body)
                self.assertNotIn(
                    r.status_code, (401, 403),
                    f"admin on {method} {url} got auth/role error {r.status_code}",
                )


if __name__ == "__main__":
    unittest.main()
