"""Report sinks. PostgresReportSink is implemented in Phase 6."""

from xauditor_portal.sinks.postgres_sink import (  # noqa: F401
    PostgresReportSink,
    RunDeletedExternallyError,
)

__all__ = [
    "PostgresReportSink",
    "RunDeletedExternallyError",
]
