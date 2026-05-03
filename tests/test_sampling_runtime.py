from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.agents import (
    AnalyzerResult,
    ExploitationResult,
    ValidationResult,
)
from xauditor.config import (
    AgentLLMOverride,
    LLMConfig,
    LLMSettings,
    LoggingConfig,
    Neo4jConfig,
    RepositoryConfig,
    RuntimeConfig,
    TeamingConfig,
    XAuditorConfig,
    load_config,
)
from xauditor.model_factory import (
    MockChatModel,
    SamplingParams,
    build_chat_model,
    build_openai_kwargs,
    missing_temperature_warning,
    resolve_chat_model,
)
from xauditor.models import (
    AuditedCallChain,
    AuditRun,
    AuditUnit,
    CoverageInventory,
    CoverageRecord,
    CoverageState,
    PathRecord,
)
from xauditor.reporting.markdown import render_stage_report


class BuildOpenAIKwargsTests(unittest.TestCase):
    def test_temperature_and_top_p_go_top_level(self) -> None:
        sampling = SamplingParams(temperature=0.4, top_p=0.8)
        top_level, extra_body = build_openai_kwargs(sampling)
        self.assertEqual(top_level, {"temperature": 0.4, "top_p": 0.8})
        self.assertEqual(extra_body, {})

    def test_top_k_and_repetition_penalty_go_extra_body(self) -> None:
        sampling = SamplingParams(top_k=20, repetition_penalty=1.1)
        top_level, extra_body = build_openai_kwargs(sampling)
        self.assertEqual(top_level, {})
        self.assertEqual(extra_body, {"top_k": 20, "repetition_penalty": 1.1})

    def test_none_fields_are_omitted(self) -> None:
        sampling = SamplingParams()
        top_level, extra_body = build_openai_kwargs(sampling)
        self.assertEqual(top_level, {})
        self.assertEqual(extra_body, {})

    def test_langchain_chat_model_merges_enable_thinking_with_extra_body(self) -> None:
        from xauditor.model_factory import LangChainChatModel

        model = LangChainChatModel(
            provider_name="p1",
            model_name="m",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            thinking_enabled=True,
            sampling=SamplingParams(top_k=20, repetition_penalty=1.1),
        )
        captured: dict[str, Any] = {}

        def fake_chat_openai(**kwargs):
            captured.update(kwargs)
            return object()

        import xauditor.model_factory as mf

        original = mf.ChatOpenAI
        mf.LANGCHAIN_OPENAI_AVAILABLE = True
        mf.ChatOpenAI = fake_chat_openai
        try:
            model._chat()
        finally:
            mf.ChatOpenAI = original

        extra_body = captured.get("extra_body")
        self.assertIsInstance(extra_body, dict)
        self.assertEqual(extra_body.get("enable_thinking"), True)
        self.assertEqual(extra_body.get("top_k"), 20)
        self.assertAlmostEqual(extra_body.get("repetition_penalty"), 1.1)
        self.assertNotIn("temperature", captured)
        self.assertNotIn("top_p", captured)


class NoSamplingKwargsRegressionTests(unittest.TestCase):
    def test_empty_sampling_emits_no_sampling_kwargs(self) -> None:
        from xauditor.model_factory import LangChainChatModel

        model = LangChainChatModel(
            provider_name="p1",
            model_name="m",
            base_url="https://api.example.com/v1",
            api_key="sk-test",
            thinking_enabled=True,
            sampling=SamplingParams(),
        )
        captured: dict[str, Any] = {}

        def fake_chat_openai(**kwargs):
            captured.update(kwargs)
            return object()

        import xauditor.model_factory as mf

        original = mf.ChatOpenAI
        mf.LANGCHAIN_OPENAI_AVAILABLE = True
        mf.ChatOpenAI = fake_chat_openai
        try:
            model._chat()
        finally:
            mf.ChatOpenAI = original

        self.assertNotIn("temperature", captured)
        self.assertNotIn("top_p", captured)
        extra_body = captured.get("extra_body", {})
        self.assertEqual(extra_body, {"enable_thinking": True})


class MissingTemperatureWarningTests(unittest.TestCase):
    def test_returns_none_when_no_real_providers(self) -> None:
        settings = LLMSettings(
            default_provider="mock",
            providers={
                "mock": LLMConfig(base_url="mock://m", api_key="k", model_name="m"),
            },
        )
        self.assertIsNone(missing_temperature_warning(settings))

    def test_returns_none_when_any_real_provider_has_temperature(self) -> None:
        settings = LLMSettings(
            default_provider="real",
            providers={
                "real": LLMConfig(
                    base_url="https://api.example.com/v1",
                    api_key="k",
                    model_name="m",
                    temperature=0,
                ),
            },
        )
        self.assertIsNone(missing_temperature_warning(settings))

    def test_returns_message_when_real_providers_missing_temperature(self) -> None:
        settings = LLMSettings(
            default_provider="real",
            providers={
                "real": LLMConfig(
                    base_url="https://api.example.com/v1",
                    api_key="k",
                    model_name="m",
                ),
            },
        )
        warning = missing_temperature_warning(settings)
        self.assertIsNotNone(warning)
        self.assertIn("temperature", warning)


class ResolveChatModelSamplingTests(unittest.TestCase):
    def test_analyzer_override_lands_on_mock_model(self) -> None:
        settings = LLMSettings(
            default_provider="shared",
            providers={
                "shared": LLMConfig(
                    base_url="mock://shared",
                    api_key="k",
                    model_name="shared-model",
                    temperature=0.1,
                    top_p=0.9,
                ),
            },
            agent_overrides={
                "auditor": AgentLLMOverride(temperature=0.4, top_k=25),
            },
        )
        analyzer_model = resolve_chat_model(settings, agent="auditor")
        validator_model = resolve_chat_model(settings, agent="validator")

        self.assertIsInstance(analyzer_model, MockChatModel)
        self.assertAlmostEqual(analyzer_model.sampling.temperature, 0.4)
        self.assertAlmostEqual(analyzer_model.sampling.top_p, 0.9)
        self.assertEqual(analyzer_model.sampling.top_k, 25)
        self.assertIsNone(analyzer_model.sampling.repetition_penalty)

        self.assertIsInstance(validator_model, MockChatModel)
        self.assertAlmostEqual(validator_model.sampling.temperature, 0.1)
        self.assertAlmostEqual(validator_model.sampling.top_p, 0.9)
        self.assertIsNone(validator_model.sampling.top_k)


def _path_record(fingerprint: str = "fp::0") -> PathRecord:
    return PathRecord(
        entry_function="entry",
        function_names=("entry",),
        file_paths=("app.py",),
        path_fingerprint=fingerprint,
        function_ids=("fn::entry",),
        business_context="ctx",
        trust_boundary="internal",
    )


def _audit_unit(fingerprint: str = "fp::0") -> AuditUnit:
    return AuditUnit(path=_path_record(fingerprint), function_ids=("fn::entry",))


def _coverage_inventory(fingerprint: str = "fp::0") -> CoverageInventory:
    inventory = CoverageInventory()
    inventory.add(
        CoverageRecord(
            identifier="app.entry",
            category="function",
            state=CoverageState.AUDITED,
        )
    )
    inventory.audited_call_chains.append(
        AuditedCallChain(
            path_fingerprint=fingerprint,
            entry_function="entry",
            function_chain=("entry",),
        )
    )
    return inventory


class ManifestSamplingRenderTests(unittest.TestCase):
    def test_render_stage_report_includes_sampling_block_with_nulls(self) -> None:
        shared_state = {
            "fp::0": {
                "analyzer": {
                    "status": "candidate",
                    "finding_name": "X",
                    "description": "d",
                    "reason": "r",
                    "evidence_strength": "high",
                },
                "model_settings": {
                    "analyzer": {
                        "role": "auditor",
                        "provider_name": "shared",
                        "sampling": {
                            "temperature": 0.2,
                            "top_p": None,
                            "top_k": None,
                            "repetition_penalty": None,
                        },
                    },
                },
            }
        }
        audit_run = AuditRun(
            build_fingerprint="build-1",
            findings=(),
            coverage=_coverage_inventory(),
            checkpoints={"fp::0": "done"},
            shared_state=shared_state,
        )
        report = render_stage_report(audit_run, "analyzer")
        self.assertIn("- Sampling:", report)
        self.assertIn("- temperature: 0.2", report)
        self.assertIn("- top_p: null", report)
        self.assertIn("- top_k: null", report)
        self.assertIn("- repetition_penalty: null", report)

    def test_render_stage_report_sampling_block_all_nulls_when_unset(self) -> None:
        shared_state = {
            "fp::0": {
                "analyzer": {
                    "status": "candidate",
                    "finding_name": "X",
                    "description": "d",
                    "reason": "r",
                    "evidence_strength": "high",
                },
                "model_settings": {
                    "analyzer": {
                        "role": "auditor",
                        "provider_name": "shared",
                        "sampling": {
                            "temperature": None,
                            "top_p": None,
                            "top_k": None,
                            "repetition_penalty": None,
                        },
                    },
                },
            }
        }
        audit_run = AuditRun(
            build_fingerprint="build-1",
            findings=(),
            coverage=_coverage_inventory(),
            checkpoints={"fp::0": "done"},
            shared_state=shared_state,
        )
        report = render_stage_report(audit_run, "analyzer")
        for field in ("temperature", "top_p", "top_k", "repetition_penalty"):
            self.assertIn(f"- {field}: null", report)


if __name__ == "__main__":
    unittest.main()
