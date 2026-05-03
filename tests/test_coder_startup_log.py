"""Tests for the one-line ``Coder transport: ...`` startup INFO log.

The helper itself returns a string (or None); this test asserts the
text shape, the absence of token values, and the conditions under which
the line is suppressed (subprocess transport, coder disabled).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.coder import startup_log_line
from xauditor.config import CoderConfig


class StartupLogLineTests(unittest.TestCase):
    def test_disabled_returns_none(self) -> None:
        self.assertIsNone(startup_log_line(CoderConfig(enabled=False)))

    def test_subprocess_transport_returns_none(self) -> None:
        cfg = CoderConfig(enabled=True, transport="subprocess")
        self.assertIsNone(startup_log_line(cfg))

    def test_http_with_auth_enabled(self) -> None:
        cfg = CoderConfig(
            enabled=True,
            transport="http",
            endpoint="https://coder-pool.internal/",
            enable_auth=True,
            endpoint_token="secret-abc",
        )
        line = startup_log_line(cfg)
        self.assertEqual(
            line,
            "Coder transport: http via https://coder-pool.internal/ (auth: enabled)",
        )
        # Token never appears in the line.
        self.assertNotIn("secret-abc", line or "")

    def test_http_with_auth_disabled(self) -> None:
        cfg = CoderConfig(
            enabled=True,
            transport="http",
            endpoint="http://coder.prod.internal:8090",
            enable_auth=False,
        )
        line = startup_log_line(cfg)
        self.assertEqual(
            line,
            "Coder transport: http via http://coder.prod.internal:8090 (auth: disabled)",
        )

    def test_http_with_unset_endpoint_still_emits_line(self) -> None:
        # Defensive: if validation slipped (shouldn't), the line still
        # describes the runtime state honestly.
        cfg = CoderConfig(enabled=True, transport="http", endpoint="")
        line = startup_log_line(cfg)
        assert line is not None
        self.assertIn("<unset>", line)


if __name__ == "__main__":
    unittest.main()
