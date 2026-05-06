"""StageRunner Protocol + Persona seeding.

`restructure-audit-modes-and-coverage` Phase 4A introduces the
`StageRunner` seam so deep mode can swap the per-stage execution
form (`prompt` vs `agentic`) without touching workflow code.

Phase 4A ships:

- `StageRunner` Protocol — the contract every per-stage runner
  honors (analyzer / validator / exploiter).
- `PromptStageRunner` — wraps today's `LLMClient`-based flow.
  Used by `fast` mode and by `deep` mode when `audit.stages.form
  = "prompt"` (deep operators who want replication / debate
  diversity without the agentic cost).
- `AgenticStageRunner` — stub that raises `NotImplementedError`
  with a clear message naming the deferred SDK choice.
  Constructed only when `audit.stages.form = "agentic"`.
- `Persona` dataclass + `DEFAULT_DEEP_PERSONAS` constant + the
  `resolve_personas(personas, replication)` resolver that maps
  per-replica personas (with repeat-from-start when replication
  exceeds the persona list).
- `estimate_audit_cost(plan, mode_config)` — pre-flight
  cost / token estimate emitted to stderr before any LLM call so
  operators see the order of magnitude before the audit starts
  burning budget.

Deferred (tracked in `proposal.md` "Known limitations" §
"Phase 4 agentic stage runner"):

- Real Claude Code Agent SDK integration (Task 4.3.1's
  decision: in-process SDK vs `claude-code` subprocess).
- `final_answer` tool registration that enforces the same
  Pydantic response model as the prompt form.
- Tool-grant scoping (`read_file`, `grep`, `query_graph`,
  optional `read_history` / `read_config`).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from xauditor.audit.agent_transport import (
    AgentResult,
    AgentTransport,
    CoderServiceAgentTransport,
)
from xauditor.audit.agents import (
    AnalyzerAgent,
    AnalyzerResult,
    ExploitationAgent,
    ExploitationResult,
    ValidationResult,
    ValidatorAgent,
)
from xauditor.audit.units import AuditUnit
from xauditor.llm_outputs import (
    AnalyzerOutput,
    ExploitationOutput,
    ValidationOutput,
)
from xauditor.models import ValidationStatus
from xauditor.config import AuditModeConfig, CoderConfig
from xauditor.runtime_logging import RuntimeLogger


StageForm = Literal["prompt", "agentic"]


# ---------------------------------------------------------------------------
# Persona — replication diversity for deep mode
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Persona:
    """A diversity-seeding persona for one replica of a stage.

    Used in deep mode when `audit.replication.<stage> > 1` to
    nudge each replica toward a different aspect of the same
    audit unit. Persona is injected as an `extra_system_prefix`
    appended to the stage's base system prompt.

    `tool_emphasis` is consumed by `AgenticStageRunner` only —
    when present, the agent is biased toward the named tools
    (e.g. an `auth_boundaries` persona emphasizes `grep` for
    middleware registration sites). `PromptStageRunner` ignores
    this field.
    """

    name: str
    focus_summary: str
    extra_system_prefix: str
    tool_emphasis: tuple[str, ...] = ()


DEFAULT_DEEP_PERSONAS: tuple[Persona, ...] = (
    Persona(
        name="data_flow",
        focus_summary="Linear taint propagation through the slice",
        extra_system_prefix=(
            "Focus on data flow from sources to sinks. Pay close "
            "attention to sanitization, encoding, and type "
            "transformations."
        ),
        tool_emphasis=("read_file", "query_graph"),
    ),
    Persona(
        name="auth_boundaries",
        focus_summary="Authentication / authorization gaps",
        extra_system_prefix=(
            "Focus on trust-boundary transitions. Inspect "
            "decorator chains, middleware registration, and "
            "entry classification before evaluating any sink."
        ),
        tool_emphasis=("read_file", "grep", "query_graph"),
    ),
    Persona(
        name="config_assumptions",
        focus_summary="Deployment / config-driven assumptions",
        extra_system_prefix=(
            "Focus on assumptions the code makes about its "
            "deployment context: internal-network only, "
            "feature-flagged, debug-only routes, etc. Use "
            "read_config when available."
        ),
        tool_emphasis=("read_config", "grep", "read_file"),
    ),
)


def resolve_personas(
    personas: tuple[Persona, ...] | None, replication: int
) -> tuple[Persona | None, ...]:
    """Map `replication` replicas to a persona each.

    - When `replication == 1` (fast mode), persona is dropped —
      returns `(None,)`. Single-replica runs have no diversity
      dimension to exploit.
    - When `personas` is empty AND `replication > 1`, defaults to
      `DEFAULT_DEEP_PERSONAS` cycling.
    - When `replication > len(personas)`, repeats from the start
      of the list (replica `i` gets `personas[i % len(personas)]`).

    The result is always a tuple of length `replication` — `None`
    entries indicate "no persona" (fast-mode default), `Persona`
    entries indicate a seeded replica.
    """

    if replication < 1:
        raise ValueError(f"replication must be >= 1, got {replication}")
    if replication == 1:
        return (None,)
    pool = personas if personas else DEFAULT_DEEP_PERSONAS
    if not pool:
        return tuple(None for _ in range(replication))
    return tuple(pool[i % len(pool)] for i in range(replication))


# ---------------------------------------------------------------------------
# StageRunner Protocol
# ---------------------------------------------------------------------------


class StageRunner(Protocol):
    """Per-stage runner contract.

    Implementations: `PromptStageRunner` (Phase 4A real),
    `AgenticStageRunner` (Phase 4A stub).

    Each method receives the audit unit, the upstream stage's
    output (when relevant), and an optional `persona`. The
    persona is `None` for single-replica runs (fast mode) and
    populated for multi-replica deep-mode runs.
    """

    def run_analyzer(
        self,
        *,
        unit: AuditUnit,
        path_functions: list,
        path_context: dict[str, object] | None = None,
        excluded_findings: tuple[dict[str, object], ...] = (),
        persona: Persona | None = None,
        subagent_id: int | None = None,
        provider_name: str | None = None,
    ) -> AnalyzerResult:
        ...

    def run_validator(
        self,
        *,
        unit: AuditUnit,
        analyzer: AnalyzerResult,
        path_context: dict[str, object] | None = None,
        persona: Persona | None = None,
    ) -> ValidationResult:
        ...

    def run_exploiter(
        self,
        *,
        unit: AuditUnit,
        analyzer: AnalyzerResult,
        validator: ValidationResult,
        path_context: dict[str, object] | None = None,
        persona: Persona | None = None,
    ) -> ExploitationResult:
        ...


# ---------------------------------------------------------------------------
# PromptStageRunner — the prompt-driven runner used by fast mode today
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromptStageRunner:
    """Wraps the existing `AnalyzerAgent` / `ValidatorAgent` /
    `ExploitationAgent` flow.

    Persona, when present, is logged for telemetry but not
    materially injected into the prompt today — single-call
    JSON-mode prompts don't benefit much from persona-seeded
    system-prefix variation, since the analyzer's own prompt is
    already constrained to one finding per call. Future work
    may inject persona prefix into the `extra_system` slot of
    `LLMClient._invoke_json`; Phase 4A treats persona as a
    no-op here.

    The runner is unit-shape-agnostic — it consumes the
    `AuditUnit` Protocol surface
    (`unit.unit_kind` / `to_*_payload`) only when a future
    follow-up wires unit-kind-specific prompt selection. Phase
    4A passes the `path_functions` / `path_context` arguments
    through verbatim, matching today's behaviour.
    """

    analyzer_agent: AnalyzerAgent
    validator_agent: ValidatorAgent
    exploitation_agent: ExploitationAgent
    logger: RuntimeLogger | None = None

    def pop_transcripts_for(
        self, unit_id: str
    ) -> tuple[dict[str, object], ...]:
        """No-op for the prompt runner — no agent tool transcripts
        exist when stage execution is a single JSON-mode chat
        completion. Always returns an empty tuple.
        """

        del unit_id
        return ()

    def run_analyzer(
        self,
        *,
        unit: AuditUnit,
        path_functions: list,
        path_context: dict[str, object] | None = None,
        excluded_findings: tuple[dict[str, object], ...] = (),
        persona: Persona | None = None,
        subagent_id: int | None = None,
        provider_name: str | None = None,
    ) -> AnalyzerResult:
        if persona is not None and self.logger is not None:
            self.logger.debug_kv(
                "PromptStageRunner persona (analyzer)",
                persona=persona.name,
                focus=persona.focus_summary,
            )
        # Forward subagent_id / provider_name only when explicitly set
        # so existing test stubs whose `run(...)` signatures don't
        # accept them keep working unchanged.
        kwargs: dict[str, object] = {}
        if subagent_id is not None:
            kwargs["subagent_id"] = subagent_id
        if provider_name is not None:
            kwargs["provider_name"] = provider_name
        return self.analyzer_agent.run(
            unit=_legacy_unit(unit),
            path_functions=path_functions,
            path_context=path_context,
            excluded_findings=excluded_findings,
            **kwargs,
        )

    def run_validator(
        self,
        *,
        unit: AuditUnit,
        analyzer: AnalyzerResult,
        path_context: dict[str, object] | None = None,
        persona: Persona | None = None,
    ) -> ValidationResult:
        if persona is not None and self.logger is not None:
            self.logger.debug_kv(
                "PromptStageRunner persona (validator)",
                persona=persona.name,
            )
        return self.validator_agent.run(
            unit=_legacy_unit(unit),
            analyzer=analyzer,
            path_context=path_context,
        )

    def run_exploiter(
        self,
        *,
        unit: AuditUnit,
        analyzer: AnalyzerResult,
        validator: ValidationResult,
        path_context: dict[str, object] | None = None,
        persona: Persona | None = None,
    ) -> ExploitationResult:
        del validator  # exploiter prompt doesn't consume the validator block today
        if persona is not None and self.logger is not None:
            self.logger.debug_kv(
                "PromptStageRunner persona (exploiter)",
                persona=persona.name,
            )
        return self.exploitation_agent.run(
            unit=_legacy_unit(unit),
            analyzer=analyzer,
            path_context=path_context,
        )


# ---------------------------------------------------------------------------
# AgenticStageRunner — Phase 4A stub
# ---------------------------------------------------------------------------


# Resolution table for the agentic stage prompts. Falls back to
# the prompt-form prompts when the agentic variant doesn't exist
# for a given unit kind. Populated lazily to avoid import cycles
# with `prompts.py`.
_AGENTIC_PROMPT_TABLE: dict[tuple[str, str], str] = {}


def _resolve_agentic_prompt(unit_kind: str, stage: str) -> str:
    """Pick the system prompt for an agentic stage call.

    Resolution order:
    1. Unit-kind-specific agentic prompt (e.g. `analyzer_sink_agentic`)
       — not yet shipped; reserved for future expansion.
    2. Generic agentic prompt (`analyzer_agentic` /
       `validator_agentic` / `exploiter_agentic`).
    3. Unit-kind-specific prompt-form prompt (e.g. `analyzer_sink`).
    4. Generic prompt-form prompt (`analyzer` / `validator` /
       `exploitation`).
    """

    from xauditor.prompts import get_prompt

    # Generic agentic always wins for now (Phase 1 of this change).
    candidates: list[str] = []
    candidates.append(f"{stage}_agentic")
    if unit_kind != "path":
        candidates.append(f"{stage}_{unit_kind}")
    # Stage-name → registry-key mapping for the prompt-form
    # fallback. The legacy registry uses `exploitation` (not
    # `exploiter`); the agentic registry uses `exploiter_agentic`.
    legacy_stage = {
        "analyzer": "analyzer",
        "validator": "validator",
        "exploiter": "exploitation",
    }[stage]
    candidates.append(legacy_stage)
    for name in candidates:
        try:
            return get_prompt(name)
        except KeyError:
            continue
    raise RuntimeError(
        f"No prompt found for stage={stage!r} unit_kind={unit_kind!r}"
    )


def _resolve_payload(
    unit: AuditUnit, stage: str, path_context: dict[str, object] | None
) -> dict[str, object]:
    """Project the unit + path_context into the agent's user payload.

    Uses the unit's `to_<stage>_payload()` method when available
    (Phase 3A's PathAuditUnit + future kinds), else falls back to
    the path_context dict directly.
    """

    method = getattr(unit, f"to_{stage}_payload", None)
    if method is not None:
        payload = method()
    else:
        payload = dict(path_context or {})
    return payload


def _unit_id_for(unit: AuditUnit) -> str:
    explicit = getattr(unit, "unit_id", None)
    if explicit:
        return str(explicit)
    # Legacy `models.AuditUnit` fallback — the path fingerprint is
    # the stable per-unit identifier today.
    path = getattr(unit, "path", None)
    return getattr(path, "path_fingerprint", "") if path is not None else ""


def _unit_kind_for(unit: AuditUnit) -> str:
    """Return the unit kind for prompt resolution. Defaults to
    ``"path"`` for legacy `models.AuditUnit` instances that don't
    carry the `AuditUnit` Protocol's `unit_kind` field."""

    return str(getattr(unit, "unit_kind", "") or "path")


def _analyzer_to_payload(analyzer: AnalyzerResult) -> dict[str, object]:
    return {
        "status": analyzer.status,
        "finding_name": analyzer.finding_name,
        "description": analyzer.description,
        "analysis": analyzer.analysis,
        "reason": analyzer.reason,
        "context_notes": analyzer.context_notes,
        "suspect_function_id": analyzer.suspect_function_id,
        "suspect_line": analyzer.suspect_line,
        "evidence_strength": analyzer.evidence_strength,
    }


def _coerce_analyzer(payload: object) -> AnalyzerResult:
    """Convert an `AnalyzerOutput` (or fallback dict) into the
    legacy `AnalyzerResult` dataclass downstream code expects."""
    get = _getter(payload)
    return AnalyzerResult(
        status=str(get("status", "no_issue")),
        finding_name=str(get("finding_name", "") or ""),
        description=str(get("description", "") or ""),
        analysis=str(get("analysis", "") or ""),
        reason=str(get("reason", "") or ""),
        context_notes=str(get("context_notes", "") or ""),
        suspect_function_id=str(get("suspect_function_id", "") or ""),
        suspect_line=int(get("suspect_line", 0) or 0),
        evidence_strength=str(get("evidence_strength", "low") or "low"),
    )


def _coerce_validator(payload: object) -> ValidationResult:
    get = _getter(payload)
    raw_status = str(get("status", "Inconclusive") or "Inconclusive").strip()
    normalized = {
        "valid": ValidationStatus.VALID,
        "partial valid": ValidationStatus.PARTIAL_VALID,
        "partial": ValidationStatus.PARTIAL_VALID,
        "inconclusive": ValidationStatus.INCONCLUSIVE,
        "false positive": ValidationStatus.FALSE_POSITIVE,
    }.get(raw_status.lower())
    if normalized is None:
        try:
            normalized = ValidationStatus(raw_status)
        except ValueError:
            normalized = ValidationStatus.INCONCLUSIVE
    return ValidationResult(
        status=normalized,
        analysis=str(get("analysis", "") or ""),
    )


def _coerce_exploiter(payload: object) -> ExploitationResult:
    get = _getter(payload)
    return ExploitationResult(
        status=str(get("status", "not_applicable") or "not_applicable"),
        steps=str(get("steps", "") or ""),
    )


def _getter(payload: object) -> Any:
    """Return a uniform `.get(key, default)` accessor for either a
    Pydantic v2 model instance or a dict."""
    if isinstance(payload, dict):
        return payload.get
    # Pydantic v2 instance
    dump = getattr(payload, "model_dump", None)
    if dump is not None:
        try:
            data = dump()
        except Exception:  # noqa: BLE001
            data = {}
        return data.get
    return lambda key, default=None: getattr(payload, key, default)


@dataclass
class AgenticStageRunner:
    """Real `claude-code`-subprocess-backed stage runner.

    Replaces the Phase 4A stub. Each stage call delegates to an
    `AgentTransport` (production: `CoderServiceAgentTransport`;
    test: `MockAgentTransport`).

    Persona injection appends the persona's
    `extra_system_prefix` to the stage's base system prompt and
    passes the persona's `tool_emphasis` to the transport as a
    hint.

    `agentic_transcripts` accumulates per-call transcripts so the
    workflow's `_build_finding` can attach them to findings for
    persistence on the new `findings.agentic_transcript` JSONB
    column (alembic 0013).
    """

    transport: AgentTransport
    timeout_seconds: int = 300
    project: str | None = None
    logger: RuntimeLogger | None = None
    agentic_transcripts: list[tuple[str, str, AgentResult]] = field(
        default_factory=list
    )

    def pop_transcripts_for(
        self, unit_id: str
    ) -> tuple[dict[str, object], ...]:
        """Drain + return transcripts collected for a given unit_id.

        Workflow's per-unit processing calls this after each finding's
        per-stage chain completes; the returned list is attached to
        the finding as `Finding.agentic_transcript` for the Postgres
        sink to persist (alembic 0013).
        """

        keep: list[tuple[str, str, AgentResult]] = []
        drained: list[dict[str, object]] = []
        for entry in self.agentic_transcripts:
            entry_unit_id, stage, result = entry
            if entry_unit_id == unit_id:
                drained.append(
                    {
                        "stage": stage,
                        "tool_call_count": result.tool_call_count,
                        "fell_back": result.fell_back,
                        "fallback_reason": result.fallback_reason,
                        "transcript": result.transcript_as_payload(),
                    }
                )
            else:
                keep.append(entry)
        self.agentic_transcripts = keep
        return tuple(drained)

    def run_analyzer(
        self,
        *,
        unit: AuditUnit,
        path_functions: list,
        path_context: dict[str, object] | None = None,
        excluded_findings: tuple[dict[str, object], ...] = (),
        persona: Persona | None = None,
        subagent_id: int | None = None,
        provider_name: str | None = None,
    ) -> AnalyzerResult:
        del path_functions, provider_name
        system_prompt = _resolve_agentic_prompt(_unit_kind_for(unit), "analyzer")
        if persona is not None:
            system_prompt = f"{system_prompt}\n\n{persona.extra_system_prefix}"
        payload = _resolve_payload(unit, "analyzer", path_context)
        if excluded_findings:
            payload["excluded_findings"] = list(excluded_findings)
        result = self.transport.invoke(
            system_prompt=system_prompt,
            user_payload=payload,
            response_model=AnalyzerOutput,
            timeout_seconds=self.timeout_seconds,
            project=self.project,
        )
        self.agentic_transcripts.append((_unit_id_for(unit), "analyzer", result))
        if self.logger is not None:
            self.logger.debug_kv(
                "AgenticStageRunner analyzer",
                unit_id=_unit_id_for(unit),
                tool_call_count=result.tool_call_count,
                fell_back=result.fell_back,
                fallback_reason=result.fallback_reason,
                persona=persona.name if persona is not None else "",
                subagent_id=subagent_id if subagent_id is not None else "",
            )
        return _coerce_analyzer(result.final_answer)

    def run_validator(
        self,
        *,
        unit: AuditUnit,
        analyzer: AnalyzerResult,
        path_context: dict[str, object] | None = None,
        persona: Persona | None = None,
    ) -> ValidationResult:
        system_prompt = _resolve_agentic_prompt(_unit_kind_for(unit), "validator")
        if persona is not None:
            system_prompt = f"{system_prompt}\n\n{persona.extra_system_prefix}"
        payload = _resolve_payload(unit, "validator", path_context)
        payload["candidate_finding"] = _analyzer_to_payload(analyzer)
        result = self.transport.invoke(
            system_prompt=system_prompt,
            user_payload=payload,
            response_model=ValidationOutput,
            timeout_seconds=self.timeout_seconds,
            project=self.project,
        )
        self.agentic_transcripts.append((_unit_id_for(unit), "validator", result))
        if self.logger is not None:
            self.logger.debug_kv(
                "AgenticStageRunner validator",
                unit_id=_unit_id_for(unit),
                tool_call_count=result.tool_call_count,
                fell_back=result.fell_back,
            )
        return _coerce_validator(result.final_answer)

    def run_exploiter(
        self,
        *,
        unit: AuditUnit,
        analyzer: AnalyzerResult,
        validator: ValidationResult,
        path_context: dict[str, object] | None = None,
        persona: Persona | None = None,
    ) -> ExploitationResult:
        system_prompt = _resolve_agentic_prompt(_unit_kind_for(unit), "exploiter")
        if persona is not None:
            system_prompt = f"{system_prompt}\n\n{persona.extra_system_prefix}"
        payload = _resolve_payload(unit, "exploiter", path_context)
        payload["candidate_finding"] = _analyzer_to_payload(analyzer)
        payload["validator_verdict"] = validator.status.value
        payload["validator_analysis"] = validator.analysis
        result = self.transport.invoke(
            system_prompt=system_prompt,
            user_payload=payload,
            response_model=ExploitationOutput,
            timeout_seconds=self.timeout_seconds,
            project=self.project,
        )
        self.agentic_transcripts.append((_unit_id_for(unit), "exploiter", result))
        if self.logger is not None:
            self.logger.debug_kv(
                "AgenticStageRunner exploiter",
                unit_id=_unit_id_for(unit),
                tool_call_count=result.tool_call_count,
                fell_back=result.fell_back,
            )
        return _coerce_exploiter(result.final_answer)


# ---------------------------------------------------------------------------
# Builder + cost estimate
# ---------------------------------------------------------------------------


def build_stage_runner(
    *,
    audit_mode: AuditModeConfig,
    analyzer_agent: AnalyzerAgent,
    validator_agent: ValidatorAgent,
    exploitation_agent: ExploitationAgent,
    logger: RuntimeLogger | None = None,
    sandbox_root: Path | None = None,  # noqa: ARG001 — kept for backward compat
    transport: AgentTransport | None = None,
    coder_cfg: CoderConfig | None = None,
    repo_root: Path | None = None,
) -> tuple[StageRunner, AgentTransport | None]:
    """Build the stage runner the workflow should use.

    Returns `(runner, transport)` — `transport` is `None` for
    the PromptStageRunner path, so the workflow can wire the
    same transport into `build_reconciler` without re-deriving
    it from config.

    Selection rule (`wire-agentic-into-workflow`):
    - `audit.stages.form == "agentic"` → `AgenticStageRunner`
      backed by `CoderServiceAgentTransport` (HTTP POST to
      coder-service's `/agent_invocations` endpoint). The
      coder-service container hosts the single claude install
      for the deployment.
    - everything else → `PromptStageRunner` wrapping the
      provided agents; transport returned as `None`.

    Tests inject a `MockAgentTransport` via the optional
    `transport=` kwarg; production wiring leaves it `None` so
    the builder constructs a `CoderServiceAgentTransport`
    from `audit.agentic.transport.coder_service.*` config.

    Project-name derivation for agentic mode:
    `audit.agentic.transport.coder_service.project` is the operator
    override. When it's empty (the common case), the project is
    derived from `repo_root` relative to `coder.workspace_root`
    using the same logic as the verification coder
    (`preflight.derive_project_from_workspace`). Falls back to
    `basename(repo_root)` for legacy single-repo containers.
    Operators only need to set the explicit project field for
    multi-tenant deployments where the workspace mount layout
    diverges from the audit-host directory layout.

    `sandbox_root` is accepted for backward compatibility but
    no longer used — the sandbox lives inside the
    coder-service container, not on the xauditor side.
    """

    form = getattr(audit_mode, "stages_form", "prompt")
    if form == "agentic":
        agentic_cfg = audit_mode.agentic
        cs_cfg = agentic_cfg.transport.coder_service
        if transport is None:
            transport = CoderServiceAgentTransport(
                endpoint=cs_cfg.endpoint,
                bearer_token_env=cs_cfg.bearer_token_env or None,
                request_timeout_safety_seconds=cs_cfg.request_timeout_safety_seconds,
            )
        # Auto-derive project from coder.workspace_root + repo_root
        # so operators don't have to repeat themselves between the
        # verification coder and agentic transport. Explicit
        # `audit.agentic.transport.coder_service.project` still wins.
        project: str | None
        if cs_cfg.project:
            project = cs_cfg.project
        elif coder_cfg is not None and repo_root is not None:
            from xauditor.audit.preflight import derive_project_from_workspace

            derived = derive_project_from_workspace(
                workspace_root=getattr(coder_cfg, "effective_workspace_root", "")
                or "",
                repo_root=repo_root,
                override="",
            )
            project = derived or None
        else:
            project = None
        runner = AgenticStageRunner(
            transport=transport,
            timeout_seconds=agentic_cfg.timeout_seconds,
            project=project,
            logger=logger,
        )
        return runner, transport
    runner = PromptStageRunner(
        analyzer_agent=analyzer_agent,
        validator_agent=validator_agent,
        exploitation_agent=exploitation_agent,
        logger=logger,
    )
    return runner, None


@dataclass(frozen=True)
class CostEstimate:
    mode: str
    unit_count: int
    estimated_calls: int
    estimated_input_tokens_low: int
    estimated_input_tokens_high: int

    def render(self) -> str:
        return (
            f"[estimate] mode={self.mode}, "
            f"~{self.unit_count} units → ~{self.estimated_calls} LLM calls, "
            f"~{self.estimated_input_tokens_low // 1000}K-"
            f"{self.estimated_input_tokens_high // 1000}K input tokens. "
            "Cost depends on your provider's per-token pricing."
        )


# Per-stage call multipliers used by the cost estimator. Derived
# from Phase 1B's iterative analyzer math:
#   fast: cap × analyzer + Cap × validator + Cap × exploiter
#         = max_findings_per_unit × 3 stages worst case
#   deep: replication.analyzer + replication.validator ×
#         debate.max_rounds + 1 exploiter (per accepted finding)
_FAST_TOKENS_PER_CALL_LOW = 1_500
_FAST_TOKENS_PER_CALL_HIGH = 3_000
_DEEP_TOKENS_PER_CALL_LOW = 8_000
_DEEP_TOKENS_PER_CALL_HIGH = 20_000


def estimate_audit_cost(
    *,
    audit_mode: AuditModeConfig,
    unit_count: int,
) -> CostEstimate:
    """Compute a rough order-of-magnitude cost estimate.

    Conservative — the worst-case math assumes every audit unit
    accepts the maximum number of findings (fast: cap; deep:
    replication.analyzer). Actual runs typically hit ~30-60% of
    the worst case.
    """

    mode = audit_mode.mode
    cap = max(1, audit_mode.max_findings_per_unit)
    rep_a = max(1, audit_mode.replication.analyzer)
    rep_v = max(1, audit_mode.replication.validator)
    debate_rounds = (
        audit_mode.validator.debate.max_rounds
        if audit_mode.validator.debate.enabled
        else 1
    )

    if mode == "fast":
        # Worst case: cap × (analyzer + validator + exploiter)
        calls_per_unit = cap * 3
        tokens_low = calls_per_unit * _FAST_TOKENS_PER_CALL_LOW
        tokens_high = calls_per_unit * _FAST_TOKENS_PER_CALL_HIGH
    else:
        # deep: replication.analyzer + replication.validator ×
        # debate_rounds + 1 exploiter, per accepted finding.
        # Worst case treats every analyzer replica as producing a
        # distinct surviving finding (rep_a accepted candidates).
        calls_per_finding = rep_a + (rep_v * debate_rounds) + 1
        calls_per_unit = rep_a * calls_per_finding
        tokens_low = calls_per_unit * _DEEP_TOKENS_PER_CALL_LOW
        tokens_high = calls_per_unit * _DEEP_TOKENS_PER_CALL_HIGH

    return CostEstimate(
        mode=mode,
        unit_count=unit_count,
        estimated_calls=calls_per_unit * unit_count,
        estimated_input_tokens_low=tokens_low * unit_count,
        estimated_input_tokens_high=tokens_high * unit_count,
    )


def emit_cost_estimate(
    estimate: CostEstimate,
    *,
    logger: RuntimeLogger | None = None,
) -> None:
    """Emit the estimate to stderr (and the run logger when present)
    BEFORE the first LLM call so operators see the magnitude up
    front. Idempotent; safe to call repeatedly."""

    line = estimate.render()
    sys.stderr.write(line + "\n")
    if logger is not None:
        logger.info(line)


# ---------------------------------------------------------------------------
# Adapter — Protocol AuditUnit → legacy `models.AuditUnit`
# ---------------------------------------------------------------------------


def _legacy_unit(unit: AuditUnit):
    """Return the legacy `models.AuditUnit` carried by a
    `PathAuditUnit`, falling back to `unit` itself when the caller
    already passed a legacy dataclass.

    `PromptStageRunner.run_*` methods consume the legacy dataclass
    surface (`unit.path.entry_function`, `unit.function_ids`)
    because the underlying `AnalyzerAgent` / `ValidatorAgent` /
    `ExploitationAgent` haven't been migrated to the Protocol yet.
    Migrating them is a future change once Sink/Entry units start
    actually flowing through the workflow.
    """

    legacy = getattr(unit, "legacy", None)
    if legacy is not None:
        return legacy
    # Caller passed a legacy dataclass directly — workflow.py's
    # current iteration shape. Fall through unchanged.
    return unit


__all__ = [
    "StageRunner",
    "PromptStageRunner",
    "AgenticStageRunner",
    "Persona",
    "DEFAULT_DEEP_PERSONAS",
    "resolve_personas",
    "build_stage_runner",
    "CostEstimate",
    "estimate_audit_cost",
    "emit_cost_estimate",
    "StageForm",
]
