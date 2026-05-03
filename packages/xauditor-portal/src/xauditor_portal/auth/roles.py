"""Role-based access-control role enumeration.

The set of role values is closed and small. The same string is used in
Python, JSON, and the database CHECK constraint so there is no need to
serialize/deserialize between layers.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    AUDITOR = "auditor"
    VIEWER = "viewer"


ROLE_VALUES: frozenset[str] = frozenset(role.value for role in Role)


def coerce_role(value: str) -> Role:
    """Return the ``Role`` member matching ``value`` or raise ``ValueError``.

    Used at API boundaries to reject role strings that drift from the closed
    set without trusting the caller.
    """

    try:
        return Role(value)
    except ValueError as exc:
        raise ValueError(
            f"Unknown role {value!r}; expected one of {sorted(ROLE_VALUES)}"
        ) from exc


__all__ = ["ROLE_VALUES", "Role", "coerce_role"]
