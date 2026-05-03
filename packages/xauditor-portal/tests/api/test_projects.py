from __future__ import annotations

import unittest

from xauditor_portal.api.projects import project_key


class ProjectKeyTests(unittest.TestCase):
    def test_returns_24_hex_characters(self) -> None:
        key = project_key("/tmp/repo", "repo")
        self.assertEqual(len(key), 24)
        self.assertTrue(all(c in "0123456789abcdef" for c in key))

    def test_is_deterministic_for_same_inputs(self) -> None:
        a = project_key("/tmp/repo", "repo")
        b = project_key("/tmp/repo", "repo")
        self.assertEqual(a, b)

    def test_is_sensitive_to_repo_root(self) -> None:
        a = project_key("/tmp/repo", "repo")
        b = project_key("/tmp/other-repo", "repo")
        self.assertNotEqual(a, b)

    def test_is_sensitive_to_project_name(self) -> None:
        a = project_key("/tmp/repo", "repo")
        b = project_key("/tmp/repo", "other-repo")
        self.assertNotEqual(a, b)

    def test_null_byte_separator_prevents_boundary_collisions(self) -> None:
        """The null byte in the hash input prevents ``"a"+"bc"`` and
        ``"ab"+"c"`` from colliding even if both would otherwise produce
        the same concatenated byte string."""

        a = project_key("a", "bc")
        b = project_key("ab", "c")
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
