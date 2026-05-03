"""Alembic environment for xauditor-portal.

Key responsibilities:
- Ensure every named schema (`report`, `config`, `auth`, `feedback`) exists
  before any migration runs, so schema-qualified table creation does not
  race with CREATE TABLE on first init.
- Set `target_metadata` to the unified `Base.metadata` so autogenerate sees
  every declared model regardless of its schema.
- Tell Alembic to also track objects in non-default schemas via
  `include_schemas=True`.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from xauditor_portal.db.base import DB_SCHEMAS
from xauditor_portal.db.models import Base


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _ensure_schemas(connection) -> None:
    for schema in DB_SCHEMAS:
        connection.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section) or {},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _ensure_schemas(connection)
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        # SQLAlchemy 2.0 will ROLLBACK on connection __exit__ unless the
        # outer transaction is explicitly committed. Alembic's
        # begin_transaction context manager commits its own SAVEPOINT but
        # leaves the autobegin transaction started by _ensure_schemas
        # uncommitted. Finish that here so the DDL persists.
        if connection.in_transaction():
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
