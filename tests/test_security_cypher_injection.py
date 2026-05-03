from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.integrations.neo4j import _cypher_quote


class CypherQuoteEscapingTests(unittest.TestCase):
    """Regression tests that lock down Cypher string-literal escaping.

    These are the last line of defense before a refactor to parameterized
    UNWIND queries lands (tracked as task 10.1). Until then, repository
    inputs are still interpolated into Cypher strings, so every character
    that could close the literal or insert a comment boundary must be
    escaped.
    """

    def test_single_quote_is_escaped(self) -> None:
        self.assertEqual(_cypher_quote("O'Reilly"), "O\\'Reilly")

    def test_backslash_is_escaped(self) -> None:
        self.assertEqual(_cypher_quote("a\\b"), "a\\\\b")

    def test_double_quote_is_escaped(self) -> None:
        self.assertEqual(_cypher_quote('he said "hi"'), 'he said \\"hi\\"')

    def test_newline_is_escaped(self) -> None:
        self.assertEqual(_cypher_quote("line1\nline2"), "line1\\nline2")

    def test_carriage_return_is_escaped(self) -> None:
        self.assertEqual(_cypher_quote("a\rb"), "a\\rb")

    def test_null_byte_is_escaped(self) -> None:
        self.assertEqual(_cypher_quote("a\0b"), "a\\u0000b")

    def test_malicious_file_path_is_rendered_literal(self) -> None:
        payload = "app.py'); MATCH (n) DETACH DELETE n; //"
        escaped = _cypher_quote(payload)
        self.assertNotIn("app.py');", escaped)
        self.assertIn("app.py\\');", escaped)
        cypher = f"MERGE (f:File {{path: '{escaped}'}});"
        unescaped_quotes = sum(
            1
            for index, ch in enumerate(cypher)
            if ch == "'" and (index == 0 or cypher[index - 1] != "\\")
        )
        self.assertEqual(unescaped_quotes, 2)

    def test_newline_injection_cannot_start_new_statement(self) -> None:
        payload = "legit\nMATCH (n) DETACH DELETE n;//"
        escaped = _cypher_quote(payload)
        self.assertNotIn("\n", escaped)
        self.assertIn("\\n", escaped)


if __name__ == "__main__":
    unittest.main()
