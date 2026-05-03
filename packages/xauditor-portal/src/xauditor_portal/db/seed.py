"""Idempotent data seeding for the portal database."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from xauditor_portal.auth.roles import Role
from xauditor_portal.db.models.auth import User


DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"
DEFAULT_ROLE = Role.ADMIN.value


def _hash_default_password() -> str:
    # bcrypt is a hard dependency of xauditor-portal; defer the import so that
    # structural tests (which never call into seeding) can run without it.
    import bcrypt

    return bcrypt.hashpw(DEFAULT_PASSWORD.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def seed_default_user(session: Session) -> User | None:
    """Create the default admin user iff `auth.users` is empty.

    Returns the created user, or `None` when the table already has at least
    one row. Running this against a populated table is a no-op.
    """

    existing = session.execute(select(User).limit(1)).scalar_one_or_none()
    if existing is not None:
        return None
    user = User(
        username=DEFAULT_USERNAME,
        password_hash=_hash_default_password(),
        must_change_password=True,
        role=DEFAULT_ROLE,
    )
    session.add(user)
    session.flush()
    return user


__all__ = ["DEFAULT_PASSWORD", "DEFAULT_ROLE", "DEFAULT_USERNAME", "seed_default_user"]
