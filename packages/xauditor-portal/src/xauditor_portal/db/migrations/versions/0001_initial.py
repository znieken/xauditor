"""Initial schema: create report / config / auth / feedback tables + seed user.

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-18
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from xauditor_portal.db.base import Base, DB_SCHEMAS
from xauditor_portal.db.models import Base as _Base  # noqa: F401 - ensure model modules are imported
from xauditor_portal.db import seed as seed_module


revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # Schemas first, then every declared table. `env.py` already emits CREATE
    # SCHEMA IF NOT EXISTS before this migration runs, but we repeat it here
    # so `alembic upgrade head --sql` (offline mode) also produces a complete
    # SQL dump without relying on env.py's connection.
    for schema in DB_SCHEMAS:
        op.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    Base.metadata.create_all(bind=bind)

    # Seed the default auditor user iff auth.users is currently empty. We use
    # a raw session bound to the migration connection so the seed happens in
    # the same transaction as the table creation.
    from sqlalchemy.orm import Session

    session = Session(bind=bind)
    try:
        seed_module.seed_default_user(session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
    for schema in reversed(DB_SCHEMAS):
        op.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
