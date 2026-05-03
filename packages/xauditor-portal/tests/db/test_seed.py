from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from xauditor_portal.auth.roles import Role
from xauditor_portal.db.models.auth import User
from xauditor_portal.db.seed import (
    DEFAULT_PASSWORD,
    DEFAULT_ROLE,
    DEFAULT_USERNAME,
    seed_default_user,
)


class SeedDefaultUserTests(unittest.TestCase):
    def _make_session(self, existing: User | None) -> MagicMock:
        session = MagicMock()
        scalar = MagicMock()
        scalar.scalar_one_or_none.return_value = existing
        session.execute.return_value = scalar
        session.add = MagicMock()
        session.flush = MagicMock()
        return session

    def test_default_seed_username_and_role_are_admin(self) -> None:
        self.assertEqual(DEFAULT_USERNAME, "admin")
        self.assertEqual(DEFAULT_PASSWORD, "admin")
        self.assertEqual(DEFAULT_ROLE, Role.ADMIN.value)

    def test_seed_creates_user_when_table_is_empty(self) -> None:
        session = self._make_session(existing=None)
        user = seed_default_user(session)
        self.assertIsNotNone(user)
        assert user is not None
        self.assertEqual(user.username, DEFAULT_USERNAME)
        self.assertEqual(user.role, Role.ADMIN.value)
        self.assertTrue(user.must_change_password)
        self.assertTrue(user.password_hash)
        self.assertNotEqual(user.password_hash, DEFAULT_PASSWORD)
        session.add.assert_called_once_with(user)
        session.flush.assert_called_once()

    def test_seed_is_noop_when_table_already_has_a_user(self) -> None:
        existing = User(
            username="someone",
            password_hash="x",
            must_change_password=False,
            role=Role.ADMIN.value,
        )
        session = self._make_session(existing=existing)
        result = seed_default_user(session)
        self.assertIsNone(result)
        session.add.assert_not_called()
        session.flush.assert_not_called()

    def test_default_password_is_bcrypt_hashed(self) -> None:
        import bcrypt

        session = self._make_session(existing=None)
        user = seed_default_user(session)
        assert user is not None
        self.assertTrue(
            bcrypt.checkpw(DEFAULT_PASSWORD.encode("utf-8"), user.password_hash.encode("utf-8"))
        )


if __name__ == "__main__":
    unittest.main()
