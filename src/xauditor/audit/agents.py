from __future__ import annotations

import json
import re
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ThreadPoolExecutor,
    as_completed,
    wait as futures_wait,
)
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from xauditor.audit._cancellation import RunCancellation

_TeamRecord = TypeVar("_TeamRecord")


def _run_subagents_with_cancel(
    pool: ThreadPoolExecutor,
    futures: list[Future[_TeamRecord]],
    cancellation: "RunCancellation | None",
) -> list[_TeamRecord]:
    """Drain ``futures`` from ``pool`` while respecting cancellation.

    Replacement for ``for f in as_completed(futures): records.append(f.result())``
    that observes ``cancellation`` between bounded waits so a SIGINT
    during a stalled subagent LLM call exits within at most ``0.5 s``
    instead of blocking on ``executor.__exit__`` until the LLM call
    settles.

    On cancel (``cancellation.is_cancelled()``) or deadline
    (``cancellation.deadline_reached()``) the helper:

    1. Calls ``pool.shutdown(wait=False, cancel_futures=True)`` to
       abandon outstanding work without blocking.
    2. Raises ``KeyboardInterrupt`` so the caller's existing
       ``except KeyboardInterrupt: ... raise UserCancelledError``
       branch unwinds the audit.

    With ``cancellation=None`` the helper degrades to the original
    ``as_completed`` behaviour (no timeout) so unit-test paths that
    don't construct a ``RunCancellation`` continue to work unchanged.

    Background subagent threads MAY continue running after this
    helper returns; the OS process exit reaps them and Python's
    finaliser doesn't block on detached LLM I/O.
    """
    if cancellation is None:
        return [future.result() for future in as_completed(futures)]

    pending: set[Future[_TeamRecord]] = set(futures)
    records: list[_TeamRecord] = []
    while pending:
        if cancellation.is_cancelled() or cancellation.deadline_reached():
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001 - escalation best-effort
                pass
            raise KeyboardInterrupt(
                "subagent pool cancelled by run-level cancellation"
            )
        done, pending = futures_wait(
            pending, timeout=0.5, return_when=FIRST_COMPLETED
        )
        for future in done:
            records.append(future.result())
    return records


def _coerce_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple):
        parts: list[str] = []
        for index, item in enumerate(value, start=1):
            text = _coerce_text(item).strip()
            if not text:
                continue
            parts.append(text if text[:1].isdigit() else f"{index}. {text}")
        return "\n".join(parts)
    if isinstance(value, dict):
        return "\n".join(f"{k}: {_coerce_text(v)}" for k, v in value.items())
    return str(value)

from xauditor.audit.markdown_payload import (
    render_analyzer_markdown,
    render_exploitation_markdown,
    render_validator_markdown,
)
from xauditor.config import (
    AuditAnalyzerStageConfig,
    AuditExploiterStageConfig,
    AuditValidatorStageConfig,
    LLMSettings,
)
from xauditor.langchain_support import invoke_agent
from xauditor.llm_outputs import (
    AnalyzerOutput,
    DedupJudgeOutput,
    ExploitationOutput,
    FindingSummaryOutput,
    ValidationOutput,
)
from xauditor.model_factory import ChatModel, build_chat_model
from xauditor.models import AuditUnit, FunctionRecord, ValidationStatus
from xauditor.prompts import (
    ANALYZER_PROMPT,
    ANALYZER_PROMPT_VERSION,
    DEDUP_JUDGE_PROMPT,
    DEDUP_JUDGE_PROMPT_VERSION,
    EXPLOITATION_PROMPT,
    EXPLOITATION_PROMPT_VERSION,
    FINDING_SUMMARY_PROMPT,
    FINDING_SUMMARY_PROMPT_VERSION,
    VALIDATOR_DEBATE_PROMPT,
    VALIDATOR_PROMPT,
    VALIDATOR_PROMPT_VERSION,
)
from xauditor.runtime_logging import RuntimeLogger


@dataclass(frozen=True)
class AnalyzerResult:
    status: str
    finding_name: str = ""
    description: str = ""
    analysis: str = ""
    reason: str = ""
    context_notes: str = ""
    suspect_function_id: str = ""
    suspect_line: int = 0
    evidence_strength: str = "low"


@dataclass(frozen=True)
class ExploitationResult:
    status: str
    steps: str


@dataclass(frozen=True)
class ValidationResult:
    status: ValidationStatus
    analysis: str


class AnalyzerAgent:
    def __init__(
        self,
        *,
        chat_model: ChatModel | None = None,
        logger: RuntimeLogger | None = None,
    ) -> None:
        self.chat_model = chat_model
        self.logger = logger

    def run(
        self,
        *,
        unit: AuditUnit,
        path_functions: list[FunctionRecord],
        path_context: dict[str, object] | None = None,
        subagent_id: int | None = None,
        provider_name: str | None = None,
        excluded_findings: tuple[dict[str, object], ...] = (),
    ) -> AnalyzerResult:
        """Invoke the analyzer agent for a single audit unit.

        ``excluded_findings`` lists candidate-finding summaries that
        have already been recorded for this audit unit. The analyzer
        prompt instructs the model to return one ADDITIONAL distinct
        candidate, OR ``status: "no_issue"`` when the unit holds no
        further candidate. Each entry in ``excluded_findings`` is
        a ``{finding_name, suspect_function_id, suspect_line}`` dict
        — the same exact-match key used by the dedup pipeline.

        When ``excluded_findings`` is empty (the default), the prompt
        behaves as it always did — the v4 system prompt skips the
        excluded-findings clause when the array is empty.
        """

        def _parse_response(response: dict[str, object]) -> AnalyzerResult:
            suspect_line = response.get("suspect_line", 0)
            try:
                parsed_suspect_line = int(suspect_line)
            except (TypeError, ValueError):
                parsed_suspect_line = 0
            return AnalyzerResult(
                status=str(response.get("status", "no_issue")),
                finding_name=_coerce_text(response.get("finding_name", "")),
                description=_coerce_text(response.get("description", "")),
                analysis=_coerce_text(response.get("analysis", "")),
                reason=_coerce_text(response.get("reason", "")),
                context_notes=_coerce_text(response.get("context_notes", "")),
                suspect_function_id=str(response.get("suspect_function_id", "")),
                suspect_line=parsed_suspect_line,
                evidence_strength=str(response.get("evidence_strength", "low")),
            )

        payload: dict[str, object] = {
            "entry_function": unit.path.entry_function,
            "function_names": unit.path.function_names,
            "path_functions": [
                {
                    "function_id": function.function_id,
                    "qualified_name": function.qualified_name,
                    "start_line": function.start_line,
                    "end_line": function.end_line,
                }
                for function in path_functions
            ],
        }
        if path_context is not None:
            payload["call_chain"] = path_context.get("call_chain", [])
            payload["function_definitions"] = path_context.get("function_definitions", [])
            payload["referenced_symbols"] = path_context.get("referenced_symbols", [])
        if excluded_findings:
            payload["excluded_findings"] = list(excluded_findings)
        result, _meta = invoke_agent(
            agent_name="analyzer",
            system_prompt=ANALYZER_PROMPT,
            payload=payload,
            chat_model=self.chat_model,
            logger=self.logger,
            response_model=AnalyzerOutput,
            response_parser=_parse_response,
            prompt_version=ANALYZER_PROMPT_VERSION,
            user_message=render_analyzer_markdown(payload),
        )
        if self.logger is not None:
            self.logger.debug_kv(
                "Analyzer agent call",
                runtime=_meta["runtime"],
                provider=provider_name or _meta.get("provider_name"),
                prompt_version=_meta["prompt_version"],
                subagent_id=subagent_id if subagent_id is not None else "",
                excluded_findings_count=len(excluded_findings),
            )
        return result


class ExploitationAgent:
    def __init__(
        self,
        *,
        chat_model: ChatModel | None = None,
        logger: RuntimeLogger | None = None,
    ) -> None:
        self.chat_model = chat_model
        self.logger = logger

    def run(
        self,
        *,
        unit: AuditUnit,
        analyzer: AnalyzerResult,
        path_context: dict[str, object] | None = None,
        subagent_id: int | None = None,
        provider_name: str | None = None,
        extra_payload: dict[str, object] | None = None,
    ) -> ExploitationResult:
        def _parse_response(response: dict[str, object]) -> ExploitationResult:
            return ExploitationResult(
                status=str(response.get("status", "not_applicable")),
                steps=_coerce_text(response.get("steps", response.get("details", ""))),
            )

        payload: dict[str, object] = {
            "entry_function": unit.path.entry_function,
            "finding_name": analyzer.finding_name,
            "description": analyzer.description,
            "reason": analyzer.reason,
            "evidence_strength": analyzer.evidence_strength,
        }
        if path_context is not None:
            payload["call_chain"] = path_context.get("call_chain", [])
            payload["function_definitions"] = path_context.get("function_definitions", [])
            payload["referenced_symbols"] = path_context.get("referenced_symbols", [])
        if extra_payload:
            for key, value in extra_payload.items():
                payload[key] = value
        result, _meta = invoke_agent(
            agent_name="exploitation",
            system_prompt=EXPLOITATION_PROMPT,
            payload=payload,
            chat_model=self.chat_model,
            logger=self.logger,
            response_model=ExploitationOutput,
            response_parser=_parse_response,
            prompt_version=EXPLOITATION_PROMPT_VERSION,
            user_message=render_exploitation_markdown(payload),
        )
        if self.logger is not None:
            self.logger.debug_kv(
                "Exploitation agent call",
                runtime=_meta["runtime"],
                provider=provider_name or _meta.get("provider_name"),
                prompt_version=_meta["prompt_version"],
                subagent_id=subagent_id if subagent_id is not None else "",
            )
        return result


class ValidatorAgent:
    def __init__(
        self,
        *,
        chat_model: ChatModel | None = None,
        logger: RuntimeLogger | None = None,
    ) -> None:
        self.chat_model = chat_model
        self.logger = logger

    def run(
        self,
        *,
        unit: AuditUnit,
        analyzer: AnalyzerResult,
        exploitation: ExploitationResult | None = None,
        path_context: dict[str, object] | None = None,
    ) -> ValidationResult:
        """Validate an analyzer-produced candidate finding.

        ``exploitation`` is accepted for backward compatibility but is
        IGNORED — after `restructure-audit-modes-and-coverage` Phase
        1B's stage reorder, the validator runs BEFORE the exploiter
        (in every mode) so exploitation context is never available
        when this method is called. The argument is retained as
        ``Optional`` so legacy callers that still pass it don't
        break; callers should drop the kwarg.
        """

        del exploitation  # explicitly ignored; see docstring

        def _parse_response(response: dict[str, object]) -> ValidationResult:
            status_value = str(response.get("status", ValidationStatus.INCONCLUSIVE.value)).strip()
            normalized = {
                "valid": ValidationStatus.VALID,
                "confirmed": ValidationStatus.VALID,
                "partial valid": ValidationStatus.PARTIAL_VALID,
                "partial": ValidationStatus.PARTIAL_VALID,
                "partially valid": ValidationStatus.PARTIAL_VALID,
                "inconclusive": ValidationStatus.INCONCLUSIVE,
                "uncertain": ValidationStatus.INCONCLUSIVE,
                "unknown": ValidationStatus.INCONCLUSIVE,
                "false positive": ValidationStatus.FALSE_POSITIVE,
                "rejected": ValidationStatus.FALSE_POSITIVE,
            }.get(status_value.lower())
            if normalized is not None:
                status = normalized
            else:
                try:
                    status = ValidationStatus(status_value)
                except ValueError:
                    status = ValidationStatus.INCONCLUSIVE
            return ValidationResult(
                status=status,
                analysis=str(response.get("analysis", "")),
            )

        payload: dict[str, object] = {
            "entry_function": unit.path.entry_function,
            "finding_name": analyzer.finding_name,
            "description": analyzer.description,
            "reason": analyzer.reason,
            "evidence_strength": analyzer.evidence_strength,
        }
        if path_context is not None:
            payload["call_chain"] = path_context.get("call_chain", [])
            payload["function_definitions"] = path_context.get("function_definitions", [])
            payload["referenced_symbols"] = path_context.get("referenced_symbols", [])
        result, _meta = invoke_agent(
            agent_name="validator",
            system_prompt=VALIDATOR_PROMPT,
            payload=payload,
            chat_model=self.chat_model,
            logger=self.logger,
            response_model=ValidationOutput,
            response_parser=_parse_response,
            prompt_version=VALIDATOR_PROMPT_VERSION,
            user_message=render_validator_markdown(payload),
        )
        if self.logger is not None:
            self.logger.debug_kv(
                "Validator agent call",
                runtime=_meta["runtime"],
                provider=_meta.get("provider_name"),
                prompt_version=_meta["prompt_version"],
            )
        return result

    def run_teaming(
        self,
        *,
        unit: AuditUnit,
        analyzer: AnalyzerResult,
        path_context: dict[str, object] | None = None,
        subagent_id: int | None = None,
        provider_name: str | None = None,
        debate_memory: "ConversationBuffer | None" = None,
    ) -> tuple[ValidationResult, dict[str, object]]:
        def _parse_response(response: dict[str, object]) -> ValidationResult:
            status_value = str(response.get("status", ValidationStatus.INCONCLUSIVE.value)).strip()
            normalized = {
                "valid": ValidationStatus.VALID,
                "confirmed": ValidationStatus.VALID,
                "partial valid": ValidationStatus.PARTIAL_VALID,
                "partial": ValidationStatus.PARTIAL_VALID,
                "partially valid": ValidationStatus.PARTIAL_VALID,
                "inconclusive": ValidationStatus.INCONCLUSIVE,
                "uncertain": ValidationStatus.INCONCLUSIVE,
                "unknown": ValidationStatus.INCONCLUSIVE,
                "false positive": ValidationStatus.FALSE_POSITIVE,
                "rejected": ValidationStatus.FALSE_POSITIVE,
            }.get(status_value.lower())
            if normalized is not None:
                status = normalized
            else:
                try:
                    status = ValidationStatus(status_value)
                except ValueError:
                    status = ValidationStatus.INCONCLUSIVE
            return ValidationResult(
                status=status,
                analysis=str(response.get("analysis", "")),
            )

        payload: dict[str, object] = {
            "entry_function": unit.path.entry_function,
            "finding_name": analyzer.finding_name,
            "description": analyzer.description,
            "reason": analyzer.reason,
            "evidence_strength": analyzer.evidence_strength,
        }
        if path_context is not None:
            payload["call_chain"] = path_context.get("call_chain", [])
            payload["function_definitions"] = path_context.get("function_definitions", [])
            payload["referenced_symbols"] = path_context.get("referenced_symbols", [])
        if debate_memory is not None:
            payload["debate_memory"] = debate_memory.snapshot()
        # After `restructure-audit-modes-and-coverage` Phase 1B's stage
        # reorder, the regular `validator` prompt no longer sees
        # exploitation context in any mode — making the previous
        # teaming-specific carve-out (`validator_teaming` v1)
        # redundant. Both single and teaming code paths now share
        # the `validator` v3 prompt; the only teaming-specific
        # behaviour is the multi-replica fan-out + debate, both of
        # which live in the orchestration layer above this method.
        result, meta = invoke_agent(
            agent_name="validator-teaming",
            system_prompt=VALIDATOR_PROMPT,
            payload=payload,
            chat_model=self.chat_model,
            logger=self.logger,
            response_model=ValidationOutput,
            response_parser=_parse_response,
            prompt_version=VALIDATOR_PROMPT_VERSION,
            user_message=render_validator_markdown(payload),
        )
        if self.logger is not None:
            self.logger.debug_kv(
                "Validator teaming agent call",
                runtime=meta["runtime"],
                provider=provider_name or meta.get("provider_name"),
                prompt_version=meta["prompt_version"],
                subagent_id=subagent_id if subagent_id is not None else "",
            )
        return result, meta


# ---------------------------------------------------------------------------
# Teaming-mode primitives
# ---------------------------------------------------------------------------

_JUDGE_PAYLOAD_BUDGET = 32_000
_EVIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}
_EXPLOIT_STATUS_RANK = {
    "exploitable": 3,
    "ready": 3,
    "uncertain": 2,
    "partial": 2,
    "partially_exploitable": 2,
    "not_exploitable": 1,
    "not_applicable": 0,
    "": 0,
}


@dataclass(frozen=True)
class AnalyzerSubagentRecord:
    subagent_index: int
    provider_name: str
    model_name: str
    result: AnalyzerResult


@dataclass(frozen=True)
class ValidatorSubagentRecord:
    subagent_index: int
    provider_name: str
    model_name: str
    result: ValidationResult


@dataclass(frozen=True)
class ExploiterSubagentRecord:
    subagent_index: int
    provider_name: str
    model_name: str
    result: ExploitationResult


@dataclass
class DebateTurn:
    round_index: int
    subagent_index: int
    provider_name: str
    system_prompt: str
    user_message: str
    raw_response: str
    verdict: str
    rebuttal: str


@dataclass
class DebateTranscript:
    finding_fingerprint: str
    max_rounds: int
    initial_verdicts: dict
    turns: list = field(default_factory=list)
    final_verdict: str = ""
    converged: bool = False


class ConversationBuffer:
    """Per-finding running conversation, modelled after langchain ConversationBufferMemory."""

    def __init__(self) -> None:
        self._messages: list[dict[str, str]] = []

    def add(self, role: str, content: str) -> None:
        self._messages.append({"role": role, "content": content})

    def snapshot(self) -> list[dict[str, str]]:
        return [dict(message) for message in self._messages]

    def to_text(self) -> str:
        return "\n\n".join(f"[{entry['role']}]\n{entry['content']}" for entry in self._messages)

    def clear(self) -> None:
        self._messages.clear()


def _redact(logger: RuntimeLogger | None, text: str) -> str:
    if logger is None:
        return text
    redactor = getattr(logger, "_redact", None)
    if callable(redactor):
        return redactor(text)
    return text


def _build_subagent_models(
    provider_list: tuple[str, ...],
    subagent_count: int,
    llm: LLMSettings,
    *,
    agent_role: str | None = None,
) -> list[ChatModel]:
    """Cycle the provider list with modulo to construct per-subagent chat models.

    When *agent_role* is supplied, the role's sampling override is overlaid on every
    cycled provider so teaming subagents inherit the role profile consistently.
    """
    if not provider_list:
        raise ValueError("provider_list must not be empty for a teaming subagent group.")
    override = llm.agent_overrides.get(agent_role) if agent_role else None
    models: list[ChatModel] = []
    for index in range(subagent_count):
        provider_name = provider_list[index % len(provider_list)]
        provider = llm.providers.get(provider_name)
        if provider is None:
            raise ValueError(f"Unknown teaming provider `{provider_name}`.")
        if override is not None:
            sampling_dict = override.sampling_overlay(provider)
        else:
            sampling_dict = provider.sampling_dict()
        models.append(
            build_chat_model(
                provider,
                provider_name=provider_name,
                sampling=sampling_dict,
            )
        )
    return models


def _payload_bytes(records: list[dict]) -> int:
    return len(json.dumps(records, default=str))


def _invoke_judge(
    *,
    chat_model: ChatModel,
    logger: RuntimeLogger | None,
    record_a: dict,
    record_b: dict,
) -> bool:
    payload = {"record_a": record_a, "record_b": record_b}
    result, _meta = invoke_agent(
        agent_name="dedup-judge",
        system_prompt=DEDUP_JUDGE_PROMPT,
        payload=payload,
        chat_model=chat_model,
        logger=logger,
        response_model=DedupJudgeOutput,
        response_parser=lambda response: bool(response.get("same", False)),
        prompt_version=DEDUP_JUDGE_PROMPT_VERSION,
    )
    return bool(result)


def _invoke_summary(
    *,
    chat_model: ChatModel,
    logger: RuntimeLogger | None,
    record: dict,
) -> str:
    payload = {"record": record}
    result, _meta = invoke_agent(
        agent_name="finding-summary",
        system_prompt=FINDING_SUMMARY_PROMPT,
        payload=payload,
        chat_model=chat_model,
        logger=logger,
        response_model=FindingSummaryOutput,
        response_parser=lambda response: _coerce_text(response.get("summary", "")),
        prompt_version=FINDING_SUMMARY_PROMPT_VERSION,
    )
    return str(result)


def _dedup_pipeline(
    *,
    records: list,
    to_judge_dict,
    exact_key,
    representative_rank,
    chat_model: ChatModel,
    logger: RuntimeLogger | None,
) -> list[list]:
    """Cluster ``records`` using exact-match then LLM-judge representatives.

    Returns a list of clusters; each cluster is a non-empty list of original records.
    """

    if not records:
        return []
    exact_clusters: dict[object, list] = {}
    for record in records:
        exact_clusters.setdefault(exact_key(record), []).append(record)
    clusters: list[list] = list(exact_clusters.values())
    if len(clusters) <= 1:
        return clusters
    representatives = [max(cluster, key=representative_rank) for cluster in clusters]
    judge_payloads = [to_judge_dict(rep) for rep in representatives]
    if _payload_bytes(judge_payloads) > _JUDGE_PAYLOAD_BUDGET:
        judge_payloads = [
            {"summary": _invoke_summary(chat_model=chat_model, logger=logger, record=payload)}
            for payload in judge_payloads
        ]
    merged: list[list] = []
    merged_indices: set[int] = set()
    for i in range(len(clusters)):
        if i in merged_indices:
            continue
        current_cluster = list(clusters[i])
        for j in range(i + 1, len(clusters)):
            if j in merged_indices:
                continue
            try:
                same = _invoke_judge(
                    chat_model=chat_model,
                    logger=logger,
                    record_a=judge_payloads[i],
                    record_b=judge_payloads[j],
                )
            except Exception as exc:  # pragma: no cover - defensive, judge failures degrade gracefully
                if logger is not None:
                    logger.warning(f"dedup judge failed: {exc}; treating clusters as distinct.")
                same = False
            if same:
                current_cluster.extend(clusters[j])
                merged_indices.add(j)
        merged.append(current_cluster)
        merged_indices.add(i)
    return merged


def _analyzer_record_key(record: AnalyzerSubagentRecord) -> tuple[str, str, int]:
    result = record.result
    return (
        (result.finding_name or "").casefold(),
        result.suspect_function_id or "",
        int(result.suspect_line or 0),
    )


def _analyzer_record_rank(record: AnalyzerSubagentRecord) -> int:
    return _EVIDENCE_RANK.get(record.result.evidence_strength.lower(), 0)


def _analyzer_to_judge(record: AnalyzerSubagentRecord) -> dict:
    result = record.result
    return {
        "finding_name": result.finding_name,
        "description": result.description,
        "analysis": result.analysis,
        "reason": result.reason,
        "suspect_function_id": result.suspect_function_id,
        "suspect_line": result.suspect_line,
        "evidence_strength": result.evidence_strength,
    }


_STEPS_FINGERPRINT_RE = re.compile(r"\s+")


def _exploit_record_key(record: ExploiterSubagentRecord) -> tuple[str, str]:
    steps = record.result.steps or ""
    fingerprint = _STEPS_FINGERPRINT_RE.sub(" ", steps.strip().casefold())[:240]
    return (record.result.status or "", fingerprint)


def _exploit_record_rank(record: ExploiterSubagentRecord) -> int:
    return _EXPLOIT_STATUS_RANK.get(record.result.status.lower(), 0)


def _exploit_to_judge(record: ExploiterSubagentRecord) -> dict:
    return {"status": record.result.status, "steps": record.result.steps}


def _synth_validator_from_context(
    validator_context: dict[str, object] | None,
) -> ValidationResult:
    """Synthesize a `ValidationResult` from a validator_context dict.

    `wire-agentic-into-workflow` Phase 2 — `ExploiterTeam` previously
    passed `validator_context` to `ExploitationAgent.run(...)` as an
    extra payload, but `AgenticStageRunner.run_exploiter(...)`
    requires a typed `ValidationResult`. Bridge by reconstructing one
    from the context dict (or default to Inconclusive when missing).
    """

    if not validator_context:
        return ValidationResult(status=ValidationStatus.INCONCLUSIVE, analysis="")
    raw_status = str(validator_context.get("validator_verdict", "")).strip()
    status_map = {
        "Valid": ValidationStatus.VALID,
        "Partial Valid": ValidationStatus.PARTIAL_VALID,
        "Inconclusive": ValidationStatus.INCONCLUSIVE,
        "False Positive": ValidationStatus.FALSE_POSITIVE,
    }
    status = status_map.get(raw_status, ValidationStatus.INCONCLUSIVE)
    analysis = str(validator_context.get("validator_analysis", "") or "")
    return ValidationResult(status=status, analysis=analysis)


class AnalyzerTeam:
    def __init__(
        self,
        *,
        stage_cfg: AuditAnalyzerStageConfig,
        replication: int,
        llm: LLMSettings,
        logger: RuntimeLogger | None = None,
        agentic_stage_runner: object | None = None,
        personas: tuple = (),
    ) -> None:
        self.stage_cfg = stage_cfg
        self.replication = max(1, replication)
        self.llm = llm
        self.logger = logger
        self._cancellation: "RunCancellation | None" = None
        # `wire-agentic-into-workflow` Phase 2 — when set, per-replica
        # `_invoke(...)` dispatches through this stage runner (a real
        # `AgenticStageRunner` from the workflow's `from_config`)
        # instead of constructing per-replica `AnalyzerAgent`
        # instances. `personas` is the resolved persona tuple
        # (length == replication); each replica's call carries
        # `personas[index]` so the agent system prompt diverges
        # per-replica even though the underlying transport is shared.
        self.agentic_stage_runner = agentic_stage_runner
        self.personas = personas

    def set_cancellation(self, cancellation: "RunCancellation | None") -> None:
        """Inject the run-level cancellation token; called by ``AuditWorkflow``."""
        self._cancellation = cancellation

    def run(
        self,
        *,
        unit: AuditUnit,
        path_functions: list[FunctionRecord],
        path_context: dict[str, object] | None = None,
    ) -> tuple[list[AnalyzerResult], list[AnalyzerSubagentRecord]]:
        records: list[AnalyzerSubagentRecord] = []

        if self.agentic_stage_runner is not None:
            # Agentic dispatch — per-replica diversity via persona
            # injection; transport (coder-service `/agent_invocations`)
            # is shared across replicas, so per-provider per-model
            # rotation is irrelevant.
            replica_count = self.replication

            def _invoke_agentic(index: int) -> AnalyzerSubagentRecord:
                persona = self.personas[index] if index < len(self.personas) else None
                result = self.agentic_stage_runner.run_analyzer(
                    unit=unit,
                    path_functions=path_functions,
                    path_context=path_context,
                    persona=persona,
                    subagent_id=index,
                )
                return AnalyzerSubagentRecord(
                    subagent_index=index,
                    provider_name="coder-service",
                    model_name=getattr(persona, "name", "") if persona else "",
                    result=result,
                )

            with ThreadPoolExecutor(max_workers=replica_count) as pool:
                futures = [pool.submit(_invoke_agentic, idx) for idx in range(replica_count)]
                records.extend(
                    _run_subagents_with_cancel(pool, futures, self._cancellation)
                )
            records.sort(key=lambda record: record.subagent_index)
        else:
            subagent_models = _build_subagent_models(
                self.stage_cfg.provider_list,
                self.replication,
                self.llm,
                agent_role="auditor",
            )

            def _invoke(index: int, model: ChatModel) -> AnalyzerSubagentRecord:
                agent = AnalyzerAgent(chat_model=model, logger=self.logger)
                result = agent.run(
                    unit=unit,
                    path_functions=path_functions,
                    path_context=path_context,
                    subagent_id=index,
                    provider_name=model.provider_name,
                )
                return AnalyzerSubagentRecord(
                    subagent_index=index,
                    provider_name=model.provider_name,
                    model_name=model.model_name,
                    result=result,
                )

            try:
                with ThreadPoolExecutor(max_workers=self.replication) as pool:
                    futures = [pool.submit(_invoke, idx, model) for idx, model in enumerate(subagent_models)]
                    records.extend(
                        _run_subagents_with_cancel(pool, futures, self._cancellation)
                    )
            finally:
                subagent_models.clear()
            records.sort(key=lambda record: record.subagent_index)

        candidates = [record for record in records if record.result.status == "candidate"]
        if not candidates:
            return [], records

        judge_provider = self.stage_cfg.provider_list[0]
        judge_provider_config = self.llm.providers.get(judge_provider)
        if judge_provider_config is None:
            raise ValueError(f"Unknown analyzer judge provider `{judge_provider}`.")
        judge_model = build_chat_model(judge_provider_config, provider_name=judge_provider)
        try:
            clusters = _dedup_pipeline(
                records=candidates,
                to_judge_dict=_analyzer_to_judge,
                exact_key=_analyzer_record_key,
                representative_rank=_analyzer_record_rank,
                chat_model=judge_model,
                logger=self.logger,
            )
        finally:
            judge_model = None  # release
        consolidated = [
            max(cluster, key=_analyzer_record_rank).result for cluster in clusters
        ]
        return consolidated, records


class ValidatorTeam:
    def __init__(
        self,
        *,
        stage_cfg: AuditValidatorStageConfig,
        replication: int,
        llm: LLMSettings,
        logger: RuntimeLogger | None = None,
    ) -> None:
        self.stage_cfg = stage_cfg
        self.replication = max(1, replication)
        self.llm = llm
        self.logger = logger
        self._cancellation: "RunCancellation | None" = None

    def set_cancellation(self, cancellation: "RunCancellation | None") -> None:
        self._cancellation = cancellation

    def run_for_finding(
        self,
        *,
        unit: AuditUnit,
        analyzer: AnalyzerResult,
        path_context: dict[str, object] | None,
        finding_fingerprint: str,
    ) -> tuple[ValidationResult, list[ValidatorSubagentRecord], DebateTranscript | None]:
        subagent_models = _build_subagent_models(
            self.stage_cfg.provider_list,
            self.replication,
            self.llm,
            agent_role="validator",
        )
        records: list[ValidatorSubagentRecord] = []

        def _invoke(index: int, model: ChatModel) -> ValidatorSubagentRecord:
            agent = ValidatorAgent(chat_model=model, logger=self.logger)
            result, _meta = agent.run_teaming(
                unit=unit,
                analyzer=analyzer,
                path_context=path_context,
                subagent_id=index,
                provider_name=model.provider_name,
            )
            return ValidatorSubagentRecord(
                subagent_index=index,
                provider_name=model.provider_name,
                model_name=model.model_name,
                result=result,
            )

        try:
            with ThreadPoolExecutor(max_workers=self.replication) as pool:
                futures = [pool.submit(_invoke, idx, model) for idx, model in enumerate(subagent_models)]
                records.extend(
                    _run_subagents_with_cancel(pool, futures, self._cancellation)
                )
            records.sort(key=lambda record: record.subagent_index)

            statuses = {record.result.status for record in records}
            if len(statuses) <= 1:
                final = records[0].result if records else ValidationResult(
                    status=ValidationStatus.INCONCLUSIVE, analysis=""
                )
                return final, records, None

            transcript = self._run_debate(
                unit=unit,
                analyzer=analyzer,
                path_context=path_context,
                records=records,
                subagent_models=subagent_models,
                finding_fingerprint=finding_fingerprint,
                rounds=self.stage_cfg.debate.max_rounds,
            )
            final_status_text = (transcript.final_verdict or "").strip().lower()
            status_map = {
                "valid": ValidationStatus.VALID,
                "partial valid": ValidationStatus.PARTIAL_VALID,
                "inconclusive": ValidationStatus.INCONCLUSIVE,
                "false positive": ValidationStatus.FALSE_POSITIVE,
            }
            final_status = status_map.get(final_status_text, ValidationStatus.INCONCLUSIVE)
            final_analysis = "\n\n".join(
                f"subagent-{record.subagent_index}-{record.provider_name}: {record.result.analysis}"
                for record in records
            )
            return (
                ValidationResult(status=final_status, analysis=final_analysis),
                records,
                transcript,
            )
        finally:
            subagent_models.clear()

    def _run_debate(
        self,
        *,
        unit: AuditUnit,
        analyzer: AnalyzerResult,
        path_context: dict[str, object] | None,
        records: list[ValidatorSubagentRecord],
        subagent_models: list[ChatModel],
        finding_fingerprint: str,
        rounds: int,
    ) -> DebateTranscript:
        ranked = sorted(records, key=lambda record: record.result.status.value)
        debater_a = ranked[0]
        debater_b = ranked[-1]
        initial_verdicts = {
            record.subagent_index: record.result.status.value for record in records
        }
        transcript = DebateTranscript(
            finding_fingerprint=finding_fingerprint,
            max_rounds=rounds,
            initial_verdicts=initial_verdicts,
        )
        memory = ConversationBuffer()
        memory.add(
            "analyzer_finding",
            json.dumps(
                {
                    "finding_name": analyzer.finding_name,
                    "description": analyzer.description,
                    "reason": analyzer.reason,
                    "evidence_strength": analyzer.evidence_strength,
                },
                default=str,
            ),
        )
        for record in records:
            memory.add(
                f"subagent-{record.subagent_index}-{record.provider_name}:initial",
                json.dumps(
                    {"verdict": record.result.status.value, "analysis": record.result.analysis},
                    default=str,
                ),
            )

        current_verdicts = {
            debater_a.subagent_index: debater_a.result.status.value,
            debater_b.subagent_index: debater_b.result.status.value,
        }

        for round_index in range(1, rounds + 1):
            for debater in (debater_a, debater_b):
                model = subagent_models[debater.subagent_index]
                payload = {
                    "finding_name": analyzer.finding_name,
                    "description": analyzer.description,
                    "reason": analyzer.reason,
                    "evidence_strength": analyzer.evidence_strength,
                    "current_verdicts": current_verdicts,
                    "my_last_verdict": current_verdicts[debater.subagent_index],
                    "debate_memory": memory.snapshot(),
                    "round": round_index,
                    "max_rounds": rounds,
                }
                user_message = json.dumps(payload, default=str, indent=2)
                raw_response = model.invoke_text(VALIDATOR_DEBATE_PROMPT, payload)
                try:
                    parsed_response = json.loads(raw_response)
                except (ValueError, TypeError):
                    parsed_response = {"verdict": current_verdicts[debater.subagent_index], "rebuttal": raw_response}
                verdict = str(parsed_response.get("verdict", "")).strip()
                rebuttal = _coerce_text(parsed_response.get("rebuttal", ""))
                current_verdicts[debater.subagent_index] = verdict
                turn = DebateTurn(
                    round_index=round_index,
                    subagent_index=debater.subagent_index,
                    provider_name=debater.provider_name,
                    system_prompt=_redact(self.logger, VALIDATOR_DEBATE_PROMPT),
                    user_message=_redact(self.logger, user_message),
                    raw_response=_redact(self.logger, str(raw_response)),
                    verdict=verdict,
                    rebuttal=rebuttal,
                )
                transcript.turns.append(turn)
                memory.add(
                    f"subagent-{debater.subagent_index}-{debater.provider_name}:round-{round_index}",
                    json.dumps({"verdict": verdict, "rebuttal": rebuttal}, default=str),
                )
            verdict_values = {value.strip().lower() for value in current_verdicts.values()}
            if len(verdict_values) <= 1:
                transcript.converged = True
                transcript.final_verdict = next(iter(verdict_values))
                break
        else:
            transcript.converged = False
            transcript.final_verdict = max(
                (str(value).strip().lower() for value in current_verdicts.values()),
                key=lambda value: {
                    "valid": 4,
                    "partial valid": 3,
                    "inconclusive": 2,
                    "false positive": 1,
                }.get(value, 0),
            )
        memory.clear()
        return transcript


class ExploiterTeam:
    def __init__(
        self,
        *,
        stage_cfg: AuditExploiterStageConfig,
        replication: int,
        llm: LLMSettings,
        logger: RuntimeLogger | None = None,
        agentic_stage_runner: object | None = None,
        personas: tuple = (),
    ) -> None:
        self.stage_cfg = stage_cfg
        self.replication = max(1, replication)
        self.llm = llm
        self.logger = logger
        self._cancellation: "RunCancellation | None" = None
        # `wire-agentic-into-workflow` Phase 2 — same pattern as
        # AnalyzerTeam: when set, per-replica `_invoke(...)` dispatches
        # through this stage runner instead of constructing
        # `ExploitationAgent` per replica.
        self.agentic_stage_runner = agentic_stage_runner
        self.personas = personas

    def set_cancellation(self, cancellation: "RunCancellation | None") -> None:
        self._cancellation = cancellation

    def run_for_finding(
        self,
        *,
        unit: AuditUnit,
        analyzer: AnalyzerResult,
        path_context: dict[str, object] | None,
        validator_context: dict[str, object] | None = None,
    ) -> tuple[ExploitationResult, list[ExploiterSubagentRecord]]:
        records: list[ExploiterSubagentRecord] = []

        if self.agentic_stage_runner is not None:
            # Agentic dispatch — per-replica diversity via personas;
            # the agentic runner's `run_exploiter` requires a
            # `validator: ValidationResult` arg, so we synthesize one
            # from the validator_context (or an empty default if not
            # provided).
            replica_count = self.replication
            synth_validator = _synth_validator_from_context(validator_context)

            def _invoke_agentic(index: int) -> ExploiterSubagentRecord:
                persona = self.personas[index] if index < len(self.personas) else None
                result = self.agentic_stage_runner.run_exploiter(
                    unit=unit,
                    analyzer=analyzer,
                    validator=synth_validator,
                    path_context=path_context,
                    persona=persona,
                )
                return ExploiterSubagentRecord(
                    subagent_index=index,
                    provider_name="coder-service",
                    model_name=getattr(persona, "name", "") if persona else "",
                    result=result,
                )

            with ThreadPoolExecutor(max_workers=replica_count) as pool:
                futures = [pool.submit(_invoke_agentic, idx) for idx in range(replica_count)]
                records.extend(
                    _run_subagents_with_cancel(pool, futures, self._cancellation)
                )
            records.sort(key=lambda record: record.subagent_index)
        else:
            subagent_models = _build_subagent_models(
                self.stage_cfg.provider_list,
                self.replication,
                self.llm,
                agent_role="exploitation",
            )

            def _invoke(index: int, model: ChatModel) -> ExploiterSubagentRecord:
                agent = ExploitationAgent(chat_model=model, logger=self.logger)
                result = agent.run(
                    unit=unit,
                    analyzer=analyzer,
                    path_context=path_context,
                    subagent_id=index,
                    provider_name=model.provider_name,
                    extra_payload=dict(validator_context) if validator_context else None,
                )
                return ExploiterSubagentRecord(
                    subagent_index=index,
                    provider_name=model.provider_name,
                    model_name=model.model_name,
                    result=result,
                )

            try:
                with ThreadPoolExecutor(max_workers=self.replication) as pool:
                    futures = [pool.submit(_invoke, idx, model) for idx, model in enumerate(subagent_models)]
                    records.extend(
                        _run_subagents_with_cancel(pool, futures, self._cancellation)
                    )
            finally:
                subagent_models.clear()
            records.sort(key=lambda record: record.subagent_index)

        judge_provider = self.stage_cfg.provider_list[0]
        judge_provider_config = self.llm.providers.get(judge_provider)
        if judge_provider_config is None:
            raise ValueError(f"Unknown exploiter judge provider `{judge_provider}`.")
        judge_model = build_chat_model(judge_provider_config, provider_name=judge_provider)
        try:
            clusters = _dedup_pipeline(
                records=records,
                to_judge_dict=_exploit_to_judge,
                exact_key=_exploit_record_key,
                representative_rank=_exploit_record_rank,
                chat_model=judge_model,
                logger=self.logger,
            )
        finally:
            judge_model = None

        if not clusters:
            return ExploitationResult(status="not_applicable", steps=""), records
        surviving_representatives = [
            max(cluster, key=_exploit_record_rank) for cluster in clusters
        ]
        best = max(surviving_representatives, key=_exploit_record_rank)
        consolidated_status = best.result.status
        step_blocks: list[str] = []
        for representative in surviving_representatives:
            label = f"subagent-{representative.subagent_index}-{representative.provider_name}"
            step_blocks.append(f"### {label}\n\n{representative.result.steps}")
        consolidated_steps = "\n\n".join(step_blocks)
        return ExploitationResult(status=consolidated_status, steps=consolidated_steps), records
