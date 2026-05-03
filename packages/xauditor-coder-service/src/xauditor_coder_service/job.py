"""In-memory job state for the coder microservice.

Per design D3 in ``add-coder-http-microservice``: jobs live in a process
local ``dict``; service restart drops every in-flight and historical job.
A periodic reaper task evicts terminal jobs after a TTL so the dict does
not grow unbounded across long-running deployments.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic
from typing import Any


# Status enum mirrored from the spec. Strings (not Python `enum`) because
# they appear directly in JSON responses and the service has no need to
# validate against a closed set internally.
STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = (STATUS_DONE, STATUS_ERROR, STATUS_CANCELLED)


class IdempotencyProjectConflict(Exception):
    """Raised by ``JobStore.submit`` when a known idempotency key is
    re-submitted with a project name different from the one the job was
    originally created against. The handler maps this to HTTP 409.
    """

    def __init__(self, *, existing_project: str, requested_project: str) -> None:
        super().__init__(
            f"idempotency_key bound to project {existing_project!r}; "
            f"got {requested_project!r}"
        )
        self.existing_project = existing_project
        self.requested_project = requested_project


@dataclass
class JobState:
    job_id: str
    idempotency_key: str
    project: str = ""
    status: str = STATUS_PENDING
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    # Cancellation channel: the worker checks this before each step and on
    # SIGTERM. The DELETE handler sets it.
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Process handle for in-flight Claude Code subprocess. The worker
    # populates this so DELETE can SIGTERM/SIGKILL it directly.
    process: Any = None
    # `monotonic()` time the job entered a terminal status, used by the
    # TTL reaper.
    terminal_at: float | None = None

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_response_dict(self) -> dict[str, Any]:
        """Serialise to the wire shape the spec defines for GET /verifications/{id}."""

        return {
            "job_id": self.job_id,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at.isoformat(),
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
        }


class JobStore:
    """Thread-safe in-memory job dictionary with idempotency + TTL reaping.

    The dict is consulted on every request, so we hold a single
    ``threading.Lock`` (NOT an asyncio lock — the lock is held only for
    short operations, and FastAPI dependencies that read the store may
    run on the event loop or on a thread pool depending on the route's
    declaration).
    """

    def __init__(self, *, terminal_ttl_seconds: float = 3600.0) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, JobState] = {}
        self._by_idempotency: dict[str, str] = {}  # key -> job_id
        self.terminal_ttl_seconds = terminal_ttl_seconds

    # submission --------------------------------------------------------------

    def submit(
        self, idempotency_key: str, *, project: str = ""
    ) -> tuple[JobState, bool]:
        """Return (job, created_new).

        - Existing pending / done job for the same key + same project →
          return existing, ``created_new=False`` (idempotent).
        - Existing pending / done job for the same key but a DIFFERENT
          project → raise :class:`IdempotencyProjectConflict` so the
          handler returns HTTP 409. Idempotency keys are per-project.
        - Existing terminal-but-failed (error / cancelled) job → discard
          the old one and create a fresh job with a new id (and the
          new project, even if it differs).
        - No prior key → create a new job.
        """

        with self._lock:
            existing_id = self._by_idempotency.get(idempotency_key)
            if existing_id is not None:
                existing = self._jobs.get(existing_id)
                if existing is not None and existing.status in (
                    STATUS_PENDING,
                    STATUS_DONE,
                ):
                    if (existing.project or "") != (project or ""):
                        raise IdempotencyProjectConflict(
                            existing_project=existing.project or "",
                            requested_project=project or "",
                        )
                    return existing, False
                # error / cancelled → drop and re-create
                if existing is not None:
                    self._jobs.pop(existing_id, None)
                self._by_idempotency.pop(idempotency_key, None)
            job_id = str(uuid.uuid4())
            job = JobState(
                job_id=job_id, idempotency_key=idempotency_key, project=project
            )
            self._jobs[job_id] = job
            self._by_idempotency[idempotency_key] = job_id
            return job, True

    # lookup ------------------------------------------------------------------

    def get(self, job_id: str) -> JobState | None:
        with self._lock:
            return self._jobs.get(job_id)

    def in_flight(self) -> int:
        """Count of non-terminal jobs (pending OR running). Used by /health."""

        with self._lock:
            return sum(
                1 for job in self._jobs.values() if not job.is_terminal()
            )

    # transitions -------------------------------------------------------------

    def mark_terminal(
        self,
        job_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> JobState | None:
        """Atomically flip the job to a terminal status. Idempotent."""

        if status not in TERMINAL_STATUSES:
            raise ValueError(f"mark_terminal called with non-terminal status: {status}")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.is_terminal():
                return job
            job.status = status
            job.result = result
            job.error = error
            job.completed_at = datetime.now(timezone.utc)
            job.terminal_at = monotonic()
            return job

    # cancellation ------------------------------------------------------------

    def request_cancel(self, job_id: str) -> JobState | None:
        """Set the job's ``cancel_event``. Returns the job (or None)."""

        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            return None
        # Set the event outside the lock — it's an asyncio.Event whose
        # setter doesn't block.
        job.cancel_event.set()
        return job

    # reaper ------------------------------------------------------------------

    def reap_terminal(self, *, now: float | None = None) -> list[str]:
        """Remove terminal jobs older than ``terminal_ttl_seconds``.

        Returns the list of job_ids reaped. Safe to call from a periodic
        background task.
        """

        cutoff = (now if now is not None else monotonic()) - self.terminal_ttl_seconds
        reaped: list[str] = []
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if not job.is_terminal():
                    continue
                if job.terminal_at is None:
                    continue
                if job.terminal_at < cutoff:
                    self._jobs.pop(job_id, None)
                    self._by_idempotency.pop(job.idempotency_key, None)
                    reaped.append(job_id)
        return reaped


__all__ = [
    "JobState",
    "JobStore",
    "STATUS_CANCELLED",
    "STATUS_DONE",
    "STATUS_ERROR",
    "STATUS_PENDING",
    "TERMINAL_STATUSES",
]
