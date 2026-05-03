"""All ORM models, packaged per PostgreSQL schema.

Importing this module registers every table on ``Base.metadata`` and is the
entry point Alembic's env.py uses to drive migrations.
"""

from xauditor_portal.db.base import Base  # noqa: F401
from xauditor_portal.db.models import auth, config, feedback, report  # noqa: F401

__all__ = ["Base", "auth", "config", "feedback", "report"]
