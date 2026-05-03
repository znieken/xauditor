"""JWT encode / decode helpers and cookie plumbing."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Response

from xauditor_portal.auth.roles import ROLE_VALUES

COOKIE_NAME = "xauditor_portal_session"
JWT_ALGORITHM = "HS256"


class TokenError(ValueError):
    """Raised when a presented JWT is invalid or expired."""


@dataclass(frozen=True)
class TokenPayload:
    user_id: str
    username: str
    jti: str
    must_change_password: bool
    role: str
    issued_at: datetime
    expires_at: datetime


def encode_token(
    *,
    user_id: str,
    username: str,
    must_change_password: bool,
    role: str,
    secret: str,
    ttl_seconds: int,
    jti: str | None = None,
) -> tuple[str, TokenPayload]:
    if role not in ROLE_VALUES:
        raise ValueError(
            f"Cannot encode token with unknown role {role!r}; "
            f"expected one of {sorted(ROLE_VALUES)}"
        )
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=ttl_seconds)
    token_id = jti or str(uuid.uuid4())
    claims: dict[str, object] = {
        "sub": user_id,
        "username": username,
        "jti": token_id,
        "mcp": bool(must_change_password),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    token = jwt.encode(claims, secret, algorithm=JWT_ALGORITHM)
    payload = TokenPayload(
        user_id=user_id,
        username=username,
        jti=token_id,
        must_change_password=must_change_password,
        role=role,
        issued_at=now,
        expires_at=expires,
    )
    return token, payload


def decode_token(token: str, *, secret: str) -> TokenPayload:
    try:
        claims = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Session expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Invalid session token.") from exc
    try:
        role = str(claims["role"])
    except KeyError as exc:
        raise TokenError("Session token missing required claims.") from exc
    if role not in ROLE_VALUES:
        raise TokenError("Session token carries an unknown role.")
    try:
        return TokenPayload(
            user_id=str(claims["sub"]),
            username=str(claims["username"]),
            jti=str(claims["jti"]),
            must_change_password=bool(claims.get("mcp", False)),
            role=role,
            issued_at=datetime.fromtimestamp(int(claims["iat"]), tz=timezone.utc),
            expires_at=datetime.fromtimestamp(int(claims["exp"]), tz=timezone.utc),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenError("Session token missing required claims.") from exc


def set_session_cookie(
    response: Response, token: str, *, max_age_seconds: int, secure: bool = False
) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=secure,
        max_age=max_age_seconds,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


__all__ = [
    "COOKIE_NAME",
    "TokenError",
    "TokenPayload",
    "clear_session_cookie",
    "decode_token",
    "encode_token",
    "set_session_cookie",
]
