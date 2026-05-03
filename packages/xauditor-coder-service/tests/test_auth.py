from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor_coder_service.auth import (
    AuthSettings,
    load_auth_settings,
    parse_bool,
)


class ParseBoolTests(unittest.TestCase):
    def test_true_variants(self) -> None:
        for v in ("true", "1", "yes", "on", "TRUE", "Yes"):
            self.assertTrue(parse_bool(v), v)

    def test_false_variants(self) -> None:
        for v in ("false", "0", "no", "off", "", "FALSE"):
            self.assertFalse(parse_bool(v), v)

    def test_none_is_false(self) -> None:
        self.assertFalse(parse_bool(None))

    def test_garbage_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_bool("maybe")


class LoadAuthSettingsTests(unittest.TestCase):
    def test_default_disables_auth(self) -> None:
        settings = load_auth_settings({})
        self.assertFalse(settings.enable_auth)
        self.assertEqual(settings.token, "")

    def test_enable_auth_with_token_succeeds(self) -> None:
        settings = load_auth_settings(
            {
                "XAUDITOR_CODER_SERVICE_ENABLE_AUTH": "true",
                "XAUDITOR_CODER_SERVICE_TOKEN": "secret-abc",
            }
        )
        self.assertTrue(settings.enable_auth)
        self.assertEqual(settings.token, "secret-abc")

    def test_enable_auth_without_token_raises(self) -> None:
        with self.assertRaises(RuntimeError) as ctx:
            load_auth_settings({"XAUDITOR_CODER_SERVICE_ENABLE_AUTH": "true"})
        message = str(ctx.exception)
        self.assertIn("XAUDITOR_CODER_SERVICE_TOKEN", message)
        self.assertIn("XAUDITOR_CODER_SERVICE_ENABLE_AUTH", message)

    def test_repr_redacts_token(self) -> None:
        settings = AuthSettings(enable_auth=True, token="sekret")
        self.assertNotIn("sekret", repr(settings))


if __name__ == "__main__":
    unittest.main()
