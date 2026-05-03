from __future__ import annotations

import unittest
from typing import Any

from xauditor.audit.agents import (
    AnalyzerAgent,
    AnalyzerResult,
    ExploitationAgent,
    ExploitationResult,
    ValidatorAgent,
)
from xauditor.audit.markdown_payload import (
    render_analyzer_markdown,
    render_exploitation_markdown,
    render_validator_markdown,
)
from xauditor.models import AuditUnit, FunctionRecord, PathRecord, ValidationStatus


def _sample_payload_with_symbols() -> dict:
    return {
        "entry_function": "handler",
        "function_names": ("handler",),
        "call_chain": [
            {
                "function_id": "fn::handler",
                "qualified_name": "app.handler",
                "file_path": "app.py",
                "start_line": 10,
                "end_line": 20,
            }
        ],
        "function_definitions": [
            {
                "function_id": "fn::handler",
                "qualified_name": "app.handler",
                "file_path": "app.py",
                "start_line": 10,
                "end_line": 20,
                "source": "def handler():\n    return API_KEY",
            }
        ],
        "referenced_symbols": [
            {
                "symbol_id": "sym::API_KEY",
                "name": "API_KEY",
                "kind": "constant",
                "module_name": "app",
                "file_path": "app.py",
                "start_line": 1,
                "end_line": 1,
                "type_annotation": "str",
                "value_repr": "'abc123'",
                "is_placeholder": False,
                "used_by": [
                    {"function_id": "fn::handler", "line_number": 11, "evidence": "return API_KEY"}
                ],
            },
            {
                "symbol_id": "sym::BIG_BLOB",
                "name": "BIG_BLOB",
                "kind": "constant",
                "module_name": "app",
                "file_path": "app.py",
                "start_line": 2,
                "end_line": 2,
                "type_annotation": "",
                "value_repr": "<placeholder:string:length=500>",
                "is_placeholder": True,
                "used_by": [
                    {"function_id": "fn::handler", "line_number": 12, "evidence": "x = BIG_BLOB"}
                ],
            },
        ],
        "finding_name": "Hardcoded credential",
        "description": "API_KEY is committed",
        "reason": "Leaks secret",
        "evidence_strength": "high",
        "exploitation_status": "confirmed",
        "exploitation_steps": "Read repo, grep for key",
    }


class ReferencedSymbolsRenderingTest(unittest.TestCase):
    def test_analyzer_markdown_includes_referenced_symbols_section(self) -> None:
        text = render_analyzer_markdown(_sample_payload_with_symbols())
        self.assertIn("## Referenced Symbols", text)
        self.assertIn("`API_KEY`", text)
        self.assertIn("kind=constant", text)
        self.assertIn("module=app", text)
        self.assertIn("app.py:1-1", text)
        self.assertIn("'abc123'", text)
        self.assertIn("Used by:", text)
        self.assertIn("`fn::handler` @ line 11", text)
        self.assertIn("return API_KEY", text)

    def test_exploitation_markdown_includes_referenced_symbols_section(self) -> None:
        text = render_exploitation_markdown(_sample_payload_with_symbols())
        self.assertIn("## Referenced Symbols", text)
        self.assertIn("`API_KEY`", text)
        self.assertIn("`BIG_BLOB`", text)

    def test_validator_markdown_includes_referenced_symbols_section(self) -> None:
        text = render_validator_markdown(_sample_payload_with_symbols())
        self.assertIn("## Referenced Symbols", text)
        self.assertIn("`API_KEY`", text)
        self.assertIn("`BIG_BLOB`", text)

    def test_placeholder_values_are_rendered_verbatim(self) -> None:
        text = render_analyzer_markdown(_sample_payload_with_symbols())
        self.assertIn("<placeholder:string:length=500>", text)
        self.assertIn("(placeholder)", text)

    def test_empty_referenced_symbols_omits_section(self) -> None:
        payload = _sample_payload_with_symbols()
        payload["referenced_symbols"] = []
        text = render_analyzer_markdown(payload)
        self.assertNotIn("## Referenced Symbols", text)

    def test_missing_referenced_symbols_omits_section(self) -> None:
        payload = _sample_payload_with_symbols()
        payload.pop("referenced_symbols")
        text = render_exploitation_markdown(payload)
        self.assertNotIn("## Referenced Symbols", text)


class _CapturingChatModel:
    provider_name = "test"
    model_name = "test-model"
    is_mock = False

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.captured_user_text: str = ""

    def invoke_json(
        self, system: str, user: dict[str, Any], *, user_text: str | None = None
    ) -> dict[str, Any]:
        self.captured_user_text = user_text or ""
        return dict(self._response)

    def invoke_text(
        self, system: str, user: dict[str, Any], *, user_text: str | None = None
    ) -> str:
        raise NotImplementedError


def _path_context_with_symbols() -> dict[str, Any]:
    return {
        "call_chain": [
            {
                "function_id": "app.py:handler:1",
                "qualified_name": "handler",
                "file_path": "app.py",
                "start_line": 1,
                "end_line": 5,
            }
        ],
        "function_definitions": [
            {
                "function_id": "app.py:handler:1",
                "qualified_name": "handler",
                "file_path": "app.py",
                "start_line": 1,
                "end_line": 5,
                "source": "def handler():\n    return API_KEY",
            }
        ],
        "referenced_symbols": [
            {
                "symbol_id": "sym::API_KEY",
                "name": "API_KEY",
                "kind": "constant",
                "module_name": "app",
                "file_path": "app.py",
                "start_line": 1,
                "end_line": 1,
                "type_annotation": "str",
                "value_repr": "'abc123'",
                "is_placeholder": False,
                "used_by": [
                    {
                        "function_id": "app.py:handler:1",
                        "line_number": 2,
                        "evidence": "return API_KEY",
                    }
                ],
            }
        ],
    }


class AgentsForwardReferencedSymbolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.unit = AuditUnit(
            path=PathRecord(
                entry_function="handler",
                function_names=("handler",),
                file_paths=("app.py",),
                path_fingerprint="path-1",
                function_ids=("app.py:handler:1",),
            ),
            function_ids=("app.py:handler:1",),
        )
        self.path_functions = [
            FunctionRecord(
                function_id="app.py:handler:1",
                name="handler",
                qualified_name="handler",
                file_path="app.py",
                module_name="app",
                start_line=1,
                end_line=5,
                source="def handler():\n    return API_KEY",
            )
        ]
        self.context = _path_context_with_symbols()

    def test_analyzer_sends_referenced_symbols_in_user_message(self) -> None:
        chat = _CapturingChatModel(
            {
                "status": "candidate",
                "finding_name": "Hardcoded credential",
                "description": "",
                "analysis": "",
                "reason": "",
                "context_notes": "",
                "suspect_function_id": "app.py:handler:1",
                "suspect_line": 2,
                "evidence_strength": "high",
            }
        )
        AnalyzerAgent(chat_model=chat).run(
            unit=self.unit,
            path_functions=self.path_functions,
            path_context=self.context,
        )
        self.assertIn("## Referenced Symbols", chat.captured_user_text)
        self.assertIn("`API_KEY`", chat.captured_user_text)
        self.assertIn("'abc123'", chat.captured_user_text)

    def test_exploitation_sends_referenced_symbols_in_user_message(self) -> None:
        chat = _CapturingChatModel({"status": "confirmed", "steps": "step"})
        analyzer_result = AnalyzerResult(
            status="candidate",
            finding_name="Hardcoded credential",
            description="desc",
            analysis="",
            reason="reason",
            context_notes="",
            suspect_function_id="app.py:handler:1",
            suspect_line=2,
            evidence_strength="high",
        )
        ExploitationAgent(chat_model=chat).run(
            unit=self.unit,
            analyzer=analyzer_result,
            path_context=self.context,
        )
        self.assertIn("## Referenced Symbols", chat.captured_user_text)
        self.assertIn("`API_KEY`", chat.captured_user_text)

    def test_validator_sends_referenced_symbols_in_user_message(self) -> None:
        chat = _CapturingChatModel(
            {"status": ValidationStatus.VALID.value, "analysis": "looks real"}
        )
        analyzer_result = AnalyzerResult(
            status="candidate",
            finding_name="Hardcoded credential",
            description="desc",
            analysis="",
            reason="reason",
            context_notes="",
            suspect_function_id="app.py:handler:1",
            suspect_line=2,
            evidence_strength="high",
        )
        exploitation_result = ExploitationResult(status="confirmed", steps="step")
        ValidatorAgent(chat_model=chat).run(
            unit=self.unit,
            analyzer=analyzer_result,
            exploitation=exploitation_result,
            path_context=self.context,
        )
        self.assertIn("## Referenced Symbols", chat.captured_user_text)
        self.assertIn("`API_KEY`", chat.captured_user_text)


if __name__ == "__main__":
    unittest.main()
