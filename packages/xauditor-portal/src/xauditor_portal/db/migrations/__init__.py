"""Alembic migration runner entry points.

The main `xauditor` package calls `upgrade_to_head(config)` from its
`reportdb_init` flow when `xauditor-portal` is importable. Keeping this
wrapper here lets the CLI stay agnostic about Alembic paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xauditor.config import ReportDBConfig


def _sync_alembic_url(reportdb) -> str:
    """Return a synchronous SQLAlchemy URL for Alembic.

    Alembic's ``command.upgrade()`` runs synchronously and cannot use the
    asyncpg driver. We explicitly rewrite the async URL to
    ``postgresql+psycopg://`` (psycopg v3) for the migration run so that
    SQLAlchemy does not fall back to importing ``psycopg2``. The FastAPI
    runtime continues to use the asyncpg URL via ``build_engine_url()``.
    """

    from xauditor_portal.db.base import build_engine_url

    url = build_engine_url(reportdb)
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + url[len("postgresql+asyncpg://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    return url


def _alembic_config_for(reportdb):
    from alembic.config import Config

    # The shipped alembic.ini lives two directories up from this file,
    # alongside `src/`. Walk up from `__file__` to find it.
    here = Path(__file__).resolve()
    package_root: Path | None = None
    for parent in here.parents:
        candidate = parent / "alembic.ini"
        if candidate.exists() and (parent / "src").exists():
            package_root = parent
            break
    if package_root is None:
        # Fall back to an in-memory config that points `script_location` at
        # this directory and synthesizes the URL from the ReportDBConfig.
        cfg = Config()
    else:
        cfg = Config(str(package_root / "alembic.ini"))
    cfg.set_main_option(
        "script_location", str(Path(__file__).resolve().parent)
    )
    cfg.set_main_option("sqlalchemy.url", _sync_alembic_url(reportdb))
    return cfg


def upgrade_to_head(reportdb: "ReportDBConfig") -> None:
    """Run Alembic migrations to head against the configured report DB."""

    from alembic import command

    cfg = _alembic_config_for(reportdb)
    command.upgrade(cfg, "head")


def upgrade_to_head_for_url(async_url: str) -> None:
    """Run Alembic migrations to head against a raw SQLAlchemy URL.

    Use when ``xauditor.config.ReportDBConfig`` is not importable (for
    example inside the portal Docker image, which ships without the main
    ``xauditor`` package). ``async_url`` is typically an asyncpg URL
    injected via ``XAUDITOR_PORTAL_DATABASE_URL``; we rewrite the scheme
    to ``postgresql+psycopg://`` for the sync Alembic run just like
    :func:`upgrade_to_head` does.
    """

    from alembic import command
    from alembic.config import Config

    sync_url = async_url
    if sync_url.startswith("postgresql+asyncpg://"):
        sync_url = "postgresql+psycopg://" + sync_url[len("postgresql+asyncpg://") :]
    elif sync_url.startswith("postgresql://"):
        sync_url = "postgresql+psycopg://" + sync_url[len("postgresql://") :]
    elif sync_url.startswith("postgres://"):
        sync_url = "postgresql+psycopg://" + sync_url[len("postgres://") :]

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parent))
    cfg.set_main_option("sqlalchemy.url", sync_url)
    command.upgrade(cfg, "head")


def downgrade_to_base(reportdb: "ReportDBConfig") -> None:
    """Drop every migrated table — useful for disposable test fixtures."""

    from alembic import command

    cfg = _alembic_config_for(reportdb)
    command.downgrade(cfg, "base")


__all__ = ["downgrade_to_base", "upgrade_to_head", "upgrade_to_head_for_url"]
