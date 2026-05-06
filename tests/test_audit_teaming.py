from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.agents import (
    AnalyzerResult,
    AnalyzerSubagentRecord,
    AnalyzerTeam,
    ConversationBuffer,
    DebateTranscript,
    ExploitationResult,
    ExploiterSubagentRecord,
    ExploiterTeam,
    ValidationResult,
    ValidatorSubagentRecord,
    ValidatorTeam,
    _analyzer_record_key,
    _exploit_record_key,
    _build_subagent_models,
)
from xauditor.config import (
    AnalyzerTeamConfig,
    AuditAnalyzerStageConfig,
    AuditExploiterStageConfig,
    AuditValidatorStageConfig,
    ExploiterTeamConfig,
    LLMConfig,
    LLMSettings,
    LoggingConfig,
    Neo4jConfig,
    RepositoryConfig,
    RuntimeConfig,
    TeamingConfig,
    ValidatorDebateConfig,
    ValidatorTeamConfig,
    XAuditorConfig,
)
from xauditor.models import AuditUnit, FunctionRecord, PathRecord, ValidationStatus
from xauditor.prompts import (
    ANALYZER_PROMPT,
    DEDUP_JUDGE_PROMPT,
    EXPLOITATION_PROMPT,
    VALIDATOR_DEBATE_PROMPT,
    VALIDATOR_PROMPT,
)


@dataclass
class FakeChatModel:
    provider_name: str
    model_name: str = "fake-model"
    responder: Callable[[Any, str, dict], dict] | None = None
    is_mock: bool = True
    last_thinking: str = ""
    last_reasoning_tokens: int = 0

    def invoke_json(self, system: str, user: dict, *, user_text: str | None = None) -> dict:
        if self.responder is None:
            return {}
        return self.responder(self, system, user)

    def invoke_text(self, system: str, user: dict, *, user_text: str | None = None) -> str:
        return json.dumps(self.invoke_json(system, user, user_text=user_text))


def _analyzer_kwargs(teaming: TeamingConfig) -> dict[str, Any]:
    """Translate a legacy TeamingConfig into the new AnalyzerTeam kwargs."""
    return {
        "stage_cfg": AuditAnalyzerStageConfig(
            provider_list=teaming.analyzer.provider_list
        ),
        "replication": teaming.analyzer.subagent_count,
    }


def _validator_kwargs(teaming: TeamingConfig) -> dict[str, Any]:
    return {
        "stage_cfg": AuditValidatorStageConfig(
            provider_list=teaming.validator.provider_list,
            debate=ValidatorDebateConfig(
                enabled=True,
                max_rounds=teaming.validator.debate_rounds,
            ),
        ),
        "replication": teaming.validator.subagent_count,
    }


def _exploiter_kwargs(teaming: TeamingConfig) -> dict[str, Any]:
    return {
        "stage_cfg": AuditExploiterStageConfig(
            provider_list=teaming.exploiter.provider_list
        ),
        "replication": teaming.exploiter.subagent_count,
    }


def _llm_settings(provider_names: tuple[str, ...]) -> LLMSettings:
    providers = {
        name: LLMConfig(
            base_url=f"mock://{name}",
            api_key="",
            model_name="mock-model",
            thinking_enabled=False,
        )
        for name in provider_names
    }
    return LLMSettings(default_provider=provider_names[0], providers=providers)


def _unit() -> AuditUnit:
    path = PathRecord(
        entry_function="entry",
        function_names=("entry",),
        file_paths=("app.py",),
        path_fingerprint="fp-test",
        function_ids=("fn::entry",),
        business_context="ctx",
        trust_boundary="internal",
    )
    return AuditUnit(path=path, function_ids=("fn::entry",))


def _minimal_config(teaming: TeamingConfig | None = None, *, repo_root: Path | None = None) -> XAuditorConfig:
    root = repo_root or Path("/tmp")
    return XAuditorConfig(
        repo_root=root,
        graphdb=Neo4jConfig(),
        repository=RepositoryConfig(),
        runtime=RuntimeConfig(root_dir=root),
        logging=LoggingConfig(),
        llm=_llm_settings(("p1",)),
        teaming=teaming or TeamingConfig(),
    )


def _function() -> FunctionRecord:
    return FunctionRecord(
        function_id="fn::entry",
        name="entry",
        qualified_name="app.entry",
        file_path="app.py",
        module_name="app",
        start_line=1,
        end_line=5,
        source="def entry(): pass",
    )


class AnalyzerTeamTests(unittest.TestCase):
    def test_exact_match_dedup(self) -> None:
        teaming = TeamingConfig(
            enabled=True,
            analyzer=AnalyzerTeamConfig(subagent_count=3, provider_list=("p1",)),
            validator=ValidatorTeamConfig(subagent_count=1, provider_list=("p1",)),
            exploiter=ExploiterTeamConfig(subagent_count=1, provider_list=("p1",)),
        )
        llm = _llm_settings(("p1",))

        def responder(model, system, user):
            return {
                "status": "candidate",
                "finding_name": "CMD Injection",
                "description": "dangerous",
                "analysis": "",
                "reason": "uses shell",
                "suspect_function_id": "fn::entry",
                "suspect_line": 2,
                "evidence_strength": "high",
            }

        def fake_build(provider, *, provider_name="default", sampling=None):
            return FakeChatModel(provider_name=provider_name, responder=responder)

        with patch("xauditor.audit.agents.build_chat_model", side_effect=fake_build):
            team = AnalyzerTeam(**_analyzer_kwargs(teaming), llm=llm)
            consolidated, records = team.run(
                unit=_unit(), path_functions=[_function()], path_context=None
            )
        self.assertEqual(len(records), 3)
        self.assertEqual(len(consolidated), 1)
        self.assertEqual(consolidated[0].finding_name, "CMD Injection")

    def test_provider_cycling_modulo(self) -> None:
        llm = _llm_settings(("p1", "p2"))
        models = _build_subagent_models(("p1", "p2"), 5, llm)
        self.assertEqual(
            [model.provider_name for model in models],
            ["p1", "p2", "p1", "p2", "p1"],
        )

    def test_no_candidates_returns_empty_consolidated(self) -> None:
        teaming = TeamingConfig(
            enabled=True,
            analyzer=AnalyzerTeamConfig(subagent_count=2, provider_list=("p1",)),
            validator=ValidatorTeamConfig(subagent_count=1, provider_list=("p1",)),
            exploiter=ExploiterTeamConfig(subagent_count=1, provider_list=("p1",)),
        )
        llm = _llm_settings(("p1",))

        def responder(model, system, user):
            return {
                "status": "no_issue",
                "finding_name": "",
                "description": "",
                "reason": "nothing",
                "suspect_function_id": "",
                "suspect_line": 0,
                "evidence_strength": "low",
            }

        def fake_build(provider, *, provider_name="default", sampling=None):
            return FakeChatModel(provider_name=provider_name, responder=responder)

        with patch("xauditor.audit.agents.build_chat_model", side_effect=fake_build):
            team = AnalyzerTeam(**_analyzer_kwargs(teaming), llm=llm)
            consolidated, records = team.run(
                unit=_unit(), path_functions=[_function()], path_context=None
            )
        self.assertEqual(consolidated, [])
        self.assertEqual(len(records), 2)


class ValidatorTeamTests(unittest.TestCase):
    def _make_config(self, subagents: int, rounds: int = 2) -> TeamingConfig:
        return TeamingConfig(
            enabled=True,
            analyzer=AnalyzerTeamConfig(subagent_count=1, provider_list=("p1",)),
            validator=ValidatorTeamConfig(
                subagent_count=subagents,
                provider_list=("p1", "p2")[:subagents],
                debate_rounds=rounds,
            ),
            exploiter=ExploiterTeamConfig(subagent_count=1, provider_list=("p1",)),
        )

    def test_unanimous_consensus_no_debate(self) -> None:
        teaming = self._make_config(subagents=2)
        llm = _llm_settings(("p1", "p2"))

        def responder(model, system, user):
            return {"status": "Valid", "analysis": "confirmed"}

        def fake_build(provider, *, provider_name="default", sampling=None):
            return FakeChatModel(provider_name=provider_name, responder=responder)

        analyzer = AnalyzerResult(status="candidate", finding_name="X", evidence_strength="high")
        with patch("xauditor.audit.agents.build_chat_model", side_effect=fake_build):
            team = ValidatorTeam(**_validator_kwargs(teaming), llm=llm)
            result, records, debate = team.run_for_finding(
                unit=_unit(), analyzer=analyzer, path_context=None, finding_fingerprint="fp::f0"
            )
        self.assertEqual(len(records), 2)
        self.assertIsNone(debate)
        self.assertEqual(result.status, ValidationStatus.VALID)

    def test_debate_converges_and_captures_transcript(self) -> None:
        teaming = self._make_config(subagents=2, rounds=3)
        llm = _llm_settings(("p1", "p2"))

        call_state = {"rounds": 0}

        def responder(model, system, user):
            if system == VALIDATOR_PROMPT:
                # Return disagreement initially
                if model.provider_name == "p1":
                    return {"status": "Valid", "analysis": "p1 says valid"}
                return {"status": "False Positive", "analysis": "p2 says false"}
            if system == VALIDATOR_DEBATE_PROMPT:
                call_state["rounds"] += 1
                # After 1 round of debate both agree on "valid"
                if call_state["rounds"] > 2:
                    return {"verdict": "valid", "rebuttal": "agreed"}
                return {"verdict": "valid" if model.provider_name == "p1" else "false positive", "rebuttal": "holding"}
            return {}

        def fake_build(provider, *, provider_name="default", sampling=None):
            return FakeChatModel(provider_name=provider_name, responder=responder)

        analyzer = AnalyzerResult(status="candidate", finding_name="X", evidence_strength="high")
        with patch("xauditor.audit.agents.build_chat_model", side_effect=fake_build):
            team = ValidatorTeam(**_validator_kwargs(teaming), llm=llm)
            result, records, debate = team.run_for_finding(
                unit=_unit(), analyzer=analyzer, path_context=None, finding_fingerprint="fp::f0"
            )
        self.assertIsNotNone(debate)
        self.assertGreater(len(debate.turns), 0)
        first_turn = debate.turns[0]
        self.assertEqual(first_turn.system_prompt, VALIDATOR_DEBATE_PROMPT)
        self.assertIn("finding_name", first_turn.user_message)
        self.assertIsInstance(first_turn.raw_response, str)
        self.assertIn(first_turn.verdict, {"valid", "false positive"})

    def test_debate_exhaustion_marks_non_converged(self) -> None:
        teaming = self._make_config(subagents=2, rounds=1)
        llm = _llm_settings(("p1", "p2"))

        def responder(model, system, user):
            if system == VALIDATOR_PROMPT:
                return (
                    {"status": "Valid", "analysis": "p1"}
                    if model.provider_name == "p1"
                    else {"status": "False Positive", "analysis": "p2"}
                )
            if system == VALIDATOR_DEBATE_PROMPT:
                return (
                    {"verdict": "valid", "rebuttal": "still valid"}
                    if model.provider_name == "p1"
                    else {"verdict": "false positive", "rebuttal": "still false"}
                )
            return {}

        def fake_build(provider, *, provider_name="default", sampling=None):
            return FakeChatModel(provider_name=provider_name, responder=responder)

        analyzer = AnalyzerResult(status="candidate", finding_name="X", evidence_strength="high")
        with patch("xauditor.audit.agents.build_chat_model", side_effect=fake_build):
            team = ValidatorTeam(**_validator_kwargs(teaming), llm=llm)
            _result, _records, debate = team.run_for_finding(
                unit=_unit(), analyzer=analyzer, path_context=None, finding_fingerprint="fp::f0"
            )
        self.assertIsNotNone(debate)
        self.assertFalse(debate.converged)


class ExploiterTeamTests(unittest.TestCase):
    def test_consolidated_status_picks_most_optimistic(self) -> None:
        teaming = TeamingConfig(
            enabled=True,
            analyzer=AnalyzerTeamConfig(subagent_count=1, provider_list=("p1",)),
            validator=ValidatorTeamConfig(subagent_count=1, provider_list=("p1",)),
            exploiter=ExploiterTeamConfig(subagent_count=2, provider_list=("p1", "p2")),
        )
        llm = _llm_settings(("p1", "p2"))

        def responder(model, system, user):
            if model.provider_name == "p1":
                return {"status": "exploitable", "steps": "attack vector A"}
            return {"status": "not_exploitable", "steps": "no reachability"}

        def fake_build(provider, *, provider_name="default", sampling=None):
            return FakeChatModel(provider_name=provider_name, responder=responder)

        analyzer = AnalyzerResult(status="candidate", finding_name="X", evidence_strength="high")
        with patch("xauditor.audit.agents.build_chat_model", side_effect=fake_build):
            team = ExploiterTeam(**_exploiter_kwargs(teaming), llm=llm)
            result, records = team.run_for_finding(
                unit=_unit(), analyzer=analyzer, path_context=None, validator_context={"validator_verdict": "Valid"}
            )
        self.assertEqual(len(records), 2)
        self.assertEqual(result.status, "exploitable")
        self.assertIn("subagent-0-p1", result.steps)


class ConversationBufferTests(unittest.TestCase):
    def test_snapshot_is_isolated_from_mutation(self) -> None:
        buffer = ConversationBuffer()
        buffer.add("user", "hello")
        snap = buffer.snapshot()
        buffer.add("user", "world")
        self.assertEqual(len(snap), 1)
        self.assertEqual(snap[0]["content"], "hello")

    def test_clear_empties_buffer(self) -> None:
        buffer = ConversationBuffer()
        buffer.add("user", "hi")
        buffer.clear()
        self.assertEqual(buffer.snapshot(), [])


class DedupKeyTests(unittest.TestCase):
    def test_analyzer_record_key_normalizes_finding_name(self) -> None:
        record = AnalyzerSubagentRecord(
            subagent_index=0,
            provider_name="p",
            model_name="m",
            result=AnalyzerResult(
                status="candidate",
                finding_name="CMD Injection",
                suspect_function_id="fn",
                suspect_line=7,
            ),
        )
        self.assertEqual(_analyzer_record_key(record), ("cmd injection", "fn", 7))

    def test_exploit_record_key_uses_normalized_fingerprint(self) -> None:
        record = ExploiterSubagentRecord(
            subagent_index=0,
            provider_name="p",
            model_name="m",
            result=ExploitationResult(status="exploitable", steps="  Attack \n  path  "),
        )
        status, fingerprint = _exploit_record_key(record)
        self.assertEqual(status, "exploitable")
        self.assertEqual(fingerprint, "attack path")


class _FakeAnalyzerTeam:
    def __init__(self, consolidated, records):
        self._consolidated = consolidated
        self._records = records

    def run(self, *, unit, path_functions, path_context):
        return self._consolidated, self._records


class _FakeValidatorTeam:
    def __init__(self, result, records, debate=None):
        self._result = result
        self._records = records
        self._debate = debate

    def run_for_finding(self, *, unit, analyzer, path_context, finding_fingerprint):
        return self._result, self._records, self._debate


class _FakeExploiterTeam:
    def __init__(self, result, records):
        self._result = result
        self._records = records

    def run_for_finding(self, *, unit, analyzer, path_context, validator_context=None):
        self.last_validator_context = validator_context
        return self._result, self._records


class WorkflowTeamingIntegrationTests(unittest.TestCase):
    def test_process_unit_teaming_records_shared_state(self) -> None:
        from xauditor.audit.workflow import AuditWorkflow
        from xauditor.config import XAuditorConfig

        analyzer_record = AnalyzerSubagentRecord(
            subagent_index=0,
            provider_name="p1",
            model_name="m",
            result=AnalyzerResult(
                status="candidate",
                finding_name="X",
                suspect_function_id="fn::entry",
                suspect_line=3,
                evidence_strength="high",
            ),
        )
        validator_record = ValidatorSubagentRecord(
            subagent_index=0,
            provider_name="p1",
            model_name="m",
            result=ValidationResult(status=ValidationStatus.VALID, analysis="ok"),
        )
        exploiter_record = ExploiterSubagentRecord(
            subagent_index=0,
            provider_name="p1",
            model_name="m",
            result=ExploitationResult(status="exploitable", steps="attack"),
        )

        teaming = TeamingConfig(
            enabled=True,
            analyzer=AnalyzerTeamConfig(subagent_count=1, provider_list=("p1",)),
            validator=ValidatorTeamConfig(subagent_count=1, provider_list=("p1",)),
            exploiter=ExploiterTeamConfig(subagent_count=1, provider_list=("p1",)),
        )
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        repo_root = Path(tmp_dir.name)
        (repo_root / "app.py").write_text(
            "def entry():\n    a = 1\n    b = 2\n    c = 3\n    return c\n", encoding="utf-8"
        )
        config = _minimal_config(teaming, repo_root=repo_root)

        fake_analyzer = _FakeAnalyzerTeam(
            consolidated=[analyzer_record.result], records=[analyzer_record]
        )
        fake_validator = _FakeValidatorTeam(
            result=validator_record.result, records=[validator_record]
        )
        fake_exploiter = _FakeExploiterTeam(
            result=exploiter_record.result, records=[exploiter_record]
        )

        class _StubLLMClient:
            @classmethod
            def from_config(cls, *a, **kw):
                return cls()

        with patch(
            "xauditor.audit.workflow.LLMClient", _StubLLMClient
        ), patch("xauditor.audit.workflow.resolve_chat_model", return_value=FakeChatModel(provider_name="p1")):
            workflow = AuditWorkflow(
                config=config,
                llm_client=_StubLLMClient(),
                analyzer_team=fake_analyzer,
                validator_team=fake_validator,
                exploiter_team=fake_exploiter,
            )
            per_finding, shared_state, checkpoint = workflow._process_unit_teaming(
                unit=_unit(),
                path_functions=[_function()],
                path_context={"referenced_symbols": ()},
            )
        self.assertEqual(checkpoint, "Valid")
        self.assertEqual(len(per_finding), 1)
        self.assertIn("analyzer_subagents", shared_state)
        self.assertIn("validator_subagents", shared_state)
        self.assertIn("exploiter_subagents", shared_state)
        self.assertIn("validator_debates", shared_state)
        self.assertEqual(len(shared_state["analyzer_subagents"]), 1)
        self.assertEqual(
            fake_exploiter.last_validator_context,
            {"validator_verdict": "Valid", "validator_analysis": "ok"},
        )

    def test_process_unit_teaming_skips_exploiter_when_all_false_positive(self) -> None:
        from xauditor.audit.workflow import AuditWorkflow
        from xauditor.config import XAuditorConfig

        analyzer_record = AnalyzerSubagentRecord(
            subagent_index=0,
            provider_name="p1",
            model_name="m",
            result=AnalyzerResult(
                status="candidate",
                finding_name="X",
                suspect_function_id="fn::entry",
                suspect_line=3,
                evidence_strength="high",
            ),
        )
        fp_validator_record = ValidatorSubagentRecord(
            subagent_index=0,
            provider_name="p1",
            model_name="m",
            result=ValidationResult(status=ValidationStatus.FALSE_POSITIVE, analysis="no"),
        )

        teaming = TeamingConfig(
            enabled=True,
            analyzer=AnalyzerTeamConfig(subagent_count=1, provider_list=("p1",)),
            validator=ValidatorTeamConfig(subagent_count=1, provider_list=("p1",)),
            exploiter=ExploiterTeamConfig(subagent_count=1, provider_list=("p1",)),
        )
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        repo_root = Path(tmp_dir.name)
        (repo_root / "app.py").write_text(
            "def entry():\n    a = 1\n    b = 2\n    c = 3\n    return c\n", encoding="utf-8"
        )
        config = _minimal_config(teaming, repo_root=repo_root)
        fake_exploiter = _FakeExploiterTeam(
            result=ExploitationResult(status="should_not_run", steps=""),
            records=[],
        )
        fake_exploiter.last_validator_context = "untouched"

        class _StubLLMClient:
            @classmethod
            def from_config(cls, *a, **kw):
                return cls()

        with patch(
            "xauditor.audit.workflow.LLMClient", _StubLLMClient
        ), patch("xauditor.audit.workflow.resolve_chat_model", return_value=FakeChatModel(provider_name="p1")):
            workflow = AuditWorkflow(
                config=config,
                llm_client=_StubLLMClient(),
                analyzer_team=_FakeAnalyzerTeam([analyzer_record.result], [analyzer_record]),
                validator_team=_FakeValidatorTeam(
                    result=fp_validator_record.result, records=[fp_validator_record]
                ),
                exploiter_team=fake_exploiter,
            )
            per_finding, shared_state, _checkpoint = workflow._process_unit_teaming(
                unit=_unit(),
                path_functions=[_function()],
                path_context={"referenced_symbols": ()},
            )
        # Exploiter team should not have been invoked.
        self.assertEqual(fake_exploiter.last_validator_context, "untouched")
        finding_fp = list(shared_state["exploiter_subagents"].keys())[0]
        self.assertEqual(shared_state["exploiter_subagents"][finding_fp], [])


class SingleAgentRegressionTests(unittest.TestCase):
    def test_single_agent_workflow_produces_no_teaming_keys(self) -> None:
        from xauditor.audit.agents import AnalyzerAgent, ExploitationAgent, ValidatorAgent
        from xauditor.audit.workflow import AuditWorkflow
        from xauditor.config import XAuditorConfig

        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        repo_root = Path(tmp_dir.name)
        (repo_root / "app.py").write_text(
            "def entry():\n    a = 1\n    b = 2\n    c = 3\n    return c\n", encoding="utf-8"
        )
        config = _minimal_config(repo_root=repo_root)

        analyzer_model = FakeChatModel(
            provider_name="p1",
            responder=lambda m, s, u: {
                "status": "candidate",
                "finding_name": "X",
                "description": "",
                "reason": "",
                "suspect_function_id": "fn::entry",
                "suspect_line": 3,
                "evidence_strength": "high",
            },
        )
        exploit_model = FakeChatModel(
            provider_name="p1",
            responder=lambda m, s, u: {"status": "exploitable", "steps": "attack"},
        )
        validator_model = FakeChatModel(
            provider_name="p1",
            responder=lambda m, s, u: {"status": "Valid", "analysis": "ok"},
        )

        class _StubLLMClient:
            @classmethod
            def from_config(cls, *a, **kw):
                return cls()

        with patch("xauditor.audit.workflow.LLMClient", _StubLLMClient):
            workflow = AuditWorkflow(
                config=config,
                llm_client=_StubLLMClient(),
                analyzer_agent=AnalyzerAgent(chat_model=analyzer_model),
                exploitation_agent=ExploitationAgent(chat_model=exploit_model),
                validator_agent=ValidatorAgent(chat_model=validator_model),
            )
            per_finding, shared_state, checkpoint = workflow._process_unit_single(
                unit=_unit(),
                path_functions=[_function()],
                path_context={"referenced_symbols": ()},
            )
        self.assertEqual(checkpoint, "Valid")
        for key in ("analyzer_subagents", "validator_subagents", "exploiter_subagents", "validator_debates"):
            self.assertNotIn(key, shared_state)


if __name__ == "__main__":
    unittest.main()
