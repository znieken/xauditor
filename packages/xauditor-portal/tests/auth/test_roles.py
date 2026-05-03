"""Verify the ``Role`` enum and the DB CHECK constraint enumerate the
same set of values, so adding a new role does not silently drift between
the Python and SQL layers.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from xauditor_portal.auth.roles import ROLE_VALUES, Role, coerce_role
from xauditor_portal.db.models.auth.tables import _ROLE_CHECK_SQL, _ROLE_VALUES


_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "xauditor_portal"
    / "db"
    / "migrations"
    / "versions"
    / "0008_users_role.py"
)


def _extract_check_values(sql: str) -> set[str]:
    matches = re.findall(r"'([a-zA-Z]+)'", sql)
    return set(matches)


class RoleEnumTests(unittest.TestCase):
    def test_enum_has_three_known_members(self) -> None:
        self.assertEqual(
            {member.value for member in Role},
            {"admin", "auditor", "viewer"},
        )

    def test_role_values_frozenset_matches_enum(self) -> None:
        self.assertEqual(ROLE_VALUES, {member.value for member in Role})

    def test_coerce_role_accepts_known_values(self) -> None:
        for member in Role:
            self.assertEqual(coerce_role(member.value), member)

    def test_coerce_role_rejects_unknown_values(self) -> None:
        with self.assertRaises(ValueError):
            coerce_role("superuser")


class RoleCheckConstraintParityTests(unittest.TestCase):
    """The Python enum and the DB CHECK definition must enumerate the same
    set of role strings. If they drift, a CHECK violation could fire
    against a perfectly legal-looking Python value."""

    def test_table_check_definition_matches_enum(self) -> None:
        check_values = _extract_check_values(_ROLE_CHECK_SQL)
        self.assertEqual(check_values, {member.value for member in Role})

    def test_tables_role_values_tuple_matches_enum(self) -> None:
        # ``tables.py`` duplicates the literal role strings to avoid a
        # circular import. This test is the drift safety net.
        self.assertEqual(set(_ROLE_VALUES), {member.value for member in Role})

    def test_alembic_migration_check_matches_enum(self) -> None:
        sql = _MIGRATION_PATH.read_text(encoding="utf-8")
        # Pull the CHECK clause from the upgrade body and parse its values.
        match = re.search(r"role IN \(([^)]+)\)", sql)
        self.assertIsNotNone(match, "Expected role IN (...) clause in migration")
        assert match is not None
        check_values = _extract_check_values(match.group(1))
        self.assertEqual(check_values, {member.value for member in Role})


if __name__ == "__main__":
    unittest.main()
