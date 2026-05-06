"""`/api/findings` and `/api/runs/{id}/findings` endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from xauditor_portal.auth import (
    forbid_must_change_password,
    get_session,
    require_writer,
)
from xauditor_portal.db.models.auth import User
from xauditor_portal.db.models.feedback import (
    LABELS,
    LABEL_DUPLICATE,
    AnnotationHistory,
    FindingAnnotation,
)
from xauditor_portal.db.models.report import (
    AuditRun,
    CoderFinding,
    CoderFindingEvidence,
    Finding,
    FindingSourceReference,
    ReferencedSymbol,
    ValidatorDebate,
)


router = APIRouter(tags=["findings"], dependencies=[Depends(forbid_must_change_password)])


class FindingSummary(BaseModel):
    id: str
    run_id: str
    finding_id: str
    finding_name: str
    confidence_level: str
    validation_status: str
    exploitation_status: str
    file_path: str | None
    function_name: str | None
    suspect_line: int | None
    feedback_label: str | None
    has_debate: bool = False
    coder_status: str = "Skipped"
    created_at: datetime


class FindingDetail(FindingSummary):
    finding_description: str
    analyzer_status: str | None
    evidence_strength: str | None
    analysis: str
    reason: str
    context: str
    business_context: str
    context_notes: str | None
    exploitation_steps: str
    validation_analysis: str
    source_references: list[dict[str, Any]]
    referenced_symbols: list[dict[str, Any]]
    coder_analysis: str = ""
    coder_reason: str = ""
    coder_call_chain_evidence: list[dict[str, Any]] = []
    # Populated when the human-feedback annotation has
    # ``label = "duplicate"``. ``duplicate_of_finding_id`` is
    # the canonical target's ``Finding.id`` (UUID string).
    # ``duplicate_of`` is a small summary object the FE uses
    # to render the "Duplicate of F-XXXX" chip without an
    # extra round-trip.
    duplicate_of_finding_id: str | None = None
    duplicate_of: "DuplicateOfSummary | None" = None
    reconciliation: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Per-finding reconciliation payload. Schema: "
            "`{per_unit_verdicts: list[{unit_kind, unit_id, verdict, analysis}], "
            "consolidated_verdict: str, consolidation_reasoning: str, "
            "transcript?: list[{tool, input, output}]}`. NULL on path-only / "
            "passthrough-reconciled findings; populated when the agentic "
            "reconciler runs on multi-unit findings."
        ),
    )
    agentic_transcript: list[dict[str, Any]] | None = Field(
        default=None,
        description=(
            "Per-finding agentic tool-call transcript written by the "
            "AgenticStageRunner when `audit.stages.form: agentic`. "
            "JSON list of `{tool, input, output}` records. NULL on "
            "prompt-form findings."
        ),
    )


class FeedbackIn(BaseModel):
    label: str
    researcher_note: str | None = None
    # Required when ``label == "duplicate"``; MUST be the
    # ``Finding.id`` (UUID) of the canonical target finding.
    # MUST be ``None`` for any non-duplicate label. Same-run
    # rule, self-ref guard, and single-level guard are
    # enforced at the API layer (see ``_validate_duplicate_payload``).
    duplicate_of_finding_id: str | None = None


class DuplicateOfSummary(BaseModel):
    id: str
    finding_id: str
    name: str


class FeedbackOut(BaseModel):
    label: str
    researcher_note: str | None
    reviewer_username: str
    updated_at: datetime
    created_at: datetime
    duplicate_of_finding_id: str | None = None
    duplicate_of: DuplicateOfSummary | None = None


class IncomingDuplicate(BaseModel):
    id: str
    finding_id: str
    name: str
    run_id: str
    annotation_id: str


class IncomingDuplicatesPage(BaseModel):
    items: list[IncomingDuplicate]
    total: int


def _summary(
    row: Finding,
    feedback_label: str | None,
    *,
    has_debate: bool = False,
    coder_status: str = "Skipped",
) -> FindingSummary:
    return FindingSummary(
        id=str(row.id),
        run_id=str(row.run_id),
        finding_id=row.finding_id,
        finding_name=row.finding_name,
        confidence_level=row.confidence_level,
        validation_status=row.validation_status,
        exploitation_status=row.exploitation_status,
        file_path=row.file_path,
        function_name=row.function_name,
        suspect_line=row.suspect_line,
        feedback_label=feedback_label,
        has_debate=has_debate,
        coder_status=coder_status,
        created_at=row.created_at,
    )


@router.get(
    "/runs/{run_id}/findings",
    response_model=list[FindingSummary],
)
async def list_run_findings(
    run_id: str,
    session: AsyncSession = Depends(get_session),
    file: str | None = Query(default=None),
    function: str | None = Query(default=None),
    confidence: list[str] | None = Query(default=None),
    validation_status: list[str] | None = Query(default=None),
    exploitation_status: list[str] | None = Query(default=None),
    feedback_label: list[str] | None = Query(default=None),
    q: str | None = Query(default=None, description="Free-text substring search"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> list[FindingSummary]:
    run = await session.get(AuditRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audit run not found.")
    query = select(Finding).where(Finding.run_id == run_id)
    if file:
        query = query.where(Finding.file_path.ilike(f"%{file}%"))
    if function:
        query = query.where(Finding.function_name.ilike(f"%{function}%"))
    if confidence:
        query = query.where(Finding.confidence_level.in_(confidence))
    if validation_status:
        query = query.where(Finding.validation_status.in_(validation_status))
    if exploitation_status:
        query = query.where(Finding.exploitation_status.in_(exploitation_status))
    if q:
        like = f"%{q}%"
        query = query.where(
            or_(
                Finding.finding_name.ilike(like),
                Finding.finding_description.ilike(like),
                Finding.analysis.ilike(like),
                Finding.reason.ilike(like),
            )
        )
    query = query.order_by(Finding.created_at.asc()).offset(offset).limit(limit)
    findings = (await session.execute(query)).scalars().all()
    # Fetch annotations for all findings in one go.
    annotations = {
        a.finding_id: a.label
        for a in (
            await session.execute(
                select(FindingAnnotation).where(
                    FindingAnnotation.finding_id.in_([f.id for f in findings])
                )
            )
        )
        .scalars()
        .all()
    }
    # Fetch debate finding_refs for this run once so cards can render the
    # debate affordance without an extra round-trip per finding.
    debate_refs: set[str] = set()
    if findings:
        debate_refs = {
            ref
            for (ref,) in (
                await session.execute(
                    select(ValidatorDebate.finding_ref).where(
                        ValidatorDebate.run_id == run_id
                    )
                )
            ).all()
        }
    # Fetch coder verdicts for this run in one go so the collapsed-card
    # chip renders without a per-finding round-trip.
    coder_status_by_finding_ref: dict[str, str] = {}
    if findings:
        coder_status_by_finding_ref = {
            finding_ref: status
            for finding_ref, status in (
                await session.execute(
                    select(CoderFinding.finding_ref, CoderFinding.status).where(
                        CoderFinding.run_id == run_id
                    )
                )
            ).all()
        }
    items = [
        _summary(
            f,
            annotations.get(f.id),
            has_debate=f.finding_id in debate_refs,
            coder_status=coder_status_by_finding_ref.get(f.finding_id, "Skipped"),
        )
        for f in findings
    ]
    if feedback_label:
        wanted = set(feedback_label)
        items = [i for i in items if (i.feedback_label or "unlabeled") in wanted]
    return items


async def _load_finding(session: AsyncSession, finding_id: str) -> Finding:
    row = await session.get(Finding, finding_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Finding not found."
        )
    return row


@router.get("/findings/{finding_id}", response_model=FindingDetail)
async def get_finding(
    finding_id: str, session: AsyncSession = Depends(get_session)
) -> FindingDetail:
    finding = await _load_finding(session, finding_id)
    annotation = await session.scalar(
        select(FindingAnnotation).where(FindingAnnotation.finding_id == finding.id)
    )
    refs = (
        await session.execute(
            select(FindingSourceReference).where(
                FindingSourceReference.finding_id == finding.id
            )
        )
    ).scalars().all()
    symbols = (
        await session.execute(
            select(ReferencedSymbol).where(ReferencedSymbol.finding_id == finding.id)
        )
    ).scalars().all()
    coder_row = await session.scalar(
        select(CoderFinding).where(
            CoderFinding.run_id == finding.run_id,
            CoderFinding.finding_ref == finding.finding_id,
        )
    )
    coder_evidence_rows = []
    if coder_row is not None:
        coder_evidence_rows = (
            await session.execute(
                select(CoderFindingEvidence)
                .where(CoderFindingEvidence.coder_finding_id == coder_row.id)
                .order_by(CoderFindingEvidence.ordinal)
            )
        ).scalars().all()
    duplicate_pointer = (
        annotation.duplicate_of_finding_id if annotation else None
    )
    return FindingDetail(
        id=str(finding.id),
        run_id=str(finding.run_id),
        finding_id=finding.finding_id,
        finding_name=finding.finding_name,
        confidence_level=finding.confidence_level,
        validation_status=finding.validation_status,
        exploitation_status=finding.exploitation_status,
        file_path=finding.file_path,
        function_name=finding.function_name,
        suspect_line=finding.suspect_line,
        feedback_label=annotation.label if annotation else None,
        coder_status=coder_row.status if coder_row else "Skipped",
        created_at=finding.created_at,
        duplicate_of_finding_id=(
            str(duplicate_pointer) if duplicate_pointer is not None else None
        ),
        duplicate_of=await _duplicate_of_summary(session, duplicate_pointer),
        finding_description=finding.finding_description,
        analyzer_status=finding.analyzer_status,
        evidence_strength=finding.evidence_strength,
        analysis=finding.analysis,
        reason=finding.reason,
        context=finding.context,
        business_context=finding.business_context,
        context_notes=finding.context_notes,
        exploitation_steps=finding.exploitation_steps,
        validation_analysis=finding.validation_analysis,
        source_references=[
            {
                "file_path": r.file_path,
                "snippet": r.snippet,
                "language": r.language,
                "ordinal": r.ordinal,
            }
            for r in refs
        ],
        referenced_symbols=[
            {
                "name": s.name,
                "kind": s.kind,
                "module_name": s.module_name,
                "file_path": s.file_path,
                "start_line": s.start_line,
                "end_line": s.end_line,
                "type_annotation": s.type_annotation,
                "value_repr": s.value_repr,
                "is_placeholder": s.is_placeholder,
                "used_by": s.used_by,
            }
            for s in symbols
        ],
        coder_analysis=coder_row.analysis if coder_row else "",
        coder_reason=coder_row.reason if coder_row else "",
        coder_call_chain_evidence=[
            {
                "file_path": e.file_path,
                "function_name": e.function_name,
                "snippet": e.snippet,
                "language": e.language,
                "role": e.role,
                "ordinal": e.ordinal,
            }
            for e in coder_evidence_rows
        ],
        reconciliation=finding.reconciliation,
        agentic_transcript=finding.agentic_transcript,
    )


async def _annotation_for(
    session: AsyncSession, finding: Finding
) -> FindingAnnotation | None:
    return await session.scalar(
        select(FindingAnnotation).where(FindingAnnotation.finding_id == finding.id)
    )


async def _feedback_response(
    session: AsyncSession, finding: Finding
) -> FeedbackOut | Response:
    annotation = await _annotation_for(session, finding)
    if annotation is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    reviewer = await session.get(User, annotation.reviewer_user_id)
    return FeedbackOut(
        label=annotation.label,
        researcher_note=annotation.researcher_note,
        reviewer_username=reviewer.username if reviewer else "(unknown)",
        updated_at=annotation.updated_at,
        created_at=annotation.created_at,
        duplicate_of_finding_id=(
            str(annotation.duplicate_of_finding_id)
            if annotation.duplicate_of_finding_id is not None
            else None
        ),
        duplicate_of=await _duplicate_of_summary(
            session, annotation.duplicate_of_finding_id
        ),
    )


def _validate_label(label: str) -> None:
    if label not in LABELS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown feedback label `{label}`; expected one of {LABELS}.",
        )


def _bad(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail=message
    )


async def _validate_duplicate_payload(
    session: AsyncSession,
    *,
    finding: Finding,
    body: FeedbackIn,
) -> None:
    """Enforce the duplicate-label invariants from
    ``add-duplicate-feedback-label`` design D1+D2+D3 at the API
    layer. Raises ``HTTPException(400)`` on violation; returns
    silently on success.

    Order (each check named in the error message it raises):

    1. Biconditional: ``label == "duplicate"`` ↔
       ``duplicate_of_finding_id is not None``.
    2. Self-reference rejection
       (``duplicate_of_finding_id != finding.id``).
    3. Target finding exists.
    4. Target finding's ``run_id`` matches the current
       finding's ``run_id`` (same-run rule, D1).
    5. Target finding has no annotation row with
       ``label == "duplicate"`` (single-level rule, D2).

    Steps 3-5 only run when the label is ``duplicate``; the
    other branches early-return after the biconditional check.
    """
    label = body.label
    pointer = body.duplicate_of_finding_id
    is_duplicate = label == LABEL_DUPLICATE
    has_pointer = pointer is not None
    if is_duplicate and not has_pointer:
        raise _bad(
            "label='duplicate' requires duplicate_of_finding_id; "
            "received null"
        )
    if not is_duplicate and has_pointer:
        raise _bad(
            "duplicate_of_finding_id is only valid when "
            f"label='duplicate'; got label='{label}'"
        )
    if not is_duplicate:
        return  # nothing more to check for non-duplicate labels
    if pointer == str(finding.id):
        raise _bad(
            "duplicate_of_finding_id must not equal the finding's "
            "own id (self-reference rejected)"
        )
    target = await session.get(Finding, pointer)
    if target is None:
        raise _bad(
            f"duplicate_of_finding_id `{pointer}` does not match any "
            "existing finding"
        )
    if target.run_id != finding.run_id:
        raise _bad(
            "same-run rule: duplicate target must belong to the "
            "same audit run as the current finding "
            f"(current run={finding.run_id}, target run={target.run_id})"
        )
    target_annotation = await session.scalar(
        select(FindingAnnotation).where(
            FindingAnnotation.finding_id == target.id
        )
    )
    if target_annotation is not None and target_annotation.label == LABEL_DUPLICATE:
        raise _bad(
            "single-level rule: duplicate target is itself labeled "
            "'duplicate'; pick its canonical finding instead"
        )


async def _duplicate_of_summary(
    session: AsyncSession,
    duplicate_of_finding_id,
) -> DuplicateOfSummary | None:
    if duplicate_of_finding_id is None:
        return None
    target = await session.get(Finding, duplicate_of_finding_id)
    if target is None:
        # Pointer was cleared by ``ON DELETE SET NULL`` after the
        # canonical finding was deleted but BEFORE the
        # accompanying CHECK violation was repaired by an
        # operator. Surface the NULL summary; the chip on the FE
        # will fall back to "Duplicate (target deleted)".
        return None
    return DuplicateOfSummary(
        id=str(target.id),
        finding_id=target.finding_id,
        name=target.finding_name,
    )


@router.get("/findings/{finding_id}/feedback", response_model=None)
async def get_feedback(
    finding_id: str,
    session: AsyncSession = Depends(get_session),
):
    finding = await _load_finding(session, finding_id)
    return await _feedback_response(session, finding)


@router.post("/findings/{finding_id}/feedback", response_model=FeedbackOut)
async def create_feedback(
    finding_id: str,
    body: FeedbackIn,
    session: AsyncSession = Depends(get_session),
    reviewer: User = Depends(require_writer),
) -> FeedbackOut:
    _validate_label(body.label)
    finding = await _load_finding(session, finding_id)
    await _validate_duplicate_payload(session, finding=finding, body=body)
    existing = await _annotation_for(session, finding)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Annotation already exists; use PATCH to update.",
        )
    new_pointer = body.duplicate_of_finding_id
    annotation = FindingAnnotation(
        run_id=finding.run_id,
        finding_id=finding.id,
        label=body.label,
        researcher_note=body.researcher_note,
        reviewer_user_id=reviewer.id,
        duplicate_of_finding_id=new_pointer,
    )
    session.add(annotation)
    await session.flush()
    session.add(
        AnnotationHistory(
            annotation_id=annotation.id,
            previous_label=None,
            new_label=body.label,
            previous_note=None,
            new_note=body.researcher_note,
            previous_duplicate_of=None,
            new_duplicate_of=new_pointer,
            changed_by_user_id=reviewer.id,
        )
    )
    await session.commit()
    return FeedbackOut(
        label=annotation.label,
        researcher_note=annotation.researcher_note,
        reviewer_username=reviewer.username,
        updated_at=annotation.updated_at,
        created_at=annotation.created_at,
        duplicate_of_finding_id=(
            str(annotation.duplicate_of_finding_id)
            if annotation.duplicate_of_finding_id is not None
            else None
        ),
        duplicate_of=await _duplicate_of_summary(
            session, annotation.duplicate_of_finding_id
        ),
    )


@router.get(
    "/findings/{finding_id}/duplicates",
    response_model=IncomingDuplicatesPage,
)
async def list_incoming_duplicates(
    finding_id: str,
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> IncomingDuplicatesPage:
    """Reverse-query: return findings whose annotation has
    ``duplicate_of_finding_id == <finding_id>``. Lazy-loaded
    by the FE on card expand. Required by
    ``add-duplicate-feedback-label`` D5.
    """
    finding = await _load_finding(session, finding_id)
    base = (
        select(Finding, FindingAnnotation)
        .join(
            FindingAnnotation,
            FindingAnnotation.finding_id == Finding.id,
        )
        .where(FindingAnnotation.duplicate_of_finding_id == finding.id)
    )
    total_query = (
        select(FindingAnnotation.id).where(
            FindingAnnotation.duplicate_of_finding_id == finding.id
        )
    )
    total = len((await session.execute(total_query)).all())
    rows = (
        await session.execute(
            base.order_by(Finding.created_at.asc()).offset(offset).limit(limit)
        )
    ).all()
    items = [
        IncomingDuplicate(
            id=str(f.id),
            finding_id=f.finding_id,
            name=f.finding_name,
            run_id=str(f.run_id),
            annotation_id=str(a.id),
        )
        for (f, a) in rows
    ]
    return IncomingDuplicatesPage(items=items, total=total)


@router.patch("/findings/{finding_id}/feedback", response_model=FeedbackOut)
async def update_feedback(
    finding_id: str,
    body: FeedbackIn,
    session: AsyncSession = Depends(get_session),
    reviewer: User = Depends(require_writer),
) -> FeedbackOut:
    _validate_label(body.label)
    finding = await _load_finding(session, finding_id)
    await _validate_duplicate_payload(session, finding=finding, body=body)
    annotation = await _annotation_for(session, finding)
    if annotation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No annotation exists; use POST to create.",
        )
    previous_label = annotation.label
    previous_note = annotation.researcher_note
    previous_pointer = annotation.duplicate_of_finding_id
    new_pointer = body.duplicate_of_finding_id
    annotation.label = body.label
    annotation.researcher_note = body.researcher_note
    # ``_validate_duplicate_payload`` already enforced the
    # biconditional, so the pointer transitions safely:
    # - duplicate → duplicate (same / different target): set
    # - non-duplicate → duplicate: set (was None)
    # - duplicate → non-duplicate: clear (body had None)
    annotation.duplicate_of_finding_id = new_pointer
    annotation.updated_at = datetime.now()
    session.add(
        AnnotationHistory(
            annotation_id=annotation.id,
            previous_label=previous_label,
            new_label=body.label,
            previous_note=previous_note,
            new_note=body.researcher_note,
            previous_duplicate_of=previous_pointer,
            new_duplicate_of=new_pointer,
            changed_by_user_id=reviewer.id,
        )
    )
    await session.commit()
    return FeedbackOut(
        label=annotation.label,
        researcher_note=annotation.researcher_note,
        reviewer_username=reviewer.username,
        updated_at=annotation.updated_at,
        created_at=annotation.created_at,
        duplicate_of_finding_id=(
            str(annotation.duplicate_of_finding_id)
            if annotation.duplicate_of_finding_id is not None
            else None
        ),
        duplicate_of=await _duplicate_of_summary(
            session, annotation.duplicate_of_finding_id
        ),
    )
