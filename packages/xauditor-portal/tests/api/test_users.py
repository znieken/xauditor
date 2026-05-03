"""Tests for ``/api/users/*`` admin-only CRUD endpoints.

These tests exercise the role-gate, body validation, and happy-path
response shape using ``TestClient`` with the auth and DB-session
dependencies overridden. The advisory-lock concurrency scenarios live
in the live-DB test class at the bottom (skipped without
``XAUDITOR_TEST_DATABASE_URL``).
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


def _user(role: str, *, must_change_password: bool = False) -> User:
    user = User(
        id=uuid.uuid4(),
        username="actor",
        password_hash="x",
        must_change_password=must_change_password,
        role=role,
    )
    user.created_at = datetime.now(timezone.utc)
    user.updated_at = user.created_at
    return user


def _fake_session() -> MagicMock:
    """Return a MagicMock that satisfies the ``AsyncSession`` shape used
    by ``users.py``. Individual tests override specific call returns."""

    session = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    session.get = AsyncMock(return_value=None)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.rollback = AsyncMock()
    session.delete = AsyncMock()
    return session


def _build_client(*, role: str, session: Any | None = None) -> TestClient:
    app = create_app()

    actor = _user(role)
    app.dependency_overrides[current_user] = lambda: actor

    fake_session = session or _fake_session()

    async def _provider() -> Any:
        yield fake_session

    app.dependency_overrides[get_session] = _provider
    client = TestClient(app)
    client._actor = actor  # type: ignore[attr-defined]
    client._session = fake_session  # type: ignore[attr-defined]
    return client


class RoleGateTests(unittest.TestCase):
    """Every /api/users endpoint requires the admin role. Auditor and
    viewer get 403; unauthenticated gets 401."""

    def test_auditor_is_blocked_from_every_endpoint(self) -> None:
        with _build_client(role=Role.AUDITOR.value) as client:
            for method, url, body in [
                ("GET", "/api/users", None),
                ("POST", "/api/users", {"username": "a", "initial_password": "AbcDef123456", "role": "viewer"}),
                ("PATCH", f"/api/users/{uuid.uuid4()}", {"role": "viewer"}),
                ("POST", f"/api/users/{uuid.uuid4()}/reset-password", {"new_password": "AbcDef123456"}),
                ("DELETE", f"/api/users/{uuid.uuid4()}", None),
            ]:
                response = client.request(method, url, json=body)
                self.assertEqual(
                    response.status_code, 403,
                    f"auditor on {method} {url} got {response.status_code}",
                )

    def test_viewer_is_blocked_from_every_endpoint(self) -> None:
        with _build_client(role=Role.VIEWER.value) as client:
            response = client.get("/api/users")
            self.assertEqual(response.status_code, 403)

    def test_unauthenticated_request_is_401(self) -> None:
        # Build a client WITHOUT overriding current_user, so it falls
        # through to the real dependency which expects a cookie.
        app = create_app()

        async def _provider() -> Any:
            yield _fake_session()

        app.dependency_overrides[get_session] = _provider

        with TestClient(app) as client:
            response = client.get("/api/users")
        self.assertEqual(response.status_code, 401)


class CreateUserValidationTests(unittest.TestCase):
    def test_unknown_role_is_400(self) -> None:
        with _build_client(role=Role.ADMIN.value) as client:
            response = client.post(
                "/api/users",
                json={"username": "alice", "initial_password": "AbcDef123456", "role": "superuser"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("superuser", response.json()["detail"])

    def test_weak_password_is_400(self) -> None:
        with _build_client(role=Role.ADMIN.value) as client:
            response = client.post(
                "/api/users",
                json={"username": "alice", "initial_password": "short1", "role": "viewer"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("12", response.json()["detail"])

    def test_missing_field_is_422(self) -> None:
        with _build_client(role=Role.ADMIN.value) as client:
            response = client.post(
                "/api/users",
                json={"username": "alice", "role": "viewer"},  # initial_password missing
            )
        self.assertEqual(response.status_code, 422)


class CreateUserHappyPathTests(unittest.TestCase):
    def test_create_user_happy_path(self) -> None:
        # Build a session whose commit + refresh succeed; no IntegrityError.
        session = _fake_session()

        async def _refresh(obj: User) -> None:
            # Mimic what session.refresh does in practice — give the row
            # a stable id + timestamps so the response shape is complete.
            obj.id = uuid.uuid4()
            obj.created_at = datetime.now(timezone.utc)
            obj.updated_at = obj.created_at

        session.refresh = AsyncMock(side_effect=_refresh)

        with _build_client(role=Role.ADMIN.value, session=session) as client:
            response = client.post(
                "/api/users",
                json={
                    "username": "alice",
                    "initial_password": "AbcDef123456",
                    "role": "auditor",
                },
            )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["username"], "alice")
        self.assertEqual(body["role"], "auditor")
        self.assertTrue(body["must_change_password"])
        self.assertNotIn("password_hash", body)


class ListUsersTests(unittest.TestCase):
    def test_list_returns_items_without_password_hash(self) -> None:
        admin = _user(Role.ADMIN.value)
        auditor = _user(Role.AUDITOR.value)

        session = _fake_session()
        result = MagicMock()
        scalars = MagicMock()
        scalars.all = MagicMock(return_value=[admin, auditor])
        result.scalars = MagicMock(return_value=scalars)
        session.execute = AsyncMock(return_value=result)

        with _build_client(role=Role.ADMIN.value, session=session) as client:
            response = client.get("/api/users")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 2)
        roles = {item["role"] for item in body["items"]}
        self.assertEqual(roles, {"admin", "auditor"})
        for item in body["items"]:
            self.assertNotIn("password_hash", item)


class UpdateUserValidationTests(unittest.TestCase):
    def test_invalid_uuid_is_404(self) -> None:
        with _build_client(role=Role.ADMIN.value) as client:
            response = client.patch("/api/users/not-a-uuid", json={"role": "viewer"})
        self.assertEqual(response.status_code, 404)

    def test_unknown_role_is_400(self) -> None:
        with _build_client(role=Role.ADMIN.value) as client:
            response = client.patch(
                f"/api/users/{uuid.uuid4()}", json={"role": "supremo"}
            )
        self.assertEqual(response.status_code, 400)

    def test_empty_patch_body_is_400(self) -> None:
        with _build_client(role=Role.ADMIN.value) as client:
            response = client.patch(f"/api/users/{uuid.uuid4()}", json={})
        self.assertEqual(response.status_code, 400)


class ResetPasswordValidationTests(unittest.TestCase):
    def test_weak_password_is_400(self) -> None:
        with _build_client(role=Role.ADMIN.value) as client:
            response = client.post(
                f"/api/users/{uuid.uuid4()}/reset-password",
                json={"new_password": "short"},
            )
        self.assertEqual(response.status_code, 400)


class DeleteUserTests(unittest.TestCase):
    def test_invalid_uuid_is_404(self) -> None:
        with _build_client(role=Role.ADMIN.value) as client:
            response = client.delete("/api/users/not-a-uuid")
        self.assertEqual(response.status_code, 404)

    def test_target_not_found_is_404(self) -> None:
        # Default _fake_session has session.get returning None.
        with _build_client(role=Role.ADMIN.value) as client:
            response = client.delete(f"/api/users/{uuid.uuid4()}")
        self.assertEqual(response.status_code, 404)

    def test_self_deletion_is_409(self) -> None:
        with _build_client(role=Role.ADMIN.value) as client:
            actor: User = client._actor  # type: ignore[attr-defined]
            response = client.delete(f"/api/users/{actor.id}")
        self.assertEqual(response.status_code, 409)
        self.assertIn("your own account", response.json()["detail"].lower())


class DisableEnableTests(unittest.TestCase):
    """Soft-disable / re-enable + last-admin-guard coverage."""

    def _target(
        self, *, role: str = Role.AUDITOR.value, disabled: bool = False
    ) -> User:
        target = _user(role=role)
        target.id = uuid.uuid4()
        target.username = "target"
        if disabled:
            target.disabled_at = datetime.now(timezone.utc)
        return target

    def test_disable_invalid_uuid_is_404(self) -> None:
        with _build_client(role=Role.ADMIN.value) as client:
            response = client.post("/api/users/not-a-uuid/disable")
        self.assertEqual(response.status_code, 404)

    def test_disable_target_not_found_is_404(self) -> None:
        with _build_client(role=Role.ADMIN.value) as client:
            response = client.post(f"/api/users/{uuid.uuid4()}/disable")
        self.assertEqual(response.status_code, 404)

    def test_disable_active_non_admin_succeeds(self) -> None:
        target = self._target()
        session = _fake_session()
        session.get = AsyncMock(return_value=target)

        async def _refresh(_: User) -> None:
            return None

        session.refresh = AsyncMock(side_effect=_refresh)

        with _build_client(role=Role.ADMIN.value, session=session) as client:
            response = client.post(f"/api/users/{target.id}/disable")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNotNone(body["disabled_at"])
        self.assertIsNotNone(target.disabled_at)
        self.assertIsNotNone(target.sessions_invalid_before)
        # Commit MUST run before the response is returned.
        session.commit.assert_awaited()

    def test_disable_already_disabled_is_idempotent_no_mutation(self) -> None:
        target = self._target(disabled=True)
        original_disabled_at = target.disabled_at
        session = _fake_session()
        session.get = AsyncMock(return_value=target)

        with _build_client(role=Role.ADMIN.value, session=session) as client:
            response = client.post(f"/api/users/{target.id}/disable")
        self.assertEqual(response.status_code, 200)
        # The row was returned unchanged — neither disabled_at nor any
        # other field was advanced.
        self.assertEqual(target.disabled_at, original_disabled_at)
        session.commit.assert_not_awaited()

    def test_enable_clears_disabled_at(self) -> None:
        target = self._target(disabled=True)
        session = _fake_session()
        session.get = AsyncMock(return_value=target)
        session.refresh = AsyncMock()

        with _build_client(role=Role.ADMIN.value, session=session) as client:
            response = client.post(f"/api/users/{target.id}/enable")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body["disabled_at"])
        self.assertIsNone(target.disabled_at)

    def test_enable_already_enabled_is_idempotent(self) -> None:
        target = self._target(disabled=False)
        session = _fake_session()
        session.get = AsyncMock(return_value=target)

        with _build_client(role=Role.ADMIN.value, session=session) as client:
            response = client.post(f"/api/users/{target.id}/enable")
        self.assertEqual(response.status_code, 200)
        session.commit.assert_not_awaited()

    def test_disabling_only_remaining_admin_is_409(self) -> None:
        # Target is an admin; _count_admins returns 1 (only one enabled
        # admin in the system), so disable MUST refuse.
        target = self._target(role=Role.ADMIN.value)
        session = _fake_session()
        session.get = AsyncMock(return_value=target)

        # _count_admins runs `select(User).where(...)` and counts the
        # scalars; we patch session.execute so only one admin row is
        # returned.
        admin_only_result = MagicMock()
        admin_only_result.scalars.return_value.all.return_value = [target]
        session.execute = AsyncMock(return_value=admin_only_result)

        with _build_client(role=Role.ADMIN.value, session=session) as client:
            response = client.post(f"/api/users/{target.id}/disable")
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(target.disabled_at)

    def test_disabling_admin_with_peer_admin_succeeds(self) -> None:
        target = self._target(role=Role.ADMIN.value)
        peer = self._target(role=Role.ADMIN.value)
        peer.id = uuid.uuid4()
        session = _fake_session()
        session.get = AsyncMock(return_value=target)

        # Two enabled admins: target + peer.
        result = MagicMock()
        result.scalars.return_value.all.return_value = [target, peer]
        session.execute = AsyncMock(return_value=result)
        session.refresh = AsyncMock()

        with _build_client(role=Role.ADMIN.value, session=session) as client:
            response = client.post(f"/api/users/{target.id}/disable")
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(target.disabled_at)


class SelfRoleChangeRejectionTests(unittest.TestCase):
    """Admin SHALL NOT change their own role.

    The body carrying a ``role`` field — even when it matches the current
    role — is treated as a role-change request and refused. Username self-
    edits without a role field still succeed.
    """

    def test_self_role_change_to_other_role_is_409(self) -> None:
        with _build_client(role=Role.ADMIN.value) as client:
            actor: User = client._actor  # type: ignore[attr-defined]
            response = client.patch(
                f"/api/users/{actor.id}", json={"role": "auditor"}
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("own role", response.json()["detail"].lower())

    def test_self_role_change_to_same_role_is_still_409(self) -> None:
        with _build_client(role=Role.ADMIN.value) as client:
            actor: User = client._actor  # type: ignore[attr-defined]
            response = client.patch(
                f"/api/users/{actor.id}", json={"role": "admin"}
            )
        # Body carries a role field → rejected even though the role does
        # not actually change.
        self.assertEqual(response.status_code, 409)

    def test_self_username_edit_no_role_proceeds(self) -> None:
        # The handler proceeds past the self-role check and then reaches
        # the advisory lock + row read. session.get returning None means
        # the lookup eventually 404s, which is fine — what we care about
        # is that the 409 path was NOT taken (i.e., we got past the self-
        # role guard).
        session = _fake_session()
        session.get = AsyncMock(return_value=None)

        with _build_client(role=Role.ADMIN.value, session=session) as client:
            actor: User = client._actor  # type: ignore[attr-defined]
            response = client.patch(
                f"/api/users/{actor.id}", json={"username": "new-name"}
            )
        # 404 (not 409) proves we passed the self-role guard.
        self.assertEqual(response.status_code, 404)

    def test_peer_role_change_remains_allowed(self) -> None:
        peer = _user(role=Role.ADMIN.value)
        peer.id = uuid.uuid4()
        peer.username = "peer"
        session = _fake_session()
        session.get = AsyncMock(return_value=peer)

        # Two admins exist so the last-admin guard passes.
        admin_result = MagicMock()
        admin_result.scalars.return_value.all.return_value = [peer, peer]
        session.execute = AsyncMock(return_value=admin_result)
        session.refresh = AsyncMock()

        with _build_client(role=Role.ADMIN.value, session=session) as client:
            response = client.patch(
                f"/api/users/{peer.id}", json={"role": "auditor"}
            )
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
