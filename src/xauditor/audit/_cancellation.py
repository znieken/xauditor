"""Run-level cancellation primitive shared across the audit pipeline.

A single ``RunCancellation`` instance is constructed at the start of an audit
run and passed to every long-running wait inside the workflow, teaming
subagent pools, coder dispatcher, and worker pool. Each wait polls the
embedded ``threading.Event`` (via the ``wait()`` / ``is_cancelled()`` /
``remaining_seconds()`` helpers) so a SIGINT handler can request a graceful
shutdown without that wait having to know the signalling mechanism.

The CLI installs the SIGINT handler. On the first SIGINT it calls
``set_cancelled()`` on this instance, which both sets the underlying event
and arms a wall-clock deadline. On the second SIGINT the handler bypasses
this object entirely and calls ``os._exit(130)`` — that escape hatch is the
caller's responsibility, not this module's.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic
from types import FrameType
from typing import TYPE_CHECKING, Iterator
from uuid import uuid4

if TYPE_CHECKING:
    from xauditor.runtime_logging import RuntimeLogger

__all__ = ["RunCancellation", "install_sigint_handler"]


# Event names emitted by the shutdown sequence; surfaced as constants so
# tests and operator dashboards can reference them without typos.
EVENT_SHUTDOWN_START = "audit.shutdown.start"
EVENT_SHUTDOWN_TEAMING_DONE = "audit.shutdown.teaming_done"
EVENT_SHUTDOWN_CODER_DONE = "audit.shutdown.coder_done"
EVENT_SHUTDOWN_WORKER_POOL_DONE = "audit.shutdown.worker_pool_done"
EVENT_SHUTDOWN_COMPLETE = "audit.shutdown.complete"
EVENT_SHUTDOWN_TIMEOUT = "audit.shutdown.timeout"
EVENT_SHUTDOWN_FORCE_EXIT = "audit.shutdown.force_exit"


@dataclass
class RunCancellation:
    """Cooperative cancellation token plus a wall-clock shutdown deadline.

    Pass a single instance into every long-running wait inside the audit
    pipeline. The CLI's SIGINT handler calls :py:meth:`set_cancelled` on
    the first interrupt; every wait observes the event in its next poll
    interval and exits cooperatively. After ``set_cancelled`` is invoked
    the deadline starts ticking — once :py:meth:`remaining_seconds`
    returns ``0.0``, callers SHOULD escalate from a graceful drain to a
    forced shutdown (``shutdown(wait=False)`` + SIGTERM / SIGKILL of any
    tracked subprocesses).
    """

    timeout_seconds: float
    """Total wall-clock budget from the moment ``set_cancelled`` is called
    until callers MUST escalate to a forced shutdown. Configured by
    ``audit.shutdown_timeout_seconds`` and validated upstream."""

    cancel_event: threading.Event = field(default_factory=threading.Event)
    _shutdown_deadline: float | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def set_cancelled(self) -> bool:
        """Set the cancellation event and arm the shutdown deadline.

        Returns ``True`` the first time it is called and ``False`` on
        every subsequent call. Callers can use the return value to
        distinguish the first SIGINT (set up shutdown bookkeeping) from
        repeat SIGINTs (escalate to ``os._exit``).
        """
        with self._lock:
            if self.cancel_event.is_set():
                return False
            self._shutdown_deadline = monotonic() + self.timeout_seconds
            self.cancel_event.set()
            return True

    def is_cancelled(self) -> bool:
        """Has cancellation been requested?"""
        return self.cancel_event.is_set()

    @property
    def shutdown_deadline(self) -> float | None:
        """Monotonic timestamp at which graceful shutdown SHALL be aborted.

        ``None`` until ``set_cancelled`` has been called. Once armed it is
        immutable.
        """
        return self._shutdown_deadline

    def remaining_seconds(self) -> float | None:
        """Seconds remaining before the shutdown deadline.

        Returns ``None`` if no deadline is armed (cancellation not yet
        requested). Returns ``0.0`` when the deadline has elapsed.
        Returns a positive float otherwise.
        """
        deadline = self._shutdown_deadline
        if deadline is None:
            return None
        remaining = deadline - monotonic()
        return remaining if remaining > 0.0 else 0.0

    def deadline_reached(self) -> bool:
        """Is the shutdown deadline reached or already past?"""
        remaining = self.remaining_seconds()
        return remaining is not None and remaining <= 0.0

    def wait(self, interval: float) -> bool:
        """Block for at most ``interval`` seconds OR until cancelled.

        Returns ``True`` when cancellation arrives during (or before) the
        wait, ``False`` if the interval elapses without cancellation.

        Use this in inner busy-poll loops in place of ``time.sleep`` so
        cancellation is observed within the next event-set rather than
        having to wait out the current poll interval.
        """
        return self.cancel_event.wait(interval)


@contextmanager
def install_sigint_handler(
    cancellation: "RunCancellation",
    *,
    logger: "RuntimeLogger | None" = None,
) -> Iterator[None]:
    """Install the audit SIGINT handler for the duration of the audit run.

    The first SIGINT sets the cancellation event, arms the shutdown
    deadline, and then chains to Python's default SIGINT handler so any
    in-flight wait that would otherwise raise ``KeyboardInterrupt``
    continues to do so — this preserves the existing
    ``except KeyboardInterrupt: ... raise UserCancelledError`` flow in
    ``audit/workflow.py`` without requiring every wait to poll the event
    explicitly.

    The second SIGINT logs ``audit.shutdown.force_exit`` and immediately
    calls ``os._exit(130)``, bypassing Python finalisers — that escape
    hatch exists precisely because the finalisers may be the things that
    hung in the first place.

    The handler is only installed when called from the main thread (the
    only thread Python permits ``signal.signal`` to run on); when invoked
    from a worker thread (e.g. inside a unit test) the context manager
    is a no-op so callers don't need to special-case it.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    cancellation_id = uuid4().hex[:12]

    def handler(signum: int, frame: FrameType | None) -> None:
        if not cancellation.set_cancelled():
            # Repeat SIGINT — operator wants out NOW.
            if logger is not None:
                try:
                    logger.warning_kv(
                        EVENT_SHUTDOWN_FORCE_EXIT,
                        cancel_event_id=cancellation_id,
                        signum=signum,
                    )
                except Exception:  # noqa: BLE001 - never let logging block exit
                    pass
            # Best-effort flush so the operator at least sees the line.
            try:
                sys.stderr.flush()
            except Exception:  # noqa: BLE001
                pass
            os._exit(130)
            return

        deadline_iso = (
            datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            if cancellation.shutdown_deadline is None
            else datetime.fromtimestamp(0, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
        )
        # The deadline is a monotonic timestamp; render the absolute UTC
        # wall-clock equivalent for operator-facing logs.
        try:
            from time import monotonic as _mono, time as _time

            if cancellation.shutdown_deadline is not None:
                wall = _time() + (cancellation.shutdown_deadline - _mono())
                deadline_iso = datetime.fromtimestamp(
                    wall, tz=timezone.utc
                ).isoformat(timespec="milliseconds")
        except Exception:  # noqa: BLE001 - logging-only computation
            pass

        if logger is not None:
            try:
                logger.info_kv(
                    EVENT_SHUTDOWN_START,
                    cancel_event_id=cancellation_id,
                    deadline=deadline_iso,
                    timeout_seconds=cancellation.timeout_seconds,
                )
            except Exception:  # noqa: BLE001
                pass

        # Chain to Python's default handler so the main thread receives
        # KeyboardInterrupt at its next interruption point. The existing
        # ``except KeyboardInterrupt`` in workflow.py then runs the
        # cancel bookkeeping.
        try:
            signal.default_int_handler(signum, frame)
        except KeyboardInterrupt:
            # Re-raise so the main thread sees it; the signal handler
            # context catches and propagates.
            raise

    prior = signal.signal(signal.SIGINT, handler)
    try:
        yield
    finally:
        try:
            signal.signal(signal.SIGINT, prior)
        except (ValueError, OSError):
            pass
