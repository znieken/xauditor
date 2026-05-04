"""`/api/projects` endpoints.

Projects and graph-build listings are served as **aggregates** over the
`report.audit_runs` table — no dedicated table holds project metadata.
This matches how Markdown artifacts are organized on disk (one directory
per run, with repo_root / project_name / build_fingerprint stamped into
each) and avoids a second write path during audit execution.

A `project_key` is the 24-hex-char prefix of
``sha256(repo_root + "\\x00" + project_name)`` so URLs stay stable across
renames of one of the two fields.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import case, desc, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from xauditor_portal.api.runs import (
    RunSummary,
    _batch_run_metrics,
    _to_summary,
    _zero_metrics,
)
from xauditor_portal.auth import forbid_must_change_password, get_session
from xauditor_portal.db.models.report import AuditRun


router = APIRouter(tags=["projects"], dependencies=[Depends(forbid_must_change_password)])


class ProjectSummary(BaseModel):
    project_key: str
    project_name: str
    repo_root: str
    total_graph_builds: int
    total_audit_runs: int
    running_audit_runs: int
    added_at: datetime


class ProjectsPage(BaseModel):
    items: list[ProjectSummary]
    total: int


class BuildSummary(BaseModel):
    build_fingerprint: str
    built_at: datetime
    total_audit_runs: int
    last_run_started_at: datetime
    last_run_status: str


class BuildsPage(BaseModel):
    items: list[BuildSummary]
    total: int


class RunsPage(BaseModel):
    items: list[RunSummary]
    total: int


def project_key(repo_root: str, project_name: str) -> str:
    """Deterministic short id for the (repo_root, project_name) pair."""

    h = hashlib.sha256()
    h.update(repo_root.encode("utf-8"))
    h.update(b"\x00")
    h.update(project_name.encode("utf-8"))
    return h.hexdigest()[:24]


async def _resolve_project_identifiers(
    session: AsyncSession, key: str
) -> tuple[str, str]:
    """Return the (repo_root, project_name) pair matching the hashed key.

    Raises HTTPException(404) when the key matches no existing run.
    """

    pairs = (
        await session.execute(
            select(AuditRun.repo_root, AuditRun.project_name).distinct()
        )
    ).all()
    for repo_root, project_name in pairs:
        if project_key(repo_root, project_name) == key:
            return repo_root, project_name
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Project not found for the given project_key.",
    )


@router.get("", response_model=ProjectsPage)
async def list_projects(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> ProjectsPage:
    aggregated = (
        select(
            AuditRun.repo_root,
            AuditRun.project_name,
            func.count(func.distinct(AuditRun.build_fingerprint)).label("builds"),
            func.count(AuditRun.id).label("runs"),
            func.sum(case((AuditRun.status == "in_progress", 1), else_=0)).label(
                "running"
            ),
            func.min(AuditRun.started_at).label("added_at"),
        )
        .group_by(AuditRun.repo_root, AuditRun.project_name)
        .order_by(desc("added_at"))
    )
    total = (
        await session.execute(
            select(func.count()).select_from(aggregated.subquery())
        )
    ).scalar_one()
    rows = (
        await session.execute(aggregated.offset(offset).limit(limit))
    ).all()
    items = [
        ProjectSummary(
            project_key=project_key(row.repo_root, row.project_name),
            project_name=row.project_name,
            repo_root=row.repo_root,
            total_graph_builds=int(row.builds or 0),
            total_audit_runs=int(row.runs or 0),
            running_audit_runs=int(row.running or 0),
            added_at=row.added_at,
        )
        for row in rows
    ]
    return ProjectsPage(items=items, total=int(total))


@router.get("/{project_key}/builds", response_model=BuildsPage)
async def list_project_builds(
    project_key: str,
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> BuildsPage:
    repo_root, project_name = await _resolve_project_identifiers(session, project_key)
    # Correlated sub-queries for "last run" info keep the query scoped to a
    # single group-by pass per page.
    aggregated = (
        select(
            AuditRun.build_fingerprint,
            func.count(AuditRun.id).label("runs"),
            func.min(AuditRun.started_at).label("built_at"),
            func.max(AuditRun.started_at).label("last_run_started_at"),
        )
        .where(AuditRun.repo_root == repo_root)
        .where(AuditRun.project_name == project_name)
        .group_by(AuditRun.build_fingerprint)
        .order_by(desc("last_run_started_at"))
    )
    total = (
        await session.execute(
            select(func.count()).select_from(aggregated.subquery())
        )
    ).scalar_one()
    rows = (
        await session.execute(aggregated.offset(offset).limit(limit))
    ).all()
    build_fingerprints = [row.build_fingerprint for row in rows]
    status_map: dict[tuple[str, datetime], str] = {}
    if build_fingerprints:
        latest_keys = [
            (row.build_fingerprint, row.last_run_started_at) for row in rows
        ]
        status_rows = (
            await session.execute(
                select(
                    AuditRun.build_fingerprint,
                    AuditRun.started_at,
                    AuditRun.status,
                )
                .where(AuditRun.repo_root == repo_root)
                .where(AuditRun.project_name == project_name)
                .where(
                    tuple_(AuditRun.build_fingerprint, AuditRun.started_at).in_(
                        latest_keys
                    )
                )
            )
        ).all()
        status_map = {
            (r.build_fingerprint, r.started_at): r.status for r in status_rows
        }
    items = [
        BuildSummary(
            build_fingerprint=row.build_fingerprint,
            built_at=row.built_at,
            total_audit_runs=int(row.runs or 0),
            last_run_started_at=row.last_run_started_at,
            last_run_status=status_map.get(
                (row.build_fingerprint, row.last_run_started_at), ""
            ),
        )
        for row in rows
    ]
    return BuildsPage(items=items, total=int(total))


@router.get(
    "/{project_key}/builds/{build_fingerprint}/runs",
    response_model=RunsPage,
)
async def list_build_runs(
    project_key: str,
    build_fingerprint: str,
    session: AsyncSession = Depends(get_session),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> RunsPage:
    repo_root, project_name = await _resolve_project_identifiers(session, project_key)
    base = (
        select(AuditRun)
        .where(AuditRun.repo_root == repo_root)
        .where(AuditRun.project_name == project_name)
        .where(AuditRun.build_fingerprint == build_fingerprint)
    )
    total = (
        await session.execute(
            select(func.count()).select_from(base.subquery())
        )
    ).scalar_one()
    rows = (
        await session.execute(
            base.order_by(AuditRun.started_at.desc()).offset(offset).limit(limit)
        )
    ).scalars().all()
    metrics_map = await _batch_run_metrics(session, [str(r.id) for r in rows])
    items = [
        _to_summary(r, metrics_map.get(str(r.id), _zero_metrics())) for r in rows
    ]
    return RunsPage(items=items, total=int(total))


__all__ = ["project_key", "router"]
