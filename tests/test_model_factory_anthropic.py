"""Model-factory dispatch + Anthropic-specific behavior tests.

Covers `add-anthropic-provider-kind`: dispatch by kind, sampling-field
mapping, repetition_penalty warning, thinking-enabled kwarg shape, and
the missing-package preflight error.
"""

from __future__ import annotations

import logging
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
from xauditor.errors import PreflightError
from xauditor.model_factory import (
    AnthropicLangChainChatModel,
    LangChainChatModel,
    SamplingParams,
    build_chat_model,
    ensure_provider_runtime_available,
)


def _real_anthropic_provider(**overrides) -> LLMConfig:
    base = dict(
        base_url="https://api.anthropic.com",
        api_key="sk-ant-fake",
        model_name="claude-sonnet-4-5",
        kind=PROVIDER_KIND_ANTHROPIC,
    )
    base.update(overrides)
    return LLMConfig(**base)


def _real_openai_provider(**overrides) -> LLMConfig:
    base = dict(
        base_url="https://api.openai.example/v1",
        api_key="sk-fake",
        model_name="gpt-test",
        kind=PROVIDER_KIND_OPENAI,
    )
    base.update(overrides)
    return LLMConfig(**base)


class BuildChatModelDispatchTests(unittest.TestCase):
    def test_anthropic_kind_returns_anthropic_chat_model(self) -> None:
        provider = _real_anthropic_provider()
        model = build_chat_model(provider, provider_name="p1")
        self.assertIsInstance(model, AnthropicLangChainChatModel)
        self.assertEqual(model.provider_name, "p1")
        self.assertEqual(model.model_name, "claude-sonnet-4-5")

    def test_openai_kind_returns_openai_chat_model(self) -> None:
        provider = _real_openai_provider()
        model = build_chat_model(provider, provider_name="p1")
        self.assertIsInstance(model, LangChainChatModel)
        # Sanity: the OpenAI path SHALL NOT accidentally land on the
        # Anthropic class.
        self.assertNotIsInstance(model, AnthropicLangChainChatModel)

    def test_default_kind_in_dataclass_is_openai(self) -> None:
        # Constructing LLMConfig without an explicit kind preserves the
        # 0.5.0 behavior: openai backend.
        provider = LLMConfig(
            base_url="https://api.example/v1",
            api_key="k",
            model_name="m",
        )
        self.assertEqual(provider.kind, PROVIDER_KIND_OPENAI)
        self.assertIsInstance(
            build_chat_model(provider, provider_name="default"),
            LangChainChatModel,
        )

    def test_mock_url_bypasses_kind_dispatch(self) -> None:
        provider = LLMConfig(
            base_url="mock://offline",
            api_key="k",
            model_name="m",
            kind=PROVIDER_KIND_ANTHROPIC,
        )
        model = build_chat_model(provider, provider_name="p1")
        # Mock path SHALL NOT depend on kind — both kinds use the in-process
        # MockChatModel for offline tests.
        self.assertEqual(type(model).__name__, "MockChatModel")


class AnthropicChatModelKwargsTests(unittest.TestCase):
    """Inspect the kwargs passed to ``ChatAnthropic`` constructor."""

    def _build_chat_capturing_kwargs(
        self,
        *,
        sampling: SamplingParams,
        thinking_enabled: bool = False,
        thinking_effort: str | None = None,
    ) -> dict:
        captured: dict = {}

        def _fake_chat_anthropic(**kwargs):
            captured.update(kwargs)
            return MagicMock(name="chat_anthropic")

        with patch(
            "xauditor.model_factory.ChatAnthropic",
            side_effect=_fake_chat_anthropic,
        ), patch("xauditor.model_factory.LANGCHAIN_ANTHROPIC_AVAILABLE", True):
            model = AnthropicLangChainChatModel(
                provider_name="p1",
                model_name="claude-sonnet-4-5",
                base_url="https://api.anthropic.com",
                api_key="sk-ant-fake",
                thinking_enabled=thinking_enabled,
                thinking_effort=thinking_effort,
                sampling=sampling,
            )
            model._chat()
        return captured

    def test_sampling_fields_routed_top_level(self) -> None:
        captured = self._build_chat_capturing_kwargs(
            sampling=SamplingParams(temperature=0.0, top_p=0.95, top_k=64)
        )
        self.assertEqual(captured["temperature"], 0.0)
        self.assertEqual(captured["top_p"], 0.95)
        # top_k SHALL be top-level for Anthropic (NOT in extra_body).
        self.assertEqual(captured["top_k"], 64)
        self.assertNotIn("extra_body", captured)
        self.assertEqual(captured["model"], "claude-sonnet-4-5")
        self.assertEqual(captured["anthropic_api_url"], "https://api.anthropic.com")

    def test_thinking_enabled_sets_thinking_kwarg(self) -> None:
        # Legacy path: thinking_enabled=True without thinking_effort
        # falls back to the deprecated `thinking={...}` + budget shape.
        captured: dict = {}

        def _fake(**kwargs):
            captured.update(kwargs)
            return MagicMock(name="chat_anthropic")

        with patch(
            "xauditor.model_factory.ChatAnthropic", side_effect=_fake
        ), patch("xauditor.model_factory.LANGCHAIN_ANTHROPIC_AVAILABLE", True):
            model = AnthropicLangChainChatModel(
                provider_name="p1",
                model_name="claude-sonnet-4-7",
                base_url="https://api.anthropic.com",
                api_key="sk-ant-fake",
                thinking_enabled=True,
                sampling=SamplingParams(temperature=0.0),
            )
            with self.assertLogs("xauditor.model_factory", level=logging.WARNING) as cm:
                model._chat()
        self.assertIn("thinking", captured)
        self.assertEqual(captured["thinking"]["type"], "enabled")
        self.assertGreater(captured["thinking"]["budget_tokens"], 0)
        # max_tokens MUST be set (Anthropic requires it when thinking is on)
        # and MUST exceed the thinking budget.
        self.assertIn("max_tokens", captured)
        self.assertGreater(
            captured["max_tokens"], captured["thinking"]["budget_tokens"]
        )
        self.assertNotIn("effort", captured)
        # Deprecation warning fires once recommending thinking_effort.
        self.assertIn("thinking_effort", "\n".join(cm.output))

    def test_thinking_effort_routes_to_effort_kwarg(self) -> None:
        captured = self._build_chat_capturing_kwargs(
            sampling=SamplingParams(),
            thinking_enabled=False,
            thinking_effort="high",
        )
        # New path: thinking_effort lands as ChatAnthropic's `effort=`
        # kwarg; legacy `thinking={...}` shape SHALL NOT be sent.
        self.assertEqual(captured.get("effort"), "high")
        self.assertNotIn("thinking", captured)

    def test_thinking_effort_pins_max_tokens_floor(self) -> None:
        # Regression: without an explicit max_tokens, langchain-anthropic
        # 1.4.x falls back to 4096 for models without a profile entry
        # (e.g. preview models like `claude-mythos-preview`). Under
        # extended thinking that 4K gets fully consumed by reasoning,
        # the response carries only `type:"thinking"` parts, and
        # downstream `invoke_json` blows up on `json.loads("")`. The
        # effort branch MUST pin a generous max_tokens floor.
        from xauditor.model_factory import _ANTHROPIC_EFFORT_MAX_TOKENS_FLOOR

        captured = self._build_chat_capturing_kwargs(
            sampling=SamplingParams(),
            thinking_enabled=False,
            thinking_effort="high",
        )
        self.assertEqual(
            captured.get("max_tokens"), _ANTHROPIC_EFFORT_MAX_TOKENS_FLOOR
        )

    def test_thinking_effort_takes_precedence_over_thinking_enabled(self) -> None:
        # When BOTH are set, thinking_effort wins (it's the
        # forward-compatible API surface) — and no deprecation warning.
        captured: dict = {}

        def _fake(**kwargs):
            captured.update(kwargs)
            return MagicMock(name="chat_anthropic")

        with patch(
            "xauditor.model_factory.ChatAnthropic", side_effect=_fake
        ), patch("xauditor.model_factory.LANGCHAIN_ANTHROPIC_AVAILABLE", True):
            model = AnthropicLangChainChatModel(
                provider_name="p1",
                model_name="claude-sonnet-4-7",
                base_url="https://api.anthropic.com",
                api_key="sk-ant-fake",
                thinking_enabled=True,
                thinking_effort="medium",
                sampling=SamplingParams(),
            )
            with self.assertNoLogs("xauditor.model_factory", level=logging.WARNING):
                model._chat()
        self.assertEqual(captured.get("effort"), "medium")
        self.assertNotIn("thinking", captured)

    def test_thinking_disabled_omits_thinking_kwarg(self) -> None:
        captured = self._build_chat_capturing_kwargs(
            sampling=SamplingParams(),
            thinking_enabled=False,
        )
        self.assertNotIn("thinking", captured)
        self.assertNotIn("effort", captured)

    def test_repetition_penalty_dropped_with_warning(self) -> None:
        captured: dict = {}

        def _fake_chat_anthropic(**kwargs):
            captured.update(kwargs)
            return MagicMock(name="chat_anthropic")

        with patch(
            "xauditor.model_factory.ChatAnthropic",
            side_effect=_fake_chat_anthropic,
        ), patch("xauditor.model_factory.LANGCHAIN_ANTHROPIC_AVAILABLE", True):
            model = AnthropicLangChainChatModel(
                provider_name="anthr",
                model_name="claude-sonnet-4-5",
                base_url="https://api.anthropic.com",
                api_key="sk-ant-fake",
                thinking_enabled=False,
                sampling=SamplingParams(repetition_penalty=1.05),
            )
            with self.assertLogs("xauditor.model_factory", level=logging.WARNING) as cm:
                model._chat()
        # WARNING fires once with provider name + parameter mention.
        message = "\n".join(cm.output)
        self.assertIn("anthr", message)
        self.assertIn("repetition_penalty", message)
        # repetition_penalty SHALL NOT reach ChatAnthropic in any form.
        self.assertNotIn("repetition_penalty", captured)
        self.assertNotIn("extra_body", captured)
        # Second call SHALL NOT re-warn (deduped per instance).
        with self.assertNoLogs("xauditor.model_factory", level=logging.WARNING):
            model._chat()


class EnsureRuntimeAvailableTests(unittest.TestCase):
    def test_anthropic_kind_with_missing_package_raises(self) -> None:
        provider = _real_anthropic_provider()
        with patch("xauditor.model_factory.LANGCHAIN_ANTHROPIC_AVAILABLE", False):
            with self.assertRaises(PreflightError) as ctx:
                ensure_provider_runtime_available(provider, provider_name="p1")
        message = str(ctx.exception)
        self.assertIn("p1", message)
        self.assertIn("langchain-anthropic", message)

    def test_anthropic_kind_with_present_package_passes(self) -> None:
        provider = _real_anthropic_provider()
        with patch("xauditor.model_factory.LANGCHAIN_ANTHROPIC_AVAILABLE", True):
            # Should NOT raise.
            ensure_provider_runtime_available(provider, provider_name="p1")

    def test_openai_kind_with_missing_anthropic_package_still_passes(self) -> None:
        provider = _real_openai_provider()
        # OpenAI provider SHALL NOT require langchain-anthropic — even if
        # it is missing, ensure_provider_runtime_available is OK with it.
        with patch("xauditor.model_factory.LANGCHAIN_ANTHROPIC_AVAILABLE", False), patch(
            "xauditor.model_factory.LANGCHAIN_OPENAI_AVAILABLE", True
        ):
            ensure_provider_runtime_available(provider, provider_name="p1")

    def test_mock_url_bypasses_runtime_check(self) -> None:
        # Mock URLs SHALL NOT require any langchain runtime regardless
        # of kind.
        provider = LLMConfig(
            base_url="mock://offline",
            api_key="k",
            model_name="m",
            kind=PROVIDER_KIND_ANTHROPIC,
        )
        with patch("xauditor.model_factory.LANGCHAIN_ANTHROPIC_AVAILABLE", False):
            ensure_provider_runtime_available(provider, provider_name="p1")


if __name__ == "__main__":
    unittest.main()
