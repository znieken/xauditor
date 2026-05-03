"""Bcrypt password hashing and policy."""

from __future__ import annotations

import bcrypt


class PasswordPolicyError(ValueError):
    """Raised when a submitted password fails the enforced policy."""


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def validate_password_policy(plain: str, *, min_length: int = 12) -> None:
    if len(plain) < min_length:
        raise PasswordPolicyError(
            f"Password must be at least {min_length} characters long."
        )
    if not any(c.isalpha() for c in plain):
        raise PasswordPolicyError("Password must contain at least one letter.")
    if not any(c.isdigit() for c in plain):
        raise PasswordPolicyError("Password must contain at least one digit.")


__all__ = [
    "PasswordPolicyError",
    "hash_password",
    "validate_password_policy",
    "verify_password",
]
