"""`/api/runs` endpoints: audit run listings and detail."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from xauditor_portal.auth import forbid_must_change_password, get_session, require_admin
from xauditor_portal.db.models.auth import User
from xauditor_portal.db.models.feedback import FindingAnnotation
from xauditor_portal.db.models.report import (
    AuditRun,
    AuditRunAdminAction,
    CoderFinding,
    CoverageFile,
    CoverageFunction,
    CoverageModule,
    Finding,
    ProgressEvent,
    ValidatorDebate,
    AnalyzerSubagentRecord,
    ValidatorSubagentRecord,
    ExploiterSubagentRecord,
)


_log = logging.getLogger(__name__)

router = APIRouter(tags=["runs"], dependencies=[Depends(forbid_must_change_password)])


class RunSummary(BaseModel):
    id: str
    repo_root: str
    project_name: str
    build_fingerprint: str
    mode: str
    stages_form: Literal["prompt", "agentic"] = "prompt"
    status: str
    progress_percent: int
    valid_findings: int
    false_positives: int
    unlabeled_findings: int
    duplicate_findings: int = 0
    started_at: datetime
    completed_at: datetime | None


class FeedbackBreakdown(BaseModel):
    base: int
    added_by_feedback: int
    removed_by_feedback: int
    duplicates_in_bucket: int = 0
    net: int


class RunDetail(RunSummary):
    report_dir: str | None
    llm_providers_used: dict[str, Any]
    valid_findings_breakdown: FeedbackBreakdown
    valid_rate: float | None = None
    coverage_gaps: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Per-run CoverageGaps payload. Schema: "
            "`{audited_classes: list[str], skipped_by_mode: list[str], "
            "out_of_scope: list[str], mode: 'fast'|'deep', advice_to_user: str}`. "
            "NULL on runs created before `restructure-audit-modes-and-coverage` "
            "Phase 5A populated the column."
        ),
    )


_VALID_STATUSES = ("Valid", "Partial Valid", "Inconclusive")
_FP_STATUS = "False Positive"
_LABEL_DUPLICATE = "duplicate"
_LABEL_UNLABELED = "unlabeled"
_LABEL_TRUE_POSITIVE = "true_positive"
_LABEL_FALSE_POSITIVE = "false_positive"

# Default coder-status subset applied when the caller does not supply an
# explicit ``coder_status`` query param. Matches the Findings filter bar's
# first-open subset on the portal — hides ``Not Verified`` AND ``Fail`` so
# run-list / run-detail headers reflect the post-filter view operators see.
_DEFAULT_CODER_FILTER: tuple[str, ...] = (
    "Verified",
    "Inconclusive",
    "Pending",
    "Skipped",
)


@dataclass(frozen=True)
class _RunMetrics:
    """Per-run live counts derived from a single SQL pivot.

    ``false_positives`` counts findings whose latest feedback annotation has
    ``label = 'false_positive'`` (independent of agent ``validation_status``).
    ``valid_findings`` retains its agent-base + human-feedback adjustment
    shape; the matching ``valid_breakdown`` exposes the components for the
    run-detail card UI.
    """

    valid_findings: int
    false_positives: int
    duplicate_findings: int
    unlabeled_findings: int
    valid_breakdown: FeedbackBreakdown
    valid_rate: float | None


def _zero_metrics() -> _RunMetrics:
    zero = FeedbackBreakdown(
        base=0,
        added_by_feedback=0,
        removed_by_feedback=0,
        duplicates_in_bucket=0,
        net=0,
    )
    return _RunMetrics(
        valid_findings=0,
        false_positives=0,
        duplicate_findings=0,
        unlabeled_findings=0,
        valid_breakdown=zero,
        valid_rate=None,
    )


def _resolve_coder_filter(
    coder_status_filter: Sequence[str] | None,
) -> tuple[str, ...] | None:
    """Translate the three-state caller input into a SQL-ready tuple.

    ``None`` → apply the server default ``_DEFAULT_CODER_FILTER``.
    Empty sequence → ``None`` (no restriction; the operator unchecked everything).
    Non-empty sequence → the values verbatim.
    """

    if coder_status_filter is None:
        return _DEFAULT_CODER_FILTER
    values = tuple(coder_status_filter)
    if not values:
        return None
    return values


def _build_metrics_select(
    *group_columns,
    coder_status_filter: Sequence[str] | None = None,
):
    """Shared SELECT pivot used by both the per-run and batched helpers.

    ``coder_status_filter`` follows the three-state convention documented on
    ``_resolve_coder_filter``. When the resolved filter is non-None, the
    pivot adds a ``WHERE COALESCE(coder_finding.status, 'Skipped') IN (...)``
    clause so every COUNT FILTER below sees only the in-scope finding set.
    """

    is_valid = Finding.validation_status.in_(_VALID_STATUSES)
    label = FindingAnnotation.label
    no_annotation = FindingAnnotation.id.is_(None)
    is_duplicate = label == _LABEL_DUPLICATE
    is_unlabeled = label == _LABEL_UNLABELED
    is_human_tp = label == _LABEL_TRUE_POSITIVE
    is_human_fp = label == _LABEL_FALSE_POSITIVE

    stmt = (
        select(
            *group_columns,
            func.count().label("total_count"),
            func.count().filter(is_valid).label("base_valid"),
            func.count().filter(is_human_fp).label("false_positives_count"),
            func.count()
            .filter(Finding.validation_status == _FP_STATUS, is_human_tp)
            .label("valid_added"),
            func.count().filter(is_valid & is_human_fp).label("valid_removed"),
            func.count()
            .filter(is_valid & is_duplicate)
            .label("duplicates_among_valid"),
            func.count().filter(is_duplicate).label("duplicates_total"),
            func.count()
            .filter(no_annotation | is_unlabeled)
            .label("unlabeled_count"),
        )
        .select_from(Finding)
        .outerjoin(
            FindingAnnotation, FindingAnnotation.finding_id == Finding.id
        )
        .outerjoin(
            CoderFinding,
            (CoderFinding.run_id == Finding.run_id)
            & (CoderFinding.finding_ref == Finding.finding_id),
        )
    )
    resolved = _resolve_coder_filter(coder_status_filter)
    if resolved is not None:
        stmt = stmt.where(
            func.coalesce(CoderFinding.status, "Skipped").in_(resolved)
        )
    return stmt


def _row_to_metrics(row) -> _RunMetrics:
    base_valid = int(row.base_valid or 0)
    valid_added = int(row.valid_added or 0)
    valid_removed = int(row.valid_removed or 0)
    duplicates_among_valid = int(row.duplicates_among_valid or 0)
    duplicates_total = int(row.duplicates_total or 0)
    unlabeled_count = int(row.unlabeled_count or 0)
    false_positives = int(row.false_positives_count or 0)

    valid_net = base_valid + valid_added - valid_removed - duplicates_among_valid

    valid_breakdown = FeedbackBreakdown(
        base=base_valid,
        added_by_feedback=valid_added,
        removed_by_feedback=valid_removed,
        duplicates_in_bucket=duplicates_among_valid,
        net=valid_net,
    )

    # `portal-drop-candidates-and-rebase-positive-rate`: re-base the
    # rate to mean "agent precision after reviewer feedback".
    # Denominator is `base_valid` (count of findings the agent put
    # into its valid-side pool: validation_status in
    # _VALID_STATUSES) rather than the previous
    # `total_count - duplicates_total`. The new value answers: of
    # the findings the agent claimed as valid, how many held up
    # after feedback. Range allows >1.0 when the reviewer promotes
    # more FP-bucket findings to true_positive than they downgrade
    # Valid to false_positive.
    valid_rate = valid_net / base_valid if base_valid > 0 else None

    return _RunMetrics(
        valid_findings=valid_net,
        false_positives=false_positives,
        duplicate_findings=duplicates_total,
        unlabeled_findings=unlabeled_count,
        valid_breakdown=valid_breakdown,
        valid_rate=valid_rate,
    )


async def _run_metrics(
    session: AsyncSession,
    run_id: str,
    coder_status_filter: Sequence[str] | None = None,
) -> _RunMetrics:
    """Compute every live count for one run in a single SQL round-trip.

    LEFT-JOINs ``feedback.finding_annotations`` AND ``report.coder_findings``
    against ``report.findings`` and pivots the valid-side base + adjustment
    counts, the feedback-derived ``false_positives_count`` (annotations whose
    label is ``false_positive``), and the duplicate / unlabeled / total
    counts. ``coder_status_filter`` is forwarded to ``_build_metrics_select``;
    see ``_resolve_coder_filter`` for the three-state semantics. The
    ``feedback.finding_annotations`` UNIQUE on (run_id, finding_id) and the
    ``coder_findings`` UNIQUE on (run_id, finding_ref) both keep the joins
    row-equivalent to per-finding lookups, so no de-duplication is needed in
    the COUNT FILTERs. Cost scales with findings-per-run.
    """

    row = (
        await session.execute(
            _build_metrics_select(coder_status_filter=coder_status_filter).where(
                Finding.run_id == run_id
            )
        )
    ).one()
    return _row_to_metrics(row)


async def _batch_run_metrics(
    session: AsyncSession,
    run_ids: Sequence[str],
    coder_status_filter: Sequence[str] | None = None,
) -> dict[str, _RunMetrics]:
    """Same SQL as ``_run_metrics`` but pivoted per ``run_id`` via GROUP BY.

    Used by the list endpoints so the page of N runs costs one round-trip
    instead of N. Runs whose finding set is empty (no rows in
    ``report.findings``) won't appear in the result; callers fill the gap
    with ``_zero_metrics()``. ``coder_status_filter`` is forwarded to
    ``_build_metrics_select``; the list endpoints pass ``None`` so the
    server-side default ``_DEFAULT_CODER_FILTER`` applies.
    """

    if not run_ids:
        return {}

    rows = (
        await session.execute(
            _build_metrics_select(
                Finding.run_id.label("run_id"),
                coder_status_filter=coder_status_filter,
            )
            .where(Finding.run_id.in_(run_ids))
            .group_by(Finding.run_id)
        )
    ).all()
    return {str(row.run_id): _row_to_metrics(row) for row in rows}


class ProgressEventOut(BaseModel):
    stage: str
    heartbeat_kind: str
    current_path_index: int | None
    total_paths: int | None
    message: str
    timestamp: datetime


class RunsPage(BaseModel):
    items: list[RunSummary]
    total: int


def _to_summary(row: AuditRun, metrics: _RunMetrics) -> RunSummary:
    return RunSummary(
        id=str(row.id),
        repo_root=row.repo_root,
        project_name=row.project_name,
        build_fingerprint=row.build_fingerprint,
        mode=row.mode,
        stages_form=row.stages_form,
        status=row.status,
        progress_percent=row.progress_percent,
        valid_findings=metrics.valid_findings,
        false_positives=metrics.false_positives,
        unlabeled_findings=metrics.unlabeled_findings,
        duplicate_findings=metrics.duplicate_findings,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


@router.get("", response_model=RunsPage)
async def list_runs(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status"),
    mode_filter: str | None = Query(default=None, alias="mode"),
    project: str | None = Query(default=None),
) -> RunsPage:
    query = select(AuditRun)
    count_query = select(func.count(AuditRun.id))
    if status_filter:
        query = query.where(AuditRun.status == status_filter)
        count_query = count_query.where(AuditRun.status == status_filter)
    if mode_filter:
        query = query.where(AuditRun.mode == mode_filter)
        count_query = count_query.where(AuditRun.mode == mode_filter)
    if project:
        query = query.where(AuditRun.project_name.ilike(f"%{project}%"))
        count_query = count_query.where(AuditRun.project_name.ilike(f"%{project}%"))
    query = query.order_by(AuditRun.started_at.desc()).offset(offset).limit(limit)
    rows = (await session.execute(query)).scalars().all()
    total = (await session.execute(count_query)).scalar_one()
    metrics_map = await _batch_run_metrics(session, [str(r.id) for r in rows])
    items = [
        _to_summary(r, metrics_map.get(str(r.id), _zero_metrics())) for r in rows
    ]
    return RunsPage(items=items, total=int(total))


async def _load_run(session: AsyncSession, run_id: str) -> AuditRun:
    row = await session.get(AuditRun, run_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Audit run not found."
        )
    return row


def _parse_coder_status_query(
    coder_status: list[str] | None,
) -> Sequence[str] | None:
    """Translate the FastAPI query value into ``_run_metrics`` semantics.

    The portal serializes filter state as repeatable query params; an
    operator who unchecks every chip sends the present-but-empty marker
    ``?coder_status=``, which FastAPI surfaces as ``[""]``. Distinguish:

    - Missing entirely (``None``) → return ``None`` (helper applies default)
    - Present with at least one non-empty value → return the filtered list
    - Present with only empty strings → return ``()`` (no restriction)
    """

    if coder_status is None:
        return None
    cleaned = [v for v in coder_status if v != ""]
    if cleaned:
        return cleaned
    return ()


@router.get("/{run_id}", response_model=RunDetail)
async def get_run(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    coder_status: list[str] | None = Query(default=None),
) -> RunDetail:
    row = await _load_run(session, run_id)
    metrics = await _run_metrics(
        session,
        run_id,
        coder_status_filter=_parse_coder_status_query(coder_status),
    )
    return await _build_run_detail(session, row, metrics)


async def _build_run_detail(
    session: AsyncSession, row: AuditRun, metrics: _RunMetrics
) -> RunDetail:
    summary = _to_summary(row, metrics).model_dump()
    return RunDetail(
        **summary,
        report_dir=row.report_dir,
        llm_providers_used=row.llm_providers_used,
        valid_findings_breakdown=metrics.valid_breakdown,
        valid_rate=metrics.valid_rate,
        coverage_gaps=row.coverage_gaps,
    )


# ---- Admin run management ----------------------------------------------
#
# These three endpoints are admin-override surfaces. They operate directly
# on the persisted row and do NOT cooperate with the audit orchestrator —
# admins reach for them precisely when the worker is stuck or crashed.
# Cancel / complete are allowed from any starting status (including terminal
# ones) so admins can correct a status set incorrectly. Idempotent calls
# preserve ``completed_at`` so terminal timestamps stay accurate.


class AdminRunActionRequest(BaseModel):
    """Optional body for cancel/complete admin actions."""

    reason: str | None = Field(default=None, max_length=2048)


def _record_admin_action(
    session: AsyncSession,
    *,
    actor_user_id: Any,
    run_id: str,
    action: str,
    previous_status: str | None,
    new_status: str,
    reason: str | None,
) -> None:
    """Best-effort audit-log row insert. Never fails the API call.

    The caller already mutated (or deleted) the run row and the response
    is on its way back to the admin. A missing audit-log row is a separate
    operational concern that operators can investigate via the standard
    structured logger; we do NOT want to surface a 500 to the admin who
    just successfully cancelled a stuck run.
    """

    try:
        session.add(
            AuditRunAdminAction(
                actor_user_id=actor_user_id,
                run_id=run_id,
                action=action,
                previous_status=previous_status,
                new_status=new_status,
                reason=reason,
            )
        )
    except SQLAlchemyError:  # pragma: no cover — extremely defensive
        _log.exception(
            "audit-log insert failed (run=%s actor=%s action=%s)",
            run_id,
            actor_user_id,
            action,
        )


@router.post("/{run_id}/cancel", response_model=RunDetail)
async def cancel_run(
    run_id: str,
    payload: AdminRunActionRequest | None = None,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(require_admin),
) -> RunDetail:
    """Manually mark an audit run as cancelled.

    Admin-only. Allowed from any starting status. Idempotent: already
    cancelled runs return the current row unchanged so terminal timestamps
    are preserved.
    """

    row = await _load_run(session, run_id)
    previous_status = row.status
    reason = payload.reason if payload is not None else None

    if row.status != "cancelled":
        row.status = "cancelled"
        if row.completed_at is None:
            row.completed_at = datetime.now(timezone.utc)
        _record_admin_action(
            session,
            actor_user_id=actor.id,
            run_id=run_id,
            action="cancel",
            previous_status=previous_status,
            new_status="cancelled",
            reason=reason,
        )
        try:
            await session.commit()
        except SQLAlchemyError:
            await session.rollback()
            _log.exception("cancel_run commit failed (run=%s)", run_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to cancel the run.",
            )
        await session.refresh(row)

    metrics = await _run_metrics(session, run_id)
    return await _build_run_detail(session, row, metrics)


@router.post("/{run_id}/complete", response_model=RunDetail)
async def complete_run(
    run_id: str,
    payload: AdminRunActionRequest | None = None,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(require_admin),
) -> RunDetail:
    """Manually mark an audit run as completed.

    Admin-only. Allowed from any starting status. Idempotent: already
    completed runs return the current row unchanged.
    """

    row = await _load_run(session, run_id)
    previous_status = row.status
    reason = payload.reason if payload is not None else None

    if row.status != "completed":
        row.status = "completed"
        if row.completed_at is None:
            row.completed_at = datetime.now(timezone.utc)
        _record_admin_action(
            session,
            actor_user_id=actor.id,
            run_id=run_id,
            action="complete",
            previous_status=previous_status,
            new_status="completed",
            reason=reason,
        )
        try:
            await session.commit()
        except SQLAlchemyError:
            await session.rollback()
            _log.exception("complete_run commit failed (run=%s)", run_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to complete the run.",
            )
        await session.refresh(row)

    metrics = await _run_metrics(session, run_id)
    return await _build_run_detail(session, row, metrics)


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    actor: User = Depends(require_admin),
) -> Response:
    """Delete an audit run and every dependent row via FK cascade.

    Admin-only. The audit-log row is inserted BEFORE the delete so we
    capture the run id while it still exists; the audit-log table has no
    FK back to ``audit_runs`` for exactly this reason.
    """

    row = await _load_run(session, run_id)
    previous_status = row.status

    _record_admin_action(
        session,
        actor_user_id=actor.id,
        run_id=run_id,
        action="delete",
        previous_status=previous_status,
        new_status="deleted",
        reason=None,
    )

    # Pre-delete finding_annotations for this run to defuse a CASCADE-vs-
    # SET-NULL race that would otherwise fire on the audit_runs delete:
    #
    #   finding_annotations has TWO FKs to report.findings:
    #     - finding_id              ON DELETE CASCADE
    #     - duplicate_of_finding_id ON DELETE SET NULL
    #
    #   plus a CHECK biconditional ``ck_finding_annotations_duplicate_pointer``:
    #     (label = 'duplicate' AND duplicate_of_finding_id IS NOT NULL)
    #     OR (label <> 'duplicate' AND duplicate_of_finding_id IS NULL)
    #
    # When the audit_runs row goes away, every finding it owns goes too.
    # If a single annotation row matches BOTH FKs (its finding_id and its
    # duplicate_of_finding_id are both pointing at findings that are being
    # deleted in the same statement), Postgres processes the SET NULL
    # trigger as part of the cascade and the row briefly has
    # label='duplicate' AND duplicate_of_finding_id=NULL — which violates
    # the CHECK and aborts the transaction with HTTP 500.
    #
    # The CHECK + SET NULL combo is fundamentally inconsistent for any
    # "delete a finding" path; the surgical fix is to delete the
    # annotations explicitly first so the SET NULL trigger has nothing
    # to update. The annotations.run_id FK already declares
    # ON DELETE CASCADE so this is just doing-it-eagerly inside the
    # same transaction. The annotation_history rows die via the
    # CASCADE FK on annotation_id.
    #
    # A future change should revisit the schema (either migrate
    # duplicate_of_finding_id to ON DELETE CASCADE, or add a trigger
    # that re-labels the annotation to 'unlabeled' when its pointer
    # gets nulled) to remove the fragility at the schema level.
    await session.execute(
        delete(FindingAnnotation).where(FindingAnnotation.run_id == row.id)
    )

    await session.delete(row)
    try:
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        _log.exception("delete_run commit failed (run=%s)", run_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete the run.",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{run_id}/progress", response_model=list[ProgressEventOut])
async def run_progress(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=200, ge=1, le=2000),
) -> list[ProgressEventOut]:
    await _load_run(session, run_id)
    rows = (
        await session.execute(
            select(ProgressEvent)
            .where(ProgressEvent.run_id == run_id)
            .order_by(ProgressEvent.timestamp.asc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        ProgressEventOut(
            stage=r.stage,
            heartbeat_kind=r.heartbeat_kind,
            current_path_index=r.current_path_index,
            total_paths=r.total_paths,
            message=r.message,
            timestamp=r.timestamp,
        )
        for r in rows
    ]


_LEGACY_COVERAGE_CAP = 500
_COVERAGE_STATUSES = ("audited", "unaudited", "excluded", "skipped", "failed")


async def _coverage_category_summary(
    session: AsyncSession, model, run_id: str
) -> dict[str, Any]:
    """Per-category summary: total + by_status + percent_audited.

    Uses a single `COUNT … GROUP BY status` query so the cost is bounded
    by the number of distinct status values (currently four) regardless
    of how many rows the run produced.
    """

    rows = (
        await session.execute(
            select(model.status, func.count())
            .where(model.run_id == run_id)
            .group_by(model.status)
        )
    ).all()
    by_status: dict[str, int] = {s: 0 for s in _COVERAGE_STATUSES}
    total = 0
    for status_value, count in rows:
        key = status_value if status_value in by_status else status_value
        by_status[key] = int(count)
        total += int(count)
    audited = by_status.get("audited", 0)
    percent_audited = (audited / total * 100.0) if total else 100.0
    return {
        "total": total,
        "by_status": by_status,
        "percent_audited": round(percent_audited, 2),
    }


@router.get("/{run_id}/coverage/summary")
async def run_coverage_summary(
    run_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    await _load_run(session, run_id)
    return {
        "modules": await _coverage_category_summary(session, CoverageModule, run_id),
        "files": await _coverage_category_summary(session, CoverageFile, run_id),
        "functions": await _coverage_category_summary(session, CoverageFunction, run_id),
    }


def _coverage_module_to_dict(row: CoverageModule) -> dict[str, Any]:
    return {"module_name": row.module_name, "status": row.status, "reason": row.reason}


def _coverage_file_to_dict(row: CoverageFile) -> dict[str, Any]:
    return {"file_path": row.file_path, "status": row.status, "reason": row.reason}


def _coverage_function_to_dict(row: CoverageFunction) -> dict[str, Any]:
    return {
        "function_id": row.function_id,
        "qualified_name": row.qualified_name,
        "file_path": row.file_path,
        "status": row.status,
        "reason": row.reason,
    }


@router.get("/{run_id}/coverage/modules")
async def run_coverage_modules(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None),
) -> dict[str, Any]:
    await _load_run(session, run_id)
    base = select(CoverageModule).where(CoverageModule.run_id == run_id)
    if status_filter:
        base = base.where(CoverageModule.status == status_filter)
    if q:
        base = base.where(CoverageModule.module_name.ilike(f"%{q}%"))
    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(
            base.order_by(CoverageModule.module_name.asc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return {
        "items": [_coverage_module_to_dict(m) for m in rows],
        "total": int(total),
    }


@router.get("/{run_id}/coverage/files")
async def run_coverage_files(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None),
) -> dict[str, Any]:
    await _load_run(session, run_id)
    base = select(CoverageFile).where(CoverageFile.run_id == run_id)
    if status_filter:
        base = base.where(CoverageFile.status == status_filter)
    if q:
        base = base.where(CoverageFile.file_path.ilike(f"%{q}%"))
    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(
            base.order_by(CoverageFile.file_path.asc()).offset(offset).limit(limit)
        )
    ).scalars().all()
    return {
        "items": [_coverage_file_to_dict(f) for f in rows],
        "total": int(total),
    }


@router.get("/{run_id}/coverage/functions")
async def run_coverage_functions(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status_filter: str | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None),
) -> dict[str, Any]:
    await _load_run(session, run_id)
    base = select(CoverageFunction).where(CoverageFunction.run_id == run_id)
    if status_filter:
        base = base.where(CoverageFunction.status == status_filter)
    if q:
        base = base.where(CoverageFunction.qualified_name.ilike(f"%{q}%"))
    total = (
        await session.execute(select(func.count()).select_from(base.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(
            base.order_by(CoverageFunction.qualified_name.asc())
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return {
        "items": [_coverage_function_to_dict(fn) for fn in rows],
        "total": int(total),
    }


@router.get("/{run_id}/coverage", deprecated=True)
async def run_coverage(
    run_id: str,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, list[dict[str, Any]]]:
    """Deprecated: use /coverage/summary + /coverage/{modules,files,functions}.

    Keeps the original contract for one release but caps each category at
    500 rows so a very large run cannot OOM the server. New clients SHALL
    use the paginated endpoints.
    """

    await _load_run(session, run_id)
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = (
        f'</api/runs/{run_id}/coverage/summary>; rel="successor-version"'
    )
    modules = (
        await session.execute(
            select(CoverageModule)
            .where(CoverageModule.run_id == run_id)
            .order_by(CoverageModule.module_name.asc())
            .limit(_LEGACY_COVERAGE_CAP)
        )
    ).scalars().all()
    files = (
        await session.execute(
            select(CoverageFile)
            .where(CoverageFile.run_id == run_id)
            .order_by(CoverageFile.file_path.asc())
            .limit(_LEGACY_COVERAGE_CAP)
        )
    ).scalars().all()
    functions = (
        await session.execute(
            select(CoverageFunction)
            .where(CoverageFunction.run_id == run_id)
            .order_by(CoverageFunction.qualified_name.asc())
            .limit(_LEGACY_COVERAGE_CAP)
        )
    ).scalars().all()
    return {
        "modules": [_coverage_module_to_dict(m) for m in modules],
        "files": [_coverage_file_to_dict(f) for f in files],
        "functions": [_coverage_function_to_dict(fn) for fn in functions],
    }


@router.get("/{run_id}/debates")
async def run_debates(
    run_id: str, session: AsyncSession = Depends(get_session)
) -> list[dict[str, Any]]:
    await _load_run(session, run_id)
    debates = (
        await session.execute(
            select(ValidatorDebate).where(ValidatorDebate.run_id == run_id)
        )
    ).scalars().all()
    return [
        {
            "id": str(d.id),
            "path_fingerprint": d.path_fingerprint,
            "finding_ref": d.finding_ref,
            "rounds": d.rounds,
            "final_verdict": d.final_verdict,
            "convergence_state": d.convergence_state,
            "configured_round_cap": d.configured_round_cap,
        }
        for d in debates
    ]


@router.get("/{run_id}/findings/{finding_id}/debate")
async def run_finding_debate(
    run_id: str,
    finding_id: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return the validator debate for a single finding within a run.

    Returns 404 when the run or finding does not exist, or when the finding
    has no associated debate (fast-mode runs never have debates;
    deep-mode runs only emit a debate when validator replicas
    disagreed).
    """

    from xauditor_portal.db.models.report import Finding  # local import to avoid cycle

    await _load_run(session, run_id)
    finding = await session.get(Finding, finding_id)
    if finding is None or str(finding.run_id) != str(run_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Finding not found."
        )
    debate = (
        await session.execute(
            select(ValidatorDebate)
            .where(ValidatorDebate.run_id == run_id)
            .where(ValidatorDebate.finding_ref == finding.finding_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if debate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No validator debate recorded for this finding.",
        )
    return {
        "id": str(debate.id),
        "path_fingerprint": debate.path_fingerprint,
        "finding_ref": debate.finding_ref,
        "rounds": debate.rounds,
        "final_verdict": debate.final_verdict,
        "convergence_state": debate.convergence_state,
        "configured_round_cap": debate.configured_round_cap,
    }


@router.get("/{run_id}/subagents")
async def run_subagents(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    stage: str | None = Query(default=None, pattern="^(analyzer|validator|exploiter)$"),
) -> dict[str, list[dict[str, Any]]]:
    await _load_run(session, run_id)
    result: dict[str, list[dict[str, Any]]] = {}
    stage_filters = {
        "analyzer": AnalyzerSubagentRecord,
        "validator": ValidatorSubagentRecord,
        "exploiter": ExploiterSubagentRecord,
    }
    stages = [stage] if stage else list(stage_filters)
    for s in stages:
        model = stage_filters[s]
        rows = (
            await session.execute(select(model).where(model.run_id == run_id))
        ).scalars().all()
        result[s] = [
            {
                "id": str(r.id),
                "path_fingerprint": r.path_fingerprint,
                "subagent_index": r.subagent_index,
                "provider_name": r.provider_name,
                "raw_output": r.raw_output,
                "finding_ref": getattr(r, "finding_ref", None),
            }
            for r in rows
        ]
    return result
