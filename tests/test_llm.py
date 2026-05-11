from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.config import LLMConfig
from xauditor.errors import LLMError, RetryExhaustedError
from xauditor.llm import LLMClient
from xauditor.prompts import (
    CLASS_SUMMARY_PROMPT,
    CLASS_SUMMARY_PROMPT_VERSION,
    FUNCTION_SUMMARY_PROMPT,
    FUNCTION_SUMMARY_PROMPT_VERSION,
    get_prompt_definition,
)
from xauditor.runtime_logging import RuntimeLogger


class LLMLoggingTests(unittest.TestCase):
    def test_summarize_path_method_is_gone(self) -> None:
        # ``drop-summarize-path-llm-call`` removed the per-path
        # enrichment call. Catch any regression that re-adds it.
        self.assertFalse(hasattr(LLMClient, "summarize_path"))

    def test_http_summarize_function_emits_request_and_response_summaries(self) -> None:
        stream = io.StringIO()
        logger = RuntimeLogger(level="debug", stream=stream, redactions=("secret-key",))
        client = LLMClient(
            config=LLMConfig(base_url="https://llm.example/v1", api_key="secret-key", model_name="gpt-test"),
            logger=logger,
        )

        with patch.object(client.model, "invoke_json", return_value={"summary": "Summarized function"}):
            summary = client.summarize_function("run_ls", "app.py", "def run_ls(user_input): ...")

        self.assertEqual(summary, "Summarized function")
        rendered = stream.getvalue()
        self.assertIn("LLM request | base_url=https://llm.example/v1", rendered)
        self.assertIn("wire_protocol=openai", rendered)
        self.assertIn("model=gpt-test", rendered)
        self.assertIn(f"prompt_version={FUNCTION_SUMMARY_PROMPT_VERSION}", rendered)
        self.assertIn("LLM response | prompt_version=", rendered)
        self.assertIn("response_keys=summary", rendered)
        self.assertIn("response_key_count=1", rendered)
        self.assertIn("response_size=", rendered)
        self.assertNotIn("preview=", rendered)
        self.assertNotIn("secret-key", rendered)

    def test_http_summarize_function_retries_transient_model_errors(self) -> None:
        client = LLMClient(
            config=LLMConfig(base_url="https://llm.example/v1", api_key="secret-key", model_name="gpt-test"),
        )
        calls = {"count": 0}

        def flaky_invoke(system: str, user: dict[str, object]) -> dict[str, object]:
            del system, user
            calls["count"] += 1
            if calls["count"] < 3:
                raise RuntimeError("temporary provider outage")
            return {"summary": "Summarized function"}

        with patch.object(client.model, "invoke_json", side_effect=flaky_invoke):
            with patch("xauditor.resilience.time.sleep", return_value=None):
                summary = client.summarize_function("run_ls", "app.py", "def run_ls(user_input): ...")

        self.assertEqual(summary, "Summarized function")
        self.assertEqual(calls["count"], 3)

    def test_http_summarize_function_raises_typed_error_after_retry_exhaustion(self) -> None:
        stream = io.StringIO()
        logger = RuntimeLogger(level="debug", stream=stream, redactions=("secret-key",))
        client = LLMClient(
            config=LLMConfig(base_url="https://llm.example/v1", api_key="secret-key", model_name="gpt-test"),
            logger=logger,
        )

        with patch.object(client.model, "invoke_json", side_effect=RuntimeError("provider secret-key outage")):
            with patch("xauditor.resilience.time.sleep", return_value=None):
                with self.assertRaises(LLMError) as ctx:
                    client.summarize_function("run_ls", "app.py", "def run_ls(user_input): ...")

        self.assertIsInstance(ctx.exception.__cause__, RetryExhaustedError)
        rendered = stream.getvalue()
        self.assertRegex(rendered, r"ERROR \[[^\]]+\]: LLM request failed")
        self.assertIn("operation=llm.summarize_function", rendered)
        self.assertIn("attempts=3", rendered)
        self.assertNotIn("secret-key", rendered)

    def test_http_summarize_function_uses_heuristic_fallback_on_malformed_output(self) -> None:
        stream = io.StringIO()
        logger = RuntimeLogger(level="debug", stream=stream, redactions=("secret-key",))
        client = LLMClient(
            config=LLMConfig(base_url="https://llm.example/v1", api_key="secret-key", model_name="gpt-test"),
            logger=logger,
        )

        with patch.object(client.model, "invoke_json", return_value={"wrong": "shape"}):
            summary = client.summarize_function("run_ls", "app.py", "def run_ls(user_input): ...")

        self.assertEqual(summary, "run_ls in app.py handles application logic.")
        rendered = stream.getvalue()
        self.assertRegex(rendered, r"ERROR \[[^\]]+\]: LLM structured output validation failed")
        self.assertIn("operation=llm.summarize_function", rendered)
        self.assertIn(f"prompt_version={FUNCTION_SUMMARY_PROMPT_VERSION}", rendered)
        self.assertRegex(rendered, r"WARNING \[[^\]]+\]: Using heuristic fallback for summarize_function after structured output validation failed\.")
        self.assertNotIn("secret-key", rendered)

    def test_http_summarize_class_uses_heuristic_fallback_on_malformed_output(self) -> None:
        stream = io.StringIO()
        logger = RuntimeLogger(level="debug", stream=stream, redactions=("secret-key",))
        client = LLMClient(
            config=LLMConfig(base_url="https://llm.example/v1", api_key="secret-key", model_name="gpt-test"),
            logger=logger,
        )

        malformed = {
            "class_name": "AnalyzerResult",
            "responsibilities": ["collect path evidence", "score exploitability"],
            "recommendations": ["Use controlled vocabulary."],
        }
        with patch.object(client.model, "invoke_json", return_value=malformed):
            summary = client.summarize_class(
                "AnalyzerResult",
                "src/xauditor/audit/agents.py",
                "class AnalyzerResult:\n    finding_name = ''",
                method_names=("render", "score"),
                member_names=("finding_name", "reason"),
            )

        self.assertEqual(
            summary,
            {
                "summary": "AnalyzerResult in src/xauditor/audit/agents.py coordinates render, score.",
                "business_context": "AnalyzerResult owns class-scoped members finding_name, reason.",
            },
        )
        rendered = stream.getvalue()
        self.assertRegex(rendered, r"ERROR \[[^\]]+\]: LLM structured output validation failed")
        self.assertIn("operation=llm.summarize_class", rendered)
        self.assertIn(f"prompt_version={CLASS_SUMMARY_PROMPT_VERSION}", rendered)
        self.assertRegex(rendered, r"WARNING \[[^\]]+\]: Using heuristic fallback for summarize_class after structured output validation failed\.")
        self.assertNotIn("secret-key", rendered)

    def test_summarization_uses_shared_prompt_catalog(self) -> None:
        client = LLMClient(
            config=LLMConfig(base_url="https://llm.example/v1", api_key="secret-key", model_name="gpt-test"),
        )
        seen_system_prompts: list[str] = []

        def fake_invoke_json(system: str, user: dict[str, object]) -> dict[str, object]:
            seen_system_prompts.append(system)
            if "method_names" in user:
                return {
                    "summary": "Class summary",
                    "business_context": "Class context",
                }
            return {"summary": "Function summary"}

        with patch.object(client.model, "invoke_json", side_effect=fake_invoke_json):
            function_summary = client.summarize_function("run_ls", "app.py", "def run_ls(user_input): ...")
            class_summary = client.summarize_class(
                "Runner",
                "app.py",
                "class Runner:\n    pass",
                method_names=("run",),
                member_names=("DEFAULT_CMD",),
            )

        self.assertEqual(function_summary, "Function summary")
        self.assertEqual(class_summary["summary"], "Class summary")
        self.assertEqual(
            seen_system_prompts,
            [FUNCTION_SUMMARY_PROMPT, CLASS_SUMMARY_PROMPT],
        )

    def test_prompt_registry_exposes_versioned_prompt_selection(self) -> None:
        default_prompt = get_prompt_definition("function_summary")
        legacy_prompt = get_prompt_definition("function_summary", "v1")

        self.assertEqual(default_prompt.version, FUNCTION_SUMMARY_PROMPT_VERSION)
        self.assertEqual(default_prompt.system, FUNCTION_SUMMARY_PROMPT)
        self.assertEqual(legacy_prompt.version, "v1")
        self.assertNotEqual(default_prompt.system, legacy_prompt.system)
        self.assertEqual(get_prompt_definition("class_summary").version, CLASS_SUMMARY_PROMPT_VERSION)


if __name__ == "__main__":
    unittest.main()
