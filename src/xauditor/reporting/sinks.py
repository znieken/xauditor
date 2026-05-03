"""Report sink Protocol + bus + RunMeta.

Phase 2 of ``make-postgres-the-canonical-sink``: there is exactly one
production sink — ``xauditor_portal.sinks.postgres_sink.PostgresReportSink``.
The legacy ``MarkdownReportSink`` has been removed. The Markdown shape
operators expect (`findings.md`, `false-positives.md`, `coverage-report.md`,
…) is now produced on demand by ``xauditor audit export <run_id>
--format markdown``, reading from the persisted Postgres rows. See
``openspec/changes/make-postgres-the-canonical-sink``.

Design invariants:
- ``ReportSinkBus`` exists solely to serialise concurrent worker
  callbacks (multiple coder threads streaming verdicts) into the
  underlying sink. There is no primary / secondary distinction
  anymore — there is only one sink.
- Sink callbacks are split into per-row (``upsert_finding``) and
  full-snapshot (``emit_snapshot``) writes. Per-row fires on every
  coder settle (~O(N)). Full-snapshot fires at path boundaries and on
  finalize so coverage / debates / subagents stay eventually
  consistent.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from xauditor.models import AuditRun, Finding


log = logging.getLogger("xauditor.reporting.sinks")


# Lazily imported reference to the portal sink's terminal-abort exception.
# Cached after first lookup (success or failure) so the import cost is paid
# once. ``None`` means the portal package is not installed — in that case
# no sink in the bus can raise the exception, so the generic ``except``
# swallow is correct and we skip the propagation check.
_RUN_DELETED_EXTERNALLY_ERROR: type[BaseException] | None = None
_RUN_DELETED_EXTERNALLY_LOOKUP_DONE = False


def _run_deleted_externally_error_class() -> type[BaseException] | None:
    global _RUN_DELETED_EXTERNALLY_ERROR, _RUN_DELETED_EXTERNALLY_LOOKUP_DONE
    if not _RUN_DELETED_EXTERNALLY_LOOKUP_DONE:
        try:
            from xauditor_portal.sinks import (
                RunDeletedExternallyError as _cls,
            )
            _RUN_DELETED_EXTERNALLY_ERROR = _cls
        except ImportError:
            _RUN_DELETED_EXTERNALLY_ERROR = None
        _RUN_DELETED_EXTERNALLY_LOOKUP_DONE = True
    return _RUN_DELETED_EXTERNALLY_ERROR


def _is_run_deleted_externally(exc: BaseException) -> bool:
    """True iff the exception is the portal sink's terminal-abort signal.

    Used by every defensive ``except Exception`` clause that wraps a
    sink-bound call to differentiate a swallowable best-effort failure
    (network blip, malformed row, etc.) from the one exception class
    that the audit MUST abort on. See the
    ``portal-user-disable-and-ux-fixes`` change's
    ``report-postgres-persistence`` delta.
    """

    cls = _run_deleted_externally_error_class()
    return cls is not None and isinstance(exc, cls)


PROGRESS_STAGES = (
    "graph-build",
    "analyzer",
    "validator",
    "exploiter",
    "reporting",
)

HEARTBEAT_KINDS = (
    "started",
    "progress",
    "stage_completed",
    "finished",
    "failed",
)


@dataclass(frozen=True)
class RunMeta:
    """Metadata about a run that does not fit in the live AuditRun snapshot.

    ``run_label`` is the stable string identifier the operator uses to
    refer to the run in CLI commands (``xauditor audit export <label>``,
    ``xauditor audit resume --run-id <label>``). For runs created by
    ``services.run_audit`` it is the per-invocation ``YYYYMMDD-HHMMSS``
    timestamp — which used to also be the on-disk
    ``<reports_dir>/<run_label>/`` directory name; the directory is gone
    in 0.5.0 but the label format is preserved for muscle-memory
    compatibility. Sinks that key audit runs by string (the Postgres
    sink stores it as ``audit_runs.report_dir``) use this value.
    """

    repo_root: str
    project_name: str
    build_fingerprint: str
    mode: str  # "single" | "team"
    run_label: str
    started_at: datetime
    llm_providers_used: dict[str, Any] = field(default_factory=dict)
    resumed: bool = False


@dataclass(frozen=True)
class ProgressEvent:
    stage: str
    heartbeat_kind: str
    current_path_index: int | None = None
    total_paths: int | None = None
    message: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(_utc()))


def _utc():  # tiny helper so the module-load default doesn't pin timezone
    from datetime import timezone

    return timezone.utc


@runtime_checkable
class ReportSink(Protocol):
    def open_run(self, meta: RunMeta) -> None: ...
    def emit_snapshot(self, snapshot: AuditRun) -> None: ...
    def upsert_finding(self, run_id: str, finding: Finding) -> None: ...
    def write_progress(self, event: ProgressEvent) -> None: ...
    def write_resume_state(self, payload: Mapping[str, Any]) -> None: ...
    def close_run(self, final: AuditRun) -> None: ...
    def fail_run(self, *, status: str, error: str) -> None: ...


class ReportSinkBus:
    """Thread-safe wrapper around the single production sink.

    Phase 1 added per-row ``upsert_finding`` to the streaming path. Coder
    workers settle in a thread pool, so multiple worker threads can call
    sink methods concurrently. The bus serialises every method through a
    single ``threading.Lock`` so the Postgres sink (or any future
    pluggable sink) doesn't have to be re-entrant.

    The class used to fan out to a primary + N secondaries; that has
    collapsed in Phase 2 since the Markdown sink is gone. The wrapper
    survives because the lock + Protocol indirection keeps
    ``services.run_audit`` cleaner than calling sink methods directly.
    """

    def __init__(self, sink: ReportSink) -> None:
        self.sink = sink
        self._lock = threading.Lock()

    def open_run(self, meta: RunMeta) -> None:
        with self._lock:
            self.sink.open_run(meta)

    def emit_snapshot(self, snapshot: AuditRun) -> None:
        with self._lock:
            self.sink.emit_snapshot(snapshot)

    def upsert_finding(self, run_id: str, finding: Finding) -> None:
        with self._lock:
            self.sink.upsert_finding(run_id, finding)

    def write_progress(self, event: ProgressEvent) -> None:
        with self._lock:
            try:
                self.sink.write_progress(event)
            except Exception as exc:  # noqa: BLE001 - progress events are best-effort
                # ``RunDeletedExternallyError`` is a terminal abort
                # signal; forward it instead of swallowing so the
                # workflow's top-level handler can route the audit
                # to a clean exit. Every other sink failure stays
                # best-effort.
                if _is_run_deleted_externally(exc):
                    raise
                log.warning("sink failed to write progress event: %s", exc)

    def write_resume_state(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            try:
                self.sink.write_resume_state(payload)
            except Exception as exc:  # noqa: BLE001 - resume-state is best-effort
                if _is_run_deleted_externally(exc):
                    raise
                # Failing to persist resume state means the operator
                # cannot ``audit resume`` this run, but the run itself
                # is not affected. Log loudly enough that operators see
                # it and can take action.
                log.warning("sink failed to write resume state: %s", exc)

    def close_run(self, final: AuditRun) -> None:
        with self._lock:
            self.sink.close_run(final)

    def fail_run(self, *, status: str = "failed", error: str = "") -> None:
        with self._lock:
            try:
                self.sink.fail_run(status=status, error=error)
            except Exception as exc:  # noqa: BLE001 - fail_run failure is non-fatal
                log.warning("sink failed fail_run: %s", exc)



class InMemoryReportSink:
    """Test double that satisfies ``ReportSink`` and records every call.

    Used by ``ApplicationServices.for_testing`` so unit / integration
    tests can drive the audit workflow end-to-end without a live
    Postgres. Tests that want to assert on sink writes can read
    ``self.calls``; tests that just want the audit to complete can
    ignore it. There is no on-disk side effect.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def open_run(self, meta: RunMeta) -> None:
        self.calls.append(("open_run", meta))

    def emit_snapshot(self, snapshot: AuditRun) -> None:
        self.calls.append(("emit_snapshot", snapshot))

    def upsert_finding(self, run_id: str, finding: Finding) -> None:
        self.calls.append(("upsert_finding", (run_id, finding)))

    def write_progress(self, event: ProgressEvent) -> None:
        self.calls.append(("write_progress", event))

    def write_resume_state(self, payload: Mapping[str, Any]) -> None:
        self.calls.append(("write_resume_state", dict(payload)))

    def close_run(self, final: AuditRun) -> None:
        self.calls.append(("close_run", final))

    def fail_run(self, *, status: str, error: str) -> None:
        self.calls.append(("fail_run", (status, error)))


__all__ = [
    "HEARTBEAT_KINDS",
    "InMemoryReportSink",
    "PROGRESS_STAGES",
    "ProgressEvent",
    "ReportSink",
    "ReportSinkBus",
    "RunMeta",
]
