"""Model factory threads request_timeout_seconds to the SDK constructor.

Per kind:
- ``kind: openai`` → ``ChatOpenAI(request_timeout=<value>)``
- ``kind: anthropic`` → ``ChatAnthropic(default_request_timeout=<value>)``

When unset, neither kwarg is passed (SDK default applies).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.config import (
    LLMConfig,
    PROVIDER_KIND_ANTHROPIC,
    PROVIDER_KIND_OPENAI,
)
from xauditor.model_factory import (
    AnthropicLangChainChatModel,
    LangChainChatModel,
    SamplingParams,
    build_chat_model,
)


def _capture_openai_kwargs(provider: LLMConfig, **build_kwargs) -> dict:
    captured: dict = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return MagicMock(name="chat_openai")

    with patch(
        "xauditor.model_factory.ChatOpenAI", side_effect=_fake
    ), patch("xauditor.model_factory.LANGCHAIN_OPENAI_AVAILABLE", True):
        chat_model = build_chat_model(provider, provider_name="p", **build_kwargs)
        chat_model._chat()
    return captured


def _capture_anthropic_kwargs(provider: LLMConfig, **build_kwargs) -> dict:
    captured: dict = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return MagicMock(name="chat_anthropic")

    with patch(
        "xauditor.model_factory.ChatAnthropic", side_effect=_fake
    ), patch("xauditor.model_factory.LANGCHAIN_ANTHROPIC_AVAILABLE", True):
        chat_model = build_chat_model(provider, provider_name="p", **build_kwargs)
        chat_model._chat()
    return captured


class OpenAITimeoutDispatchTests(unittest.TestCase):
    def test_explicit_timeout_lands_on_request_timeout_kwarg(self) -> None:
        provider = LLMConfig(
            base_url="https://api.example/v1",
            api_key="k",
            model_name="m",
            kind=PROVIDER_KIND_OPENAI,
            request_timeout_seconds=1800.0,
        )
        captured = _capture_openai_kwargs(provider)
        self.assertEqual(captured.get("request_timeout"), 1800.0)

    def test_unset_timeout_omits_request_timeout_kwarg(self) -> None:
        provider = LLMConfig(
            base_url="https://api.example/v1",
            api_key="k",
            model_name="m",
            kind=PROVIDER_KIND_OPENAI,
            # request_timeout_seconds left at None
        )
        captured = _capture_openai_kwargs(provider)
        self.assertNotIn("request_timeout", captured)

    def test_explicit_override_wins_over_provider_value(self) -> None:
        provider = LLMConfig(
            base_url="https://api.example/v1",
            api_key="k",
            model_name="m",
            request_timeout_seconds=1800.0,
        )
        captured = _capture_openai_kwargs(
            provider, request_timeout_seconds=600.0
        )
        self.assertEqual(captured.get("request_timeout"), 600.0)


class AnthropicTimeoutDispatchTests(unittest.TestCase):
    def test_explicit_timeout_lands_on_default_request_timeout_kwarg(self) -> None:
        provider = LLMConfig(
            base_url="https://api.anthropic.com",
            api_key="sk-ant-fake",
            model_name="claude-sonnet-4-5",
            kind=PROVIDER_KIND_ANTHROPIC,
            request_timeout_seconds=1800.0,
        )
        captured = _capture_anthropic_kwargs(provider)
        self.assertEqual(captured.get("default_request_timeout"), 1800.0)
        # OpenAI's kwarg name SHALL NOT leak into the Anthropic call.
        self.assertNotIn("request_timeout", captured)

    def test_unset_timeout_omits_default_request_timeout_kwarg(self) -> None:
        provider = LLMConfig(
            base_url="https://api.anthropic.com",
            api_key="sk-ant-fake",
            model_name="claude-sonnet-4-5",
            kind=PROVIDER_KIND_ANTHROPIC,
        )
        captured = _capture_anthropic_kwargs(provider)
        self.assertNotIn("default_request_timeout", captured)


class BuildChatModelDataclassTests(unittest.TestCase):
    """Verify the timeout reaches the wrapping dataclass instance."""

    def test_openai_wrapper_exposes_timeout_field(self) -> None:
        provider = LLMConfig(
            base_url="https://api.example/v1",
            api_key="k",
            model_name="m",
            request_timeout_seconds=1234.0,
        )
        model = build_chat_model(provider, provider_name="p")
        self.assertIsInstance(model, LangChainChatModel)
        self.assertEqual(model.request_timeout_seconds, 1234.0)

    def test_anthropic_wrapper_exposes_timeout_field(self) -> None:
        provider = LLMConfig(
            base_url="https://api.anthropic.com",
            api_key="sk-ant-fake",
            model_name="claude-sonnet-4-5",
            kind=PROVIDER_KIND_ANTHROPIC,
            request_timeout_seconds=4321.0,
        )
        model = build_chat_model(provider, provider_name="p")
        self.assertIsInstance(model, AnthropicLangChainChatModel)
        self.assertEqual(model.request_timeout_seconds, 4321.0)


if __name__ == "__main__":
    unittest.main()
