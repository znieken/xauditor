from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import jwt

from xauditor_portal.auth.roles import Role
from xauditor_portal.auth.tokens import (
    TokenError,
    decode_token,
    encode_token,
)


SECRET = "test-secret-do-not-use-in-prod"


def _encode(
    *,
    user_id: str = "user-1",
    username: str = "alice",
    must_change_password: bool = False,
    role: str = Role.ADMIN.value,
    ttl_seconds: int = 3600,
) -> tuple[str, object]:
    return encode_token(
        user_id=user_id,
        username=username,
        must_change_password=must_change_password,
        role=role,
        secret=SECRET,
        ttl_seconds=ttl_seconds,
    )


class EncodeDecodeTests(unittest.TestCase):
    def test_round_trip_preserves_claims(self) -> None:
        token, payload = _encode(
            user_id="user-1",
            username="alice",
            must_change_password=True,
            role=Role.AUDITOR.value,
        )
        decoded = decode_token(token, secret=SECRET)
        self.assertEqual(decoded.user_id, "user-1")
        self.assertEqual(decoded.username, "alice")
        self.assertEqual(decoded.jti, payload.jti)  # type: ignore[attr-defined]
        self.assertTrue(decoded.must_change_password)
        self.assertEqual(decoded.role, Role.AUDITOR.value)

    def test_round_trip_admin_and_viewer_roles(self) -> None:
        for role in (Role.ADMIN.value, Role.VIEWER.value):
            token, _ = _encode(role=role)
            decoded = decode_token(token, secret=SECRET)
            self.assertEqual(decoded.role, role)

    def test_unknown_role_is_rejected_at_encode(self) -> None:
        with self.assertRaises(ValueError):
            encode_token(
                user_id="user-1",
                username="alice",
                must_change_password=False,
                role="superuser",
                secret=SECRET,
                ttl_seconds=3600,
            )

    def test_unknown_role_in_token_is_rejected_at_decode(self) -> None:
        now = datetime.now(timezone.utc)
        forged = jwt.encode(
            {
                "sub": "user-1",
                "username": "bob",
                "jti": "x",
                "mcp": False,
                "role": "superuser",
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(seconds=60)).timestamp()),
            },
            SECRET,
            algorithm="HS256",
        )
        with self.assertRaises(TokenError):
            decode_token(forged, secret=SECRET)

    def test_missing_role_claim_is_rejected(self) -> None:
        now = datetime.now(timezone.utc)
        bare = jwt.encode(
            {
                "sub": "user-1",
                "username": "bob",
                "jti": "x",
                "mcp": False,
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(seconds=60)).timestamp()),
            },
            SECRET,
            algorithm="HS256",
        )
        with self.assertRaises(TokenError):
            decode_token(bare, secret=SECRET)

    def test_expired_token_is_rejected(self) -> None:
        token, _ = _encode(ttl_seconds=-10)
        with self.assertRaises(TokenError):
            decode_token(token, secret=SECRET)

    def test_wrong_secret_is_rejected(self) -> None:
        token, _ = _encode()
        with self.assertRaises(TokenError):
            decode_token(token, secret="different-secret")

    def test_tampered_token_is_rejected(self) -> None:
        token, _ = _encode()
        head, body, _sig = token.split(".")
        bogus_body = body[:-2] + "AA"
        tampered = f"{head}.{bogus_body}.{_sig}"
        with self.assertRaises(TokenError):
            decode_token(tampered, secret=SECRET)

    def test_missing_claim_raises_tokenerror(self) -> None:
        now = datetime.now(timezone.utc)
        bare = jwt.encode(
            {
                "sub": "user-1",
                "role": Role.ADMIN.value,
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(seconds=60)).timestamp()),
            },
            SECRET,
            algorithm="HS256",
        )
        with self.assertRaises(TokenError):
            decode_token(bare, secret=SECRET)

    def test_distinct_jti_per_issuance(self) -> None:
        _, p1 = _encode()
        _, p2 = _encode()
        self.assertNotEqual(p1.jti, p2.jti)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
