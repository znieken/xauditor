from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.errors import ValidationError
from xauditor.langchain_support import invoke_agent
from xauditor.llm_outputs import ExploitationOutput


class _FakeChatModel:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.provider_name = "shared"
        self.model_name = "test-model"
        self.is_mock = False
        self.calls: list[tuple[str, dict[str, object]]] = []

    def invoke_json(
        self,
        system: str,
        user: dict[str, object],
        *,
        user_text: str | None = None,
    ) -> dict[str, object]:
        self.calls.append((system, user))
        return self.response

    def invoke_text(
        self,
        system: str,
        user: dict[str, object],
        *,
        user_text: str | None = None,
    ) -> str:
        raise NotImplementedError


class LangChainSupportTests(unittest.TestCase):
    def test_invoke_agent_validates_structured_output_and_tracks_prompt_version(self) -> None:
        chat_model = _FakeChatModel({"status": "ready", "steps": "Exploit details"})

        result, meta = invoke_agent(
            agent_name="exploitation",
            system_prompt="Test prompt",
            payload={"finding_name": "Command Injection"},
            chat_model=chat_model,
            response_model=ExploitationOutput,
            response_parser=lambda response: response["steps"],
            prompt_version="v2",
        )

        self.assertEqual(result, "Exploit details")
        self.assertEqual(meta["runtime"], "shared-model")
        self.assertEqual(meta["provider_name"], "shared")
        self.assertEqual(meta["prompt_version"], "v2")
        self.assertEqual(len(chat_model.calls), 1)

    def test_invoke_agent_runs_langchain_executor_without_chat_model(self) -> None:
        result, meta = invoke_agent(
            agent_name="inventory",
            system_prompt="Inventory prompt",
            payload={"repo_root": "/tmp/repo"},
            executor=lambda _payload: {"status": "collected"},
            prompt_version="v2",
        )

        self.assertEqual(result, {"status": "collected"})
        self.assertEqual(meta["runtime"], "langchain-executor")
        self.assertEqual(meta["prompt_version"], "v2")

    def test_invoke_agent_raises_validation_error_on_malformed_structured_output(self) -> None:
        chat_model = _FakeChatModel({"status": "ready"})

        with self.assertRaises(ValidationError):
            invoke_agent(
                agent_name="exploitation",
                system_prompt="Test prompt",
                payload={"finding_name": "Command Injection"},
                chat_model=chat_model,
                response_model=ExploitationOutput,
                response_parser=lambda response: response["steps"],
                prompt_version="v2",
            )


if __name__ == "__main__":
    unittest.main()
