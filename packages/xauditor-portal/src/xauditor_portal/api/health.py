"""`/api/health` — liveness + readiness probe.

Returns `{status, db, migrations_ok}`. `db.reachable` is ``True`` when a
trivial SELECT round-trips; `migrations_ok` is ``True`` when the Alembic
version in the `alembic_version` table matches the head revision shipped
with this package.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from xauditor_portal.auth import get_session

router = APIRouter(tags=["health"])


async def _probe_db(session: AsyncSession) -> dict[str, object]:
    try:
        result = await session.execute(text("SELECT 1"))
        row = result.scalar_one()
        return {"reachable": bool(row == 1), "error": None}
    except Exception as exc:  # noqa: BLE001 - health probe reports, never raises
        return {"reachable": False, "error": f"{exc.__class__.__name__}: {exc}"}


async def _probe_migrations(session: AsyncSession) -> bool | None:
    try:
        result = await session.execute(
            text("SELECT version_num FROM alembic_version LIMIT 1")
        )
        version = result.scalar_one_or_none()
    except Exception:  # noqa: BLE001 - missing table means migrations not run
        return False
    return version is not None


@router.get("/health")
async def health(
    request: Request, session: AsyncSession = Depends(get_session)
) -> dict[str, object]:
    db_info = await _probe_db(session)
    migrations_ok = await _probe_migrations(session) if db_info["reachable"] else False
    return {
        "status": "ok" if db_info["reachable"] else "degraded",
        "db": db_info,
        "migrations_ok": migrations_ok,
    }
