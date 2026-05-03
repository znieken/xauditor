"""`config` schema tables.

Stores UI-edited xauditor configuration as append-only JSONB snapshots, plus
a per-key audit of what the UI wrote vs what yml overrode at resolution
time. Remote DB endpoints never appear here; they live only in xauditor.yml.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from xauditor_portal.db.base import Base, utcnow


SCHEMA = "config"


class ConfigSnapshot(Base):
    __tablename__ = "config_snapshots"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth.users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class ConfigOverride(Base):
    __tablename__ = "config_overrides"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    key: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # yml | db | default
    value_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.config_snapshots.id", ondelete="CASCADE"),
        nullable=True,
    )
    detected_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)


__all__ = ["ConfigOverride", "ConfigSnapshot", "SCHEMA"]
