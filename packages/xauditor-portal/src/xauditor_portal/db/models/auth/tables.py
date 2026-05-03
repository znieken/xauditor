"""`auth` schema tables: users, password history, revoked-session jti list."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from xauditor_portal.db.base import Base, utcnow


SCHEMA = "auth"

# Closed set of role values. The Python ``Role`` StrEnum lives in
# ``xauditor_portal.auth.roles`` — we duplicate the literal strings here
# (rather than importing them) to avoid a circular import: this module is
# loaded as part of ``db.models.auth``, which the ``auth`` package's
# ``deps.py`` imports for ``User``/``RevokedSession``. Importing back from
# ``auth.roles`` would force the partially-initialized ``auth`` package to
# run ``deps.py`` while ``db.models.auth`` is still loading. The drift
# safety net is the test in ``tests/auth/test_roles.py``, which asserts
# the literal set below matches the ``Role`` enum exactly.
_ROLE_VALUES: tuple[str, ...] = ("admin", "auditor", "viewer")
_ROLE_CHECK_SQL = "role IN (" + ", ".join(f"'{v}'" for v in _ROLE_VALUES) + ")"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username", name="uq_users_username"),
        CheckConstraint(_ROLE_CHECK_SQL, name="ck_users_role"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Role lives in a closed set enforced both in the Role StrEnum and the
    # ``ck_users_role`` CHECK constraint. Column-level default is the
    # least-privileged value so any Python insert path that forgets to set the
    # role fails closed; every production caller sets it explicitly.
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="viewer"
    )
    # When set, every JWT issued before this timestamp is rejected by
    # ``current_user``. The role-update and password-reset endpoints set this
    # to ``utcnow()`` so the affected user's existing cookies stop working
    # without enumerating outstanding ``jti`` values.
    sessions_invalid_before: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    # When set, the user is soft-disabled: ``current_user`` rejects every
    # request with HTTP 401 and ``login`` refuses new authentications with
    # HTTP 403. The "at least one enabled admin" invariant counts rows where
    # ``role = 'admin' AND disabled_at IS NULL``.
    disabled_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)

    password_history: Mapped[list["PasswordHistory"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="PasswordHistory.user_id",
    )
    revoked_sessions: Mapped[list["RevokedSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class PasswordHistory(Base):
    __tablename__ = "password_history"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"),
        nullable=False,
    )
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.users.id", ondelete="SET NULL"),
        nullable=True,
    )
    changed_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)

    user: Mapped[User] = relationship(
        back_populates="password_history", foreign_keys=[user_id]
    )


class RevokedSession(Base):
    __tablename__ = "revoked_sessions"
    __table_args__ = (
        UniqueConstraint("jti", name="uq_revoked_sessions_jti"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.users.id", ondelete="CASCADE"),
        nullable=False,
    )
    jti: Mapped[str] = mapped_column(String(128), nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)

    user: Mapped[User] = relationship(back_populates="revoked_sessions")


__all__ = ["PasswordHistory", "RevokedSession", "SCHEMA", "User"]
