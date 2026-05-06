"""`report` schema tables.

Every field emitted into a Markdown audit artifact has a column here so the
portal and any downstream consumer can reconstruct the artifact purely from
the DB.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from xauditor_portal.db.base import Base, utcnow


SCHEMA = "report"


class AuditRun(Base):
    __tablename__ = "audit_runs"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repo_root: Mapped[str] = mapped_column(String(2048), nullable=False)
    project_name: Mapped[str] = mapped_column(String(512), nullable=False)
    build_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)  # "fast" | "deep"
    # Stage-call form selected at run-open time (`audit.stages.form`).
    # Persisted (alembic 0014) so the portal can show which form a run
    # used without inferring from downstream artifacts. Default 'prompt'
    # mirrors the column default; the CHECK constraint enforces the
    # closed enum at the DB layer.
    stages_form: Mapped[str] = mapped_column(
        String(16), nullable=False, default="prompt"
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # "in_progress" | "completed" | "failed" | "cancelled"
    progress_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_candidates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_findings: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    false_positives: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unlabeled_findings: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Count of findings that human reviewers labeled
    # ``duplicate`` for this run. Populated by the sink's
    # ``_sync_snapshot`` from the feedback annotations table.
    # See ``add-duplicate-feedback-label`` design D6.
    duplicate_findings: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    llm_providers_used: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Stores the on-disk timestamp slug (xauditor 0.4.x) OR a stable run
    # label string (xauditor 0.5.0+; the column kept its name to avoid
    # an unrelated rename migration). Used as the natural key for run
    # lookup from the CLI / portal, so it MUST remain unique-ish per
    # active run on the same project.
    report_dir: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    started_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # In-DB audit-resume payload (Phase 2 of make-postgres-the-canonical-sink).
    # JSONB so the portal can also read it for "this run is resumable"
    # badges. NULL on rows started by xauditor 0.4.x; the resume code
    # path surfaces a clear "started on a previous version, please re-run
    # from scratch" error in that case.
    resume_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # CoverageGaps JSONB written by Phase 5 of
    # ``restructure-audit-modes-and-coverage`` (column added by alembic
    # migration 0011 in Phase 1). Phase 1 writes ``NULL``; pre-rename
    # runs are also ``NULL``. Schema (when populated):
    #   {audited_classes: [...], skipped_by_mode: [...],
    #    out_of_scope: [...], mode: "fast"|"deep",
    #    advice_to_user: "..."}
    # Portal renders an "n/a — pre-rename audit" placeholder when NULL.
    coverage_gaps: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    findings: Mapped[list["Finding"]] = relationship(back_populates="run", cascade="all, delete-orphan")
    progress_events: Mapped[list["ProgressEvent"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (
        Index("ix_findings_run_confidence", "run_id", "confidence_level"),
        Index("ix_findings_run_validation", "run_id", "validation_status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.audit_runs.id", ondelete="CASCADE"), nullable=False
    )
    finding_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    path_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    finding_name: Mapped[str] = mapped_column(String(512), nullable=False)
    finding_description: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_level: Mapped[str] = mapped_column(String(16), nullable=False)  # High | Medium | Low
    analyzer_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    evidence_strength: Mapped[str | None] = mapped_column(String(32), nullable=True)
    analysis: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[str] = mapped_column(Text, nullable=False)
    business_context: Mapped[str] = mapped_column(Text, nullable=False)
    context_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    suspect_function_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    suspect_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    function_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    exploitation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    exploitation_steps: Mapped[str] = mapped_column(Text, nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    validation_analysis: Mapped[str] = mapped_column(Text, nullable=False)
    # Cross-unit reconciliation JSONB written by the deep-mode
    # reconciler stage (Phase 5A's `PassthroughReconciler` writes
    # NULL for Path-only findings; the real `AgenticReconciler`
    # populates `{per_unit_verdicts, consolidated_verdict,
    # consolidation_reasoning}` when SinkAuditUnit / EntryAuditUnit
    # / etc. enumeration ships). Column added by alembic 0012.
    # NULL on every row from runs predating the multi-unit
    # enumeration follow-up.
    reconciliation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Per-finding agentic tool-call transcript written by
    # `AgenticStageRunner` when `audit.stages.form: agentic`.
    # JSON list of `{tool, input, output}` records. NULL on
    # prompt-form findings AND on findings produced before
    # alembic 0013 (`agentic-stage-runner-real` Phase 2).
    # PSIRT replays the transcript during finding triage to
    # see what evidence the agent collected.
    agentic_transcript: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)

    run: Mapped[AuditRun] = relationship(back_populates="findings")
    source_references: Mapped[list["FindingSourceReference"]] = relationship(
        back_populates="finding", cascade="all, delete-orphan"
    )
    referenced_symbols: Mapped[list["ReferencedSymbol"]] = relationship(
        back_populates="finding", cascade="all, delete-orphan"
    )


class FindingSourceReference(Base):
    __tablename__ = "finding_source_references"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    finding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.findings.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    snippet: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    finding: Mapped[Finding] = relationship(back_populates="source_references")


class ReferencedSymbol(Base):
    __tablename__ = "referenced_symbols"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    finding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.findings.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    module_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    start_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    type_annotation: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_repr: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_placeholder: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    used_by: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)

    finding: Mapped[Finding] = relationship(back_populates="referenced_symbols")


class CoderFinding(Base):
    """Repo-global coder verification verdict for a single finding.

    Mirrors the four ``coder_*`` fields on the in-memory ``Finding`` plus
    operator-debug columns (``cli_exit_code``, ``cli_stderr``, timing).
    The natural key is ``(run_id, finding_ref)`` so re-running the same
    audit overwrites the prior row instead of duplicating it.
    """

    __tablename__ = "coder_findings"
    __table_args__ = (
        UniqueConstraint("run_id", "finding_ref", name="uq_coder_findings_run_finding"),
        Index("ix_coder_findings_run_status", "run_id", "status"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.audit_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    finding_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    analysis: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dispatched_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cli_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cli_stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)

    evidence: Mapped[list["CoderFindingEvidence"]] = relationship(
        back_populates="coder_finding",
        cascade="all, delete-orphan",
        order_by="CoderFindingEvidence.ordinal",
    )


class CoderFindingEvidence(Base):
    """One call-chain evidence item attached to a ``CoderFinding`` row."""

    __tablename__ = "coder_finding_evidence"
    __table_args__ = (
        Index("ix_coder_finding_evidence_parent", "coder_finding_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    coder_finding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.coder_findings.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    function_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    snippet: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str] = mapped_column(String(64), nullable=False, default="supporting")
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    coder_finding: Mapped[CoderFinding] = relationship(back_populates="evidence")


class CoverageModule(Base):
    __tablename__ = "coverage_modules"
    __table_args__ = (
        Index("ix_coverage_modules_run", "run_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.audit_runs.id", ondelete="CASCADE"), nullable=False
    )
    module_name: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # audited | unaudited | excluded | skipped | failed
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class CoverageFile(Base):
    __tablename__ = "coverage_files"
    __table_args__ = (
        Index("ix_coverage_files_run", "run_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.audit_runs.id", ondelete="CASCADE"), nullable=False
    )
    file_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # audited | unaudited | excluded | skipped | failed
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class CoverageFunction(Base):
    __tablename__ = "coverage_functions"
    __table_args__ = (
        Index("ix_coverage_functions_run", "run_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.audit_runs.id", ondelete="CASCADE"), nullable=False
    )
    function_id: Mapped[str] = mapped_column(String(512), nullable=False)
    qualified_name: Mapped[str] = mapped_column(String(1024), nullable=False)
    file_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)  # audited | unaudited | excluded | skipped | failed
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class _SubagentRecordMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    path_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    subagent_index: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_name: Mapped[str] = mapped_column(String(256), nullable=False)
    raw_output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)


class AnalyzerSubagentRecord(_SubagentRecordMixin, Base):
    __tablename__ = "analyzer_subagent_records"
    __table_args__ = {"schema": SCHEMA}

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.audit_runs.id", ondelete="CASCADE"), nullable=False
    )


class ValidatorSubagentRecord(_SubagentRecordMixin, Base):
    __tablename__ = "validator_subagent_records"
    __table_args__ = {"schema": SCHEMA}

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.audit_runs.id", ondelete="CASCADE"), nullable=False
    )
    finding_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)


class ExploiterSubagentRecord(_SubagentRecordMixin, Base):
    __tablename__ = "exploiter_subagent_records"
    __table_args__ = {"schema": SCHEMA}

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.audit_runs.id", ondelete="CASCADE"), nullable=False
    )
    finding_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)


class ValidatorDebate(Base):
    __tablename__ = "validator_debates"
    __table_args__ = (
        Index("ix_validator_debates_run", "run_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.audit_runs.id", ondelete="CASCADE"), nullable=False
    )
    path_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    finding_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    rounds: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    final_verdict: Mapped[str] = mapped_column(String(64), nullable=False)
    convergence_state: Mapped[str] = mapped_column(String(64), nullable=False)
    configured_round_cap: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)


class NoFindingPath(Base):
    __tablename__ = "no_finding_paths"
    __table_args__ = (
        Index("ix_no_finding_paths_run", "run_id"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.audit_runs.id", ondelete="CASCADE"), nullable=False
    )
    path_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    entry_function: Mapped[str] = mapped_column(String(512), nullable=False)
    function_chain: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    analyzer_status: Mapped[str] = mapped_column(String(32), nullable=False)
    analyzer_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    skipped_stages: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)


class PathRawOutput(Base):
    __tablename__ = "path_raw_outputs"
    __table_args__ = (
        UniqueConstraint("run_id", "stage", "path_fingerprint", name="uq_path_raw_outputs_stage_path"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.audit_runs.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)  # analyzer|validator|exploiter|audit_log|...
    path_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    raw_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)


class ProgressEvent(Base):
    __tablename__ = "progress_events"
    __table_args__ = (
        Index("ix_progress_events_run_time", "run_id", "timestamp"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{SCHEMA}.audit_runs.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(64), nullable=False)
    current_path_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_paths: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    timestamp: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    heartbeat_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # started | progress | stage_completed | finished | failed

    run: Mapped[AuditRun] = relationship(back_populates="progress_events")


class AuditRunAdminAction(Base):
    """Audit log of admin-initiated run-management actions.

    Captures every cancel / complete / delete invoked through the admin
    run-management endpoints. Intentionally has no FK back to ``audit_runs``:
    a ``delete`` action removes the run, but we still want the audit-log row
    to survive (it is the only remaining record that the action happened).
    The ``actor_user_id`` FK uses ``ON DELETE SET NULL`` so deleting the
    actor does not cascade away their history.
    """

    __tablename__ = "audit_run_admin_actions"
    __table_args__ = (
        Index("ix_audit_run_admin_actions_run", "run_id"),
        Index("ix_audit_run_admin_actions_created", "created_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth.users.id", ondelete="SET NULL"),
        nullable=True,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    # ``cancel`` | ``complete`` | ``delete``
    previous_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)


__all__ = [
    "AnalyzerSubagentRecord",
    "AuditRun",
    "AuditRunAdminAction",
    "CoderFinding",
    "CoderFindingEvidence",
    "CoverageFile",
    "CoverageFunction",
    "CoverageModule",
    "ExploiterSubagentRecord",
    "Finding",
    "FindingSourceReference",
    "NoFindingPath",
    "PathRawOutput",
    "ProgressEvent",
    "ReferencedSymbol",
    "SCHEMA",
    "ValidatorDebate",
    "ValidatorSubagentRecord",
]
