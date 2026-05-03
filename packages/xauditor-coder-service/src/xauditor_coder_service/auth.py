"""Bearer-token auth dependency for the coder microservice.

Per spec D2 (and the explicit redesign in `add-coder-http-microservice`
D11): auth is a deliberate switch, not implicit from token presence.

- ``XAUDITOR_CODER_SERVICE_ENABLE_AUTH=true`` requires a non-empty
  ``XAUDITOR_CODER_SERVICE_TOKEN`` at startup; entrypoint fails to start
  otherwise. Every non-``/health`` request must carry the matching
  ``Authorization: Bearer <token>`` header — mismatches receive HTTP 401.
- ``XAUDITOR_CODER_SERVICE_ENABLE_AUTH=false`` (or unset) accepts every
  request unauthenticated. If a token is configured anyway, the entry
  point emits a ``WARNING`` so the operator notices the misconfig.
- ``/health`` is always reachable without the header, regardless.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping

from fastapi import Header, HTTPException, status


log = logging.getLogger("xauditor_coder_service.auth")


_TRUE_VALUES = ("true", "1", "yes", "on")
_FALSE_VALUES = ("false", "0", "no", "off", "")


def parse_bool(value: str | None) -> bool:
    """Same parsing as ``XAUDITOR_CODER_ENABLED`` on the xauditor side."""

    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(
        f"Invalid boolean env value `{value}`; expected one of "
        f"{_TRUE_VALUES + _FALSE_VALUES}."
    )


@dataclass(frozen=True)
class AuthSettings:
    enable_auth: bool
    token: str

    def __repr__(self) -> str:
        # Never print the token in logs / crash dumps.
        return f"AuthSettings(enable_auth={self.enable_auth!r}, token='***' if set else '')"


def load_auth_settings(env: Mapping[str, str]) -> AuthSettings:
    """Read ``ENABLE_AUTH`` + ``TOKEN`` from the environment.

    Validates the operator's intent at startup time (the entrypoint
    calls this BEFORE binding any socket). Raises ``RuntimeError`` if
    auth is enabled with no token; the caller should print the message
    to stderr and exit non-zero.
    """

    raw_enable = env.get("XAUDITOR_CODER_SERVICE_ENABLE_AUTH")
    enable_auth = parse_bool(raw_enable)
    token = (env.get("XAUDITOR_CODER_SERVICE_TOKEN") or "").strip()
    if enable_auth and not token:
        raise RuntimeError(
            "XAUDITOR_CODER_SERVICE_ENABLE_AUTH=true but "
            "XAUDITOR_CODER_SERVICE_TOKEN is empty. Either set the token, "
            "or set XAUDITOR_CODER_SERVICE_ENABLE_AUTH=false."
        )
    if not enable_auth and token:
        log.warning(
            "XAUDITOR_CODER_SERVICE_TOKEN is configured but auth is "
            "disabled (XAUDITOR_CODER_SERVICE_ENABLE_AUTH is not 'true'); "
            "the token will be ignored. To enable auth, set "
            "XAUDITOR_CODER_SERVICE_ENABLE_AUTH=true."
        )
    return AuthSettings(enable_auth=enable_auth, token=token)


def make_auth_dependency(settings: AuthSettings):
    """Return a FastAPI dependency that enforces (or skips) bearer auth.

    Apply via ``Depends(make_auth_dependency(settings))`` on every route
    that should be gated. Do NOT apply to ``/health`` (the spec mandates
    it remains unauth in every configuration).
    """

    async def _check(authorization: str | None = Header(default=None)) -> None:
        if not settings.enable_auth:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing Authorization: Bearer <token> header.",
            )
        supplied = authorization[len("Bearer "):].strip()
        if supplied != settings.token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Bearer token mismatch.",
            )

    return _check


__all__ = [
    "AuthSettings",
    "load_auth_settings",
    "make_auth_dependency",
    "parse_bool",
]
