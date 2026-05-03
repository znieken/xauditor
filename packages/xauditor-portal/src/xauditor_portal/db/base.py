"""Shared SQLAlchemy base, engine factory, and async session maker.

Runtime code imports `Base` to register ORM models, `build_engine_url` to
translate a `ReportDBConfig` into an asyncpg URL, and
`create_async_engine_for` / `make_async_session_factory` to wire a working
engine+session pair.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import DateTime
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

if TYPE_CHECKING:
    from xauditor.config import ReportDBConfig


DB_SCHEMAS: tuple[str, ...] = ("report", "config", "auth", "feedback")


class Base(DeclarativeBase):
    """Root declarative base for every xauditor-portal ORM model.

    We intentionally keep this minimal. Schema assignment lives on each
    model's ``__table_args__``; Alembic's env.py iterates `DB_SCHEMAS` to
    emit `CREATE SCHEMA IF NOT EXISTS` statements before running autogen.
    """

    type_annotation_map = {
        datetime: DateTime(timezone=True),
    }


def utcnow() -> datetime:
    """Timezone-aware UTC now; used for `default`/`server_default` columns."""

    return datetime.now(timezone.utc)


def build_engine_url(config: "ReportDBConfig") -> str:
    """Compute the SQLAlchemy URL for the configured report database.

    When ``remote.url`` is set, its value is returned with any `postgresql://`
    or `postgres://` scheme rewritten to `postgresql+asyncpg://` so it works
    with the asyncpg driver. Otherwise an asyncpg URL for the managed
    container (on `127.0.0.1`) is synthesized from the config.
    """

    if config.remote is not None:
        return _ensure_asyncpg_scheme(config.remote.url)
    return (
        f"postgresql+asyncpg://{config.username}:{config.password}"
        f"@127.0.0.1:{config.port}/{config.database}"
    )


def _ensure_asyncpg_scheme(url: str) -> str:
    lowered = url.lower()
    if lowered.startswith("postgresql+asyncpg://"):
        return url
    if lowered.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    if lowered.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    return url


def create_async_engine_for(config: "ReportDBConfig", **engine_kwargs) -> AsyncEngine:
    """Build an AsyncEngine from a ReportDBConfig.

    Extra kwargs are forwarded to :func:`sqlalchemy.ext.asyncio.create_async_engine`.
    """

    pool_size = config.remote.pool_size if config.remote and config.remote.pool_size else None
    kwargs: dict[str, object] = {"future": True, "pool_pre_ping": True}
    if pool_size is not None:
        kwargs["pool_size"] = pool_size
    kwargs.update(engine_kwargs)
    return create_async_engine(build_engine_url(config), **kwargs)


def make_async_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


__all__ = [
    "AsyncEngine",
    "AsyncSession",
    "Base",
    "DB_SCHEMAS",
    "build_engine_url",
    "create_async_engine_for",
    "make_async_session_factory",
    "utcnow",
]
