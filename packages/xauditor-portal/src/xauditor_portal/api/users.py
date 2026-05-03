"""``/api/users/*`` admin-only CRUD endpoints.

The router-level dependency gates every endpoint on ``require_admin``,
so non-admin callers receive HTTP 403 before any handler runs. The
last-admin invariant is enforced inside ``DELETE`` and inside any
``PATCH`` that demotes an admin, both protected by an advisory lock so
two concurrent demotions cannot race the system into zero admins.

Role changes and admin-initiated password resets stamp the affected
user's ``sessions_invalid_before`` to ``now()``. The ``current_user``
dependency rejects any cookie whose ``iat`` predates that timestamp,
which closes the session-staleness window without enumerating
outstanding ``jti`` values.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from xauditor_portal.auth import (
    PasswordPolicyError,
    Role,
    coerce_role,
    get_session,
    hash_password,
    require_admin,
    validate_password_policy,
)
from xauditor_portal.db.models.auth import User
from xauditor_portal.settings import PortalSettings, get_settings


router = APIRouter(tags=["users"], dependencies=[Depends(require_admin)])

# Postgres advisory-lock key for the last-admin guard. The exact value is
# arbitrary; consistency is what matters. Using a fixed key serializes
# every users-table mutation that could affect the admin count.
_LAST_ADMIN_LOCK_KEY = 0x7861_7574_5253_4143  # ASCII("xautRSAC") truncated


class UserSummary(BaseModel):
    id: str
    username: str
    role: str
    must_change_password: bool
    disabled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UserListResponse(BaseModel):
    items: list[UserSummary]
    total: int


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    initial_password: str = Field(min_length=1, max_length=512)
    role: str = Field(min_length=1, max_length=16)


class UpdateUserRequest(BaseModel):
    username: str | None = Field(default=None, min_length=1, max_length=128)
    role: str | None = Field(default=None, min_length=1, max_length=16)


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=1, max_length=512)


def _to_summary(user: User) -> UserSummary:
    return UserSummary(
        id=str(user.id),
        username=user.username,
        role=user.role,
        must_change_password=user.must_change_password,
        disabled_at=user.disabled_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


async def _lock_admin_count(session: AsyncSession) -> None:
    """Take the per-transaction advisory lock that serializes every
    modification capable of changing the admin count.

    Using ``pg_advisory_xact_lock`` (transaction-scoped) means the lock is
    released automatically on commit/rollback; no manual cleanup needed.
    """

    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LAST_ADMIN_LOCK_KEY})


async def _count_admins(session: AsyncSession) -> int:
    """Count enabled admins.

    A disabled admin (``disabled_at IS NOT NULL``) does not satisfy the
    "at least one admin" invariant — they cannot log in and cannot serve
    the operator role they nominally hold. The count therefore filters
    on both ``role`` and ``disabled_at``.
    """

    result = await session.execute(
        select(User).where(
            User.role == Role.ADMIN.value,
            User.disabled_at.is_(None),
        )
    )
    return len(result.scalars().all())


def _settings() -> PortalSettings:
    return get_settings()


@router.get("", response_model=UserListResponse)
async def list_users(
    session: AsyncSession = Depends(get_session),
) -> UserListResponse:
    rows = (
        await session.execute(select(User).order_by(User.created_at))
    ).scalars().all()
    items = [_to_summary(u) for u in rows]
    return UserListResponse(items=items, total=len(items))


@router.post("", response_model=UserSummary, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: CreateUserRequest,
    settings: Annotated[PortalSettings, Depends(get_settings)],
    session: AsyncSession = Depends(get_session),
) -> UserSummary:
    try:
        role = coerce_role(payload.role)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    try:
        validate_password_policy(
            payload.initial_password, min_length=settings.password_min_length
        )
    except PasswordPolicyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    user = User(
        username=payload.username,
        password_hash=hash_password(payload.initial_password),
        must_change_password=True,
        role=role.value,
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"Username {payload.username!r} is already in use.",
        ) from exc
    await session.refresh(user)
    return _to_summary(user)


@router.patch("/{user_id}", response_model=UserSummary)
async def update_user(
    user_id: str,
    payload: UpdateUserRequest,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(require_admin),
) -> UserSummary:
    try:
        target_uuid = uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.") from exc

    new_role: Role | None = None
    if payload.role is not None:
        try:
            new_role = coerce_role(payload.role)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if payload.username is None and new_role is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="Provide at least one of `username` or `role`.",
        )

    # Self-role-change prohibition. The body carrying a ``role`` field — even
    # when it matches the current role — is treated as a role-change request.
    # This keeps the rule binary: either the request is asking to change the
    # role or it is not. Username self-edits (no ``role`` field) still pass.
    # Reject before the advisory lock so we do not waste it on a request that
    # will fail.
    if target_uuid == actor.id and payload.role is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Admins cannot change their own role. Ask another admin to change it.",
        )

    # Take the advisory lock BEFORE reading the row so concurrent role
    # updates against the same user — and concurrent demotions of the only
    # remaining admin — serialize through this critical section.
    await _lock_admin_count(session)

    target = await session.get(User, target_uuid)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")

    role_changed = False
    if new_role is not None and new_role.value != target.role:
        # Last-admin guard: refuse to demote the only remaining admin.
        if target.role == Role.ADMIN.value and new_role != Role.ADMIN:
            admin_count = await _count_admins(session)
            if admin_count <= 1:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    detail="Cannot leave the system without an admin user.",
                )
        target.role = new_role.value
        role_changed = True

    if payload.username is not None and payload.username != target.username:
        target.username = payload.username

    target.updated_at = datetime.now(timezone.utc)
    if role_changed:
        # Stamp the boundary so existing JWTs for this user start failing
        # the `current_user` check on their next request.
        target.sessions_invalid_before = target.updated_at

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="Username is already in use.",
        ) from exc

    await session.refresh(target)
    return _to_summary(target)


@router.post("/{user_id}/reset-password", response_model=UserSummary)
async def reset_password(
    user_id: str,
    payload: ResetPasswordRequest,
    settings: Annotated[PortalSettings, Depends(get_settings)],
    session: AsyncSession = Depends(get_session),
) -> UserSummary:
    try:
        target_uuid = uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.") from exc

    try:
        validate_password_policy(
            payload.new_password, min_length=settings.password_min_length
        )
    except PasswordPolicyError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    target = await session.get(User, target_uuid)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")

    target.password_hash = hash_password(payload.new_password)
    target.must_change_password = True
    now = datetime.now(timezone.utc)
    target.updated_at = now
    target.sessions_invalid_before = now

    await session.commit()
    await session.refresh(target)
    return _to_summary(target)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(require_admin),
) -> None:
    try:
        target_uuid = uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.") from exc

    # Reject self-deletion before any DB work — even with another admin
    # available, deleting yourself revokes your own session mid-action and
    # strands the UI in a broken state. Match the UI's disabled-button.
    if target_uuid == actor.id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="You cannot delete your own account.",
        )

    await _lock_admin_count(session)

    target = await session.get(User, target_uuid)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")

    if target.role == Role.ADMIN.value:
        admin_count = await _count_admins(session)
        if admin_count <= 1:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="Cannot leave the system without an admin user.",
            )

    await session.delete(target)
    await session.commit()


@router.post("/{user_id}/disable", response_model=UserSummary)
async def disable_user(
    user_id: str,
    session: AsyncSession = Depends(get_session),
) -> UserSummary:
    """Soft-disable a user.

    Sets ``disabled_at`` to now (idempotent: no-op if already disabled),
    stamps ``sessions_invalid_before`` so the user's existing JWTs fail on
    the next request, and runs the last-admin guard if the target is an
    admin so the system never loses its only enabled admin.
    """

    try:
        target_uuid = uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.") from exc

    await _lock_admin_count(session)

    target = await session.get(User, target_uuid)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")

    # Idempotent: already disabled → return the current row unchanged so
    # admins can mash the button without producing redundant timestamps.
    if target.disabled_at is not None:
        return _to_summary(target)

    if target.role == Role.ADMIN.value:
        admin_count = await _count_admins(session)
        if admin_count <= 1:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail="Cannot leave the system without an admin user.",
            )

    now = datetime.now(timezone.utc)
    target.disabled_at = now
    target.updated_at = now
    target.sessions_invalid_before = now

    await session.commit()
    await session.refresh(target)
    return _to_summary(target)


@router.post("/{user_id}/enable", response_model=UserSummary)
async def enable_user(
    user_id: str,
    session: AsyncSession = Depends(get_session),
) -> UserSummary:
    """Re-enable a previously disabled user.

    Clears ``disabled_at`` to NULL. Does NOT touch ``sessions_invalid_before``:
    the user must re-authenticate. Idempotent: no-op when the user is
    already enabled.
    """

    try:
        target_uuid = uuid.UUID(user_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.") from exc

    target = await session.get(User, target_uuid)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")

    if target.disabled_at is None:
        return _to_summary(target)

    target.disabled_at = None
    target.updated_at = datetime.now(timezone.utc)

    await session.commit()
    await session.refresh(target)
    return _to_summary(target)


__all__ = ["router"]
