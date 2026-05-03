"""Unit tests for the role-aware FastAPI dependencies in auth.deps.

These tests exercise the dependencies as plain async callables — they do
not spin up a FastAPI app. The role-matrix integration tests under
``tests/api/`` exercise the dependencies via real HTTP requests.
"""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from xauditor_portal.auth.deps import (
    current_user,
    forbid_must_change_password,
    require_admin,
    require_authenticated,
    require_writer,
)
from xauditor_portal.auth.roles import Role
from xauditor_portal.auth.tokens import TokenPayload
from xauditor_portal.db.models.auth import User


def _user(*, role: str, must_change_password: bool = False) -> User:
    return User(
        id=uuid.uuid4(),
        username="alice",
        password_hash="x",
        must_change_password=must_change_password,
        role=role,
    )


class RoleGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_passes_all_three_gates(self) -> None:
        admin = _user(role=Role.ADMIN.value)
        self.assertIs(await forbid_must_change_password(admin), admin)
        self.assertIs(await require_authenticated(admin), admin)
        self.assertIs(await require_writer(admin), admin)
        self.assertIs(await require_admin(admin), admin)

    async def test_auditor_passes_writer_and_authenticated(self) -> None:
        auditor = _user(role=Role.AUDITOR.value)
        self.assertIs(await require_authenticated(auditor), auditor)
        self.assertIs(await require_writer(auditor), auditor)
        with self.assertRaises(HTTPException) as cm:
            await require_admin(auditor)
        self.assertEqual(cm.exception.status_code, 403)
        self.assertIn("admin", cm.exception.detail.lower())

    async def test_viewer_passes_only_authenticated(self) -> None:
        viewer = _user(role=Role.VIEWER.value)
        self.assertIs(await require_authenticated(viewer), viewer)
        with self.assertRaises(HTTPException) as cm_writer:
            await require_writer(viewer)
        self.assertEqual(cm_writer.exception.status_code, 403)
        with self.assertRaises(HTTPException) as cm_admin:
            await require_admin(viewer)
        self.assertEqual(cm_admin.exception.status_code, 403)

    async def test_must_change_password_blocks_every_role_gate(self) -> None:
        forced = _user(role=Role.ADMIN.value, must_change_password=True)
        with self.assertRaises(HTTPException) as cm:
            await forbid_must_change_password(forced)
        self.assertEqual(cm.exception.status_code, 403)
        self.assertIn("password change required", cm.exception.detail.lower())


class SessionsInvalidBeforeTests(unittest.IsolatedAsyncioTestCase):
    """Verify that ``current_user`` rejects tokens whose ``iat`` is before
    the user's ``sessions_invalid_before`` boundary.
    """

    async def test_token_predating_invalidation_boundary_is_rejected(self) -> None:
        boundary = datetime.now(timezone.utc)
        old_iat = boundary - timedelta(seconds=5)

        target_user = _user(role=Role.AUDITOR.value)
        target_user.sessions_invalid_before = boundary

        session = MagicMock()
        session.scalar = AsyncMock(return_value=None)  # not in revoked_sessions
        session.get = AsyncMock(return_value=target_user)

        token_payload = TokenPayload(
            user_id=str(target_user.id),
            username=target_user.username,
            jti="jti-1",
            must_change_password=False,
            role=target_user.role,
            issued_at=old_iat,
            expires_at=boundary + timedelta(hours=1),
        )

        request = MagicMock()
        request.state = MagicMock()

        with patch("xauditor_portal.auth.deps._decode", return_value=token_payload):
            with self.assertRaises(HTTPException) as cm:
                await current_user(
                    request=request,
                    cookie_token="anything",
                    session=session,
                    settings=MagicMock(),
                )
        self.assertEqual(cm.exception.status_code, 401)
        self.assertIn("revoked", cm.exception.detail.lower())

    async def test_token_after_invalidation_boundary_is_accepted(self) -> None:
        boundary = datetime.now(timezone.utc) - timedelta(minutes=5)
        recent_iat = datetime.now(timezone.utc)

        target_user = _user(role=Role.ADMIN.value)
        target_user.sessions_invalid_before = boundary

        session = MagicMock()
        session.scalar = AsyncMock(return_value=None)
        session.get = AsyncMock(return_value=target_user)

        token_payload = TokenPayload(
            user_id=str(target_user.id),
            username=target_user.username,
            jti="jti-2",
            must_change_password=False,
            role=target_user.role,
            issued_at=recent_iat,
            expires_at=recent_iat + timedelta(hours=1),
        )

        request = MagicMock()
        request.state = MagicMock()

        with patch("xauditor_portal.auth.deps._decode", return_value=token_payload):
            result = await current_user(
                request=request,
                cookie_token="anything",
                session=session,
                settings=MagicMock(),
            )
        self.assertIs(result, target_user)


class DisabledUserTests(unittest.IsolatedAsyncioTestCase):
    """``current_user`` SHALL reject any user whose ``disabled_at`` is set
    with HTTP 401 even if the JWT is otherwise valid."""

    async def test_disabled_user_with_valid_jwt_is_401(self) -> None:
        target_user = _user(role=Role.AUDITOR.value)
        target_user.disabled_at = datetime.now(timezone.utc)

        session = MagicMock()
        session.scalar = AsyncMock(return_value=None)
        session.get = AsyncMock(return_value=target_user)

        token_payload = TokenPayload(
            user_id=str(target_user.id),
            username=target_user.username,
            jti="jti-d",
            must_change_password=False,
            role=target_user.role,
            issued_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        request = MagicMock()
        request.state = MagicMock()

        with patch("xauditor_portal.auth.deps._decode", return_value=token_payload):
            with self.assertRaises(HTTPException) as cm:
                await current_user(
                    request=request,
                    cookie_token="anything",
                    session=session,
                    settings=MagicMock(),
                )
        self.assertEqual(cm.exception.status_code, 401)
        self.assertIn("disabled", cm.exception.detail.lower())

    async def test_active_user_passes_disable_check(self) -> None:
        target_user = _user(role=Role.AUDITOR.value)
        # disabled_at left as None — the user is active.

        session = MagicMock()
        session.scalar = AsyncMock(return_value=None)
        session.get = AsyncMock(return_value=target_user)

        token_payload = TokenPayload(
            user_id=str(target_user.id),
            username=target_user.username,
            jti="jti-a",
            must_change_password=False,
            role=target_user.role,
            issued_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        request = MagicMock()
        request.state = MagicMock()

        with patch("xauditor_portal.auth.deps._decode", return_value=token_payload):
            result = await current_user(
                request=request,
                cookie_token="anything",
                session=session,
                settings=MagicMock(),
            )
        self.assertIs(result, target_user)


if __name__ == "__main__":
    unittest.main()
