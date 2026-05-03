"""`/api/auth/*` endpoints: login, logout, password change, me."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xauditor_portal.auth import (
    PasswordPolicyError,
    clear_session_cookie,
    current_user,
    encode_token,
    get_portal_settings,
    get_session,
    hash_password,
    set_session_cookie,
    validate_password_policy,
    verify_password,
)
from xauditor_portal.auth.tokens import TokenPayload
from xauditor_portal.db.models.auth import PasswordHistory, RevokedSession, User
from xauditor_portal.settings import PortalSettings, resolve_jwt_secret


router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=512)


class LoginResponse(BaseModel):
    username: str
    must_change_password: bool
    role: str


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)


class MeResponse(BaseModel):
    id: str
    username: str
    must_change_password: bool
    role: str


def _resolve_secret(request: Request, settings: PortalSettings) -> str:
    override = getattr(request.app.state, "jwt_secret", None)
    return override or resolve_jwt_secret(settings)


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    settings: PortalSettings = Depends(get_portal_settings),
) -> LoginResponse:
    user = await session.scalar(select(User).where(User.username == payload.username))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )
    if user.disabled_at is not None:
        # Surface 403 (not 401) so the operator understands the account is
        # disabled rather than mis-typed. We only reach here after a correct
        # password, so the username's existence is already implicit.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been disabled. Contact an administrator.",
        )
    token, _ = encode_token(
        user_id=str(user.id),
        username=user.username,
        must_change_password=user.must_change_password,
        role=user.role,
        secret=_resolve_secret(request, settings),
        ttl_seconds=settings.jwt_ttl_seconds,
    )
    set_session_cookie(response, token, max_age_seconds=settings.jwt_ttl_seconds)
    return LoginResponse(
        username=user.username,
        must_change_password=user.must_change_password,
        role=user.role,
    )


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    payload: TokenPayload | None = getattr(request.state, "token_payload", None)
    if payload is not None:
        session.add(RevokedSession(user_id=user.id, jti=payload.jti))
        await session.commit()
    clear_session_cookie(response)
    return {"status": "ok"}


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
    settings: PortalSettings = Depends(get_portal_settings),
) -> dict[str, str]:
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect.",
        )
    try:
        validate_password_policy(
            body.new_password, min_length=settings.password_min_length
        )
    except PasswordPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    if verify_password(body.new_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must differ from the current password.",
        )
    session.add(
        PasswordHistory(
            user_id=user.id,
            password_hash=user.password_hash,
            changed_by_user_id=user.id,
        )
    )
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    user.updated_at = datetime.now(timezone.utc)

    # Invalidate every other active session for this user by revoking the
    # current jti plus rotating the cookie with a freshly-issued token.
    payload: TokenPayload | None = getattr(request.state, "token_payload", None)
    if payload is not None:
        session.add(RevokedSession(user_id=user.id, jti=payload.jti))
    await session.commit()

    new_token, _ = encode_token(
        user_id=str(user.id),
        username=user.username,
        must_change_password=False,
        role=user.role,
        secret=_resolve_secret(request, settings),
        ttl_seconds=settings.jwt_ttl_seconds,
    )
    set_session_cookie(response, new_token, max_age_seconds=settings.jwt_ttl_seconds)
    return {"status": "ok"}


@router.get("/me", response_model=MeResponse)
async def me(user: User = Depends(current_user)) -> MeResponse:
    return MeResponse(
        id=str(user.id),
        username=user.username,
        must_change_password=user.must_change_password,
        role=user.role,
    )
