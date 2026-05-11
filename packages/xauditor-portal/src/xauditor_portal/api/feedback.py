"""Dataset-export endpoint for human-feedback labels."""

from __future__ import annotations

import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from xauditor_portal.auth import forbid_must_change_password, get_session
from xauditor_portal.db.models.auth import User
from xauditor_portal.db.models.feedback import FindingAnnotation
from xauditor_portal.db.models.report import AuditRun, Finding


router = APIRouter(tags=["feedback"], dependencies=[Depends(forbid_must_change_password)])


_SECRET_PATTERNS = (
    "password",
    "api_key",
    "secret",
    "token",
    "bearer",
)


def _redact(value: str) -> str:
    lowered = value.lower()
    if any(p in lowered for p in _SECRET_PATTERNS):
        # Be conservative: if the text mentions a secret concept, keep the
        # mention but strip any obvious token-shaped payloads.
        return " ".join(
            "[REDACTED]" if any(p in word.lower() for p in _SECRET_PATTERNS) else word
            for word in value.split()
        )
    return value


@router.get("/runs/{run_id}/feedback-export")
async def feedback_export(
    run_id: str,
    format: str = Query(default="jsonl", pattern="^jsonl$"),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    run = await session.get(AuditRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audit run not found.")
    findings = (
        await session.execute(
            select(Finding).where(Finding.run_id == run_id).order_by(Finding.created_at)
        )
    ).scalars().all()
    annotations = {
        a.finding_id: a
        for a in (
            await session.execute(
                select(FindingAnnotation).where(FindingAnnotation.run_id == run_id)
            )
        )
        .scalars()
        .all()
    }
    reviewer_ids = {a.reviewer_user_id for a in annotations.values()}
    reviewers: dict[str, str] = {}
    if reviewer_ids:
        for user_id in reviewer_ids:
            u = await session.get(User, user_id)
            if u is not None:
                reviewers[str(u.id)] = u.username

    async def emit() -> AsyncIterator[bytes]:
        for finding in findings:
            annotation = annotations.get(finding.id)
            record = {
                "run_id": str(finding.run_id),
                "finding_id": finding.finding_id,
                "finding_name": finding.finding_name,
                "finding_description": _redact(finding.finding_description),
                "confidence_level": finding.confidence_level,
                "analyzer_status": finding.analyzer_status,
                "evidence_strength": finding.evidence_strength,
                "analysis": _redact(finding.analysis),
                "reason": _redact(finding.reason),
                "context": _redact(finding.context),
                "context_notes": _redact(finding.context_notes) if finding.context_notes else None,
                "suspect_function_id": finding.suspect_function_id,
                "suspect_line": finding.suspect_line,
                "exploitation_status": finding.exploitation_status,
                "exploitation_steps": _redact(finding.exploitation_steps),
                "validation_status": finding.validation_status,
                "validation_analysis": _redact(finding.validation_analysis),
                "feedback": None
                if annotation is None
                else {
                    "label": annotation.label,
                    "researcher_note": annotation.researcher_note,
                    "reviewer_username": reviewers.get(str(annotation.reviewer_user_id)),
                    "created_at": annotation.created_at.isoformat(),
                    "updated_at": annotation.updated_at.isoformat(),
                },
            }
            yield (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")

    return StreamingResponse(emit(), media_type="application/jsonl")
