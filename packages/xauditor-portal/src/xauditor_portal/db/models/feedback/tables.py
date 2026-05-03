"""`feedback` schema tables: finding annotations (true/false positive labels)
plus an audit-trail history of every label change.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from xauditor_portal.db.base import Base, utcnow


SCHEMA = "feedback"

LABEL_TRUE_POSITIVE = "true_positive"
LABEL_FALSE_POSITIVE = "false_positive"
LABEL_DUPLICATE = "duplicate"
LABEL_UNLABELED = "unlabeled"
LABELS = (
    LABEL_TRUE_POSITIVE,
    LABEL_FALSE_POSITIVE,
    LABEL_DUPLICATE,
    LABEL_UNLABELED,
)


class FindingAnnotation(Base):
    __tablename__ = "finding_annotations"
    __table_args__ = (
        UniqueConstraint("run_id", "finding_id", name="uq_finding_annotations_run_finding"),
        # Biconditional: ``label = 'duplicate'`` iff
        # ``duplicate_of_finding_id IS NOT NULL``. The pointer
        # column is meaningful only for the duplicate label;
        # any non-duplicate label MUST keep it NULL. The CHECK
        # is the database-side enforcement of D3 in the
        # ``add-duplicate-feedback-label`` change.
        CheckConstraint(
            "(label = 'duplicate' AND duplicate_of_finding_id IS NOT NULL) "
            "OR (label <> 'duplicate' AND duplicate_of_finding_id IS NULL)",
            name="ck_finding_annotations_duplicate_pointer",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("report.audit_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    finding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("report.findings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    researcher_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth.users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Canonical-target pointer for ``label = 'duplicate'``. FK
    # to ``report.findings(id)`` with ``ON DELETE SET NULL`` so
    # deleting the canonical row nulls the pointer rather than
    # cascading the delete to the duplicate annotation. The
    # same-run rule + single-level rule + self-ref rejection
    # are enforced at the API layer; the CHECK constraint above
    # only enforces the biconditional with ``label``.
    duplicate_of_finding_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("report.findings.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)

    history: Mapped[list["AnnotationHistory"]] = relationship(
        back_populates="annotation", cascade="all, delete-orphan"
    )


class AnnotationHistory(Base):
    __tablename__ = "annotation_history"
    __table_args__ = {"schema": SCHEMA}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    annotation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(f"{SCHEMA}.finding_annotations.id", ondelete="CASCADE"),
        nullable=False,
    )
    previous_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_label: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Audit-trail pair for the duplicate-target pointer. Both
    # nullable; the writer fills them per-PATCH so the history
    # carries old/new pointer values alongside the existing
    # label / note diff. Same FK shape as the canonical
    # ``finding_annotations.duplicate_of_finding_id`` column —
    # ``ON DELETE SET NULL`` so deleting a finding doesn't
    # erase the audit trail.
    previous_duplicate_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("report.findings.id", ondelete="SET NULL"),
        nullable=True,
    )
    new_duplicate_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("report.findings.id", ondelete="SET NULL"),
        nullable=True,
    )
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auth.users.id", ondelete="SET NULL"),
        nullable=True,
    )
    changed_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)

    annotation: Mapped[FindingAnnotation] = relationship(back_populates="history")


__all__ = [
    "AnnotationHistory",
    "FindingAnnotation",
    "LABELS",
    "LABEL_DUPLICATE",
    "LABEL_FALSE_POSITIVE",
    "LABEL_TRUE_POSITIVE",
    "LABEL_UNLABELED",
    "SCHEMA",
]
