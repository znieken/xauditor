"""FastAPI dependencies for sessions, auth, role-gating, and must-change-password."""

from __future__ import annotations

from typing import AsyncIterator

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xauditor_portal.auth.roles import Role
from xauditor_portal.auth.tokens import (
    COOKIE_NAME,
    TokenError,
    TokenPayload,
    decode_token,
)
from xauditor_portal.db.models.auth import RevokedSession, User
from xauditor_portal.settings import PortalSettings, get_settings, resolve_jwt_secret


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield an AsyncSession from the app-level factory.

    Wiring: `app.state.session_factory` is an ``async_sessionmaker`` set by
    `create_app` at startup. Tests can override this via ``dependency_overrides``
    or by assigning a fake ``session_factory`` to ``app.state``.
    """

    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database session factory not configured.",
        )
    async with factory() as session:
        try:
            yield session
        finally:
            pass


def get_portal_settings(request: Request) -> PortalSettings:
    # Allow app.state override for tests.
    override = getattr(request.app.state, "portal_settings", None)
    return override or get_settings()


async def current_user(
    request: Request,
    cookie_token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    session: AsyncSession = Depends(get_session),
    settings: PortalSettings = Depends(get_portal_settings),
) -> User:
    if not cookie_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    try:
        payload = _decode(cookie_token, settings=settings, request=request)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    # Reject revoked sessions (password change invalidates prior jti entries).
    revoked = await session.scalar(
        select(RevokedSession).where(RevokedSession.jti == payload.jti)
    )
    if revoked is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session has been revoked.",
        )
    user = await session.get(User, payload.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User no longer exists.",
        )
    # Reject any token issued before the user's `sessions_invalid_before`
    # boundary. This is how role changes and admin-initiated password resets
    # close out the affected user's existing cookies without enumerating
    # outstanding jti values.
    if user.sessions_invalid_before is not None:
        if payload.issued_at < user.sessions_invalid_before:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session has been revoked.",
            )
    # Reject any request whose user has been soft-disabled. Disable also
    # stamps `sessions_invalid_before` to now, so most cookies will fall
    # afoul of the boundary check above; this guard is a belt-and-braces
    # check that also covers any cookie issued exactly at disable time.
    if user.disabled_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account has been disabled.",
        )
    # Stash the decoded payload for downstream handlers that need the jti.
    request.state.token_payload = payload
    return user


async def current_user_allow_mcp_path(
    user: User = Depends(current_user),
) -> User:
    """Use when the endpoint must be reachable during a forced password change.

    The default `current_user` does NOT block must_change_password users. The
    `forbid_must_change_password` dependency below does — attach that to any
    endpoint that should be inaccessible until the user rotates.
    """

    return user


async def forbid_must_change_password(
    user: User = Depends(current_user),
) -> User:
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Password change required. POST /api/auth/change-password "
                "before using other endpoints."
            ),
        )
    return user


# `require_authenticated` is the role-aware alias for the password-rotation
# gate. Endpoints that simply require an authenticated, password-rotated user
# (with no role tier requirement) SHOULD prefer this name.
require_authenticated = forbid_must_change_password


async def require_admin(
    user: User = Depends(forbid_must_change_password),
) -> User:
    if user.role != Role.ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires the admin role.",
        )
    return user


async def require_writer(
    user: User = Depends(forbid_must_change_password),
) -> User:
    if user.role not in (Role.ADMIN.value, Role.AUDITOR.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint requires the admin or auditor role.",
        )
    return user


def _decode(
    token: str, *, settings: PortalSettings, request: Request
) -> TokenPayload:
    secret_override = getattr(request.app.state, "jwt_secret", None)
    secret = secret_override or resolve_jwt_secret(settings)
    return decode_token(token, secret=secret)


__all__ = [
    "current_user",
    "current_user_allow_mcp_path",
    "forbid_must_change_password",
    "get_portal_settings",
    "get_session",
    "require_admin",
    "require_authenticated",
    "require_writer",
]
