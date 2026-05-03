from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.config import LLMConfig
from xauditor.errors import SecretNotFoundError
from xauditor.security.secrets import SecretProvider


class SecretProviderTests(unittest.TestCase):
    def test_explicit_value_wins(self) -> None:
        provider = SecretProvider(env={"XAUDITOR_LLM_API_KEY": "env-key"})
        self.assertEqual(provider.get("llm_api_key", explicit="explicit-key"), "explicit-key")

    def test_prefers_env_over_keyring(self) -> None:
        keyring_stub = mock.Mock()
        keyring_stub.get_password.return_value = "keyring-key"
        provider = SecretProvider(
            env={"XAUDITOR_LLM_API_KEY": "env-key"},
            keyring_backend=keyring_stub,
        )
        self.assertEqual(provider.get("llm_api_key"), "env-key")
        keyring_stub.get_password.assert_not_called()

    def test_reads_from_keyring_when_env_missing(self) -> None:
        keyring_stub = mock.Mock()
        keyring_stub.get_password.return_value = "keyring-key"
        provider = SecretProvider(env={}, keyring_backend=keyring_stub)
        self.assertEqual(provider.get("llm_api_key"), "keyring-key")
        keyring_stub.get_password.assert_called_once_with("xauditor", "llm_api_key")

    def test_raises_when_nothing_resolves(self) -> None:
        provider = SecretProvider(env={}, keyring_backend=None)
        with self.assertRaises(SecretNotFoundError) as ctx:
            provider.get("llm_api_key")
        self.assertEqual(ctx.exception.name, "llm_api_key")

    def test_ignores_empty_env_values(self) -> None:
        keyring_stub = mock.Mock()
        keyring_stub.get_password.return_value = "keyring-key"
        provider = SecretProvider(
            env={"XAUDITOR_LLM_API_KEY": "   "},
            keyring_backend=keyring_stub,
        )
        self.assertEqual(provider.get("llm_api_key"), "keyring-key")

    def test_env_var_name_normalizes_dotted_keys(self) -> None:
        provider = SecretProvider(env={"XAUDITOR_LLM_PROVIDERS_OPENAI_API_KEY": "v"})
        self.assertEqual(provider.get("llm.providers.openai.api_key"), "v")

    def test_resolved_values_tracks_cache(self) -> None:
        provider = SecretProvider(env={"XAUDITOR_A": "alpha", "XAUDITOR_B": "bravo"})
        provider.get("a")
        provider.get("b")
        self.assertEqual(set(provider.resolved_values()), {"alpha", "bravo"})


class LLMConfigRedactionTests(unittest.TestCase):
    def test_repr_redacts_api_key(self) -> None:
        cfg = LLMConfig(
            base_url="https://example/v1",
            api_key="sk-supersecret",
            model_name="gpt-4",
        )
        rendered = repr(cfg)
        self.assertNotIn("sk-supersecret", rendered)
        self.assertIn("api_key='***'", rendered)
        self.assertIn("base_url='https://example/v1'", rendered)

    def test_repr_omits_redaction_when_empty(self) -> None:
        cfg = LLMConfig(base_url="https://example/v1", api_key="", model_name="gpt-4")
        rendered = repr(cfg)
        self.assertIn("api_key=''", rendered)


if __name__ == "__main__":
    unittest.main()
