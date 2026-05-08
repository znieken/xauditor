"""Streaming path-loader for the audit workflow.

Drives ``AuditGraphSource.iter_paths(page_size=batch_size)`` from a
single background thread, materialises each ``PathRecord`` into an
``AuditUnit`` (calling ``LLMClient.summarize_path`` to backfill
``business_context`` / ``trust_boundary`` when the graph row is
missing them — same body as the eager ``plan_audit_paths``), dedupes
by ``path_fingerprint``, and pushes the result onto a bounded
``queue.Queue`` of capacity ``batch_size``.

The audit workflow consumes one ``AuditUnit`` at a time via
``next_or_none``; the bounded queue is the natural backpressure that
hides Neo4j round-trip latency behind LLM work — see design D2 of
``audit-stream-path-loading``.

Errors raised by the fetcher are packaged into a sentinel and
re-raised on the consumer's next ``next_or_none`` call, matching the
``CoderDispatcher`` propagation pattern.
"""

from __future__ import annotations

import queue
import threading
import traceback
from dataclasses import dataclass
from typing import TYPE_CHECKING

from xauditor.models import AuditUnit, PathRecord

if TYPE_CHECKING:
    from xauditor.audit.source import AuditGraphSource
    from xauditor.llm import LLMClient


_END = object()


@dataclass(frozen=True)
class _FetcherError:
    exc: BaseException
    formatted: str


class PathStreamer:
    """Background fetcher + bounded queue feeding ``AuditUnit``s to the workflow."""

    def __init__(
        self,
        source: "AuditGraphSource",
        *,
        batch_size: int,
        llm_client: "LLMClient | None" = None,
        dedup: bool = True,
    ) -> None:
        if batch_size < 1:
            raise ValueError(f"batch_size must be >= 1, got {batch_size!r}")
        self._source = source
        self._batch_size = batch_size
        self._llm_client = llm_client
        self._dedup = dedup
        self._queue: queue.Queue = queue.Queue(maxsize=batch_size)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._total: int | None = None
        self._yielded = 0
        self._exhausted = False
        self._fetched = 0

    # ------------------------------------------------------------------ open

    def open(self) -> None:
        if self._thread is not None:
            raise RuntimeError("PathStreamer.open() called twice")
        self._total = self._source.path_count()
        self._thread = threading.Thread(
            target=self._run_fetcher,
            name="audit-path-streamer",
            daemon=True,
        )
        self._thread.start()

    # ------------------------------------------------------------------ pull

    def next_or_none(self) -> AuditUnit | None:
        """Return the next ``AuditUnit`` or ``None`` when the stream is drained.

        Blocks until an item is available, the streamer is exhausted, or
        the fetcher fails. A fetcher exception surfaces here on the
        consumer's thread.
        """

        if self._exhausted:
            return None
        item = self._queue.get()
        if item is _END:
            self._exhausted = True
            return None
        if isinstance(item, _FetcherError):
            self._exhausted = True
            raise item.exc
        self._yielded += 1
        return item

    # ----------------------------------------------------------------- close

    def close(self, *, wait: bool = True) -> None:
        """Stop the fetcher and (optionally) join.

        ``wait=True`` joins indefinitely — the caller is expected to
        bound the join with its own timeout (the workflow uses
        ``audit.shutdown_timeout_seconds``). ``wait=False`` signals the
        fetcher and returns immediately.
        """

        self._stop_event.set()
        # Drain the queue so the fetcher's blocking ``put`` unblocks
        # and observes the stop flag on the next loop iteration.
        try:
            while True:
                self._queue.get_nowait()
        except queue.Empty:
            pass
        if wait and self._thread is not None and self._thread.is_alive():
            self._thread.join()
        self._exhausted = True

    # ----------------------------------------------------------- properties

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def total(self) -> int:
        if self._total is None:
            raise RuntimeError("PathStreamer.total accessed before open()")
        return self._total

    @property
    def yielded(self) -> int:
        return self._yielded

    @property
    def fetched(self) -> int:
        return self._fetched

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    # --------------------------------------------------------------- fetcher

    def _run_fetcher(self) -> None:
        try:
            seen: set[str] = set()
            for path in self._source.iter_paths(page_size=self._batch_size):
                if self._stop_event.is_set():
                    return
                if self._dedup:
                    if path.path_fingerprint in seen:
                        continue
                    seen.add(path.path_fingerprint)
                unit = self._build_unit(path)
                self._fetched += 1
                while True:
                    if self._stop_event.is_set():
                        return
                    try:
                        self._queue.put(unit, timeout=0.1)
                        break
                    except queue.Full:
                        continue
        except BaseException as exc:  # noqa: BLE001 — surfacing every failure
            self._safe_put(_FetcherError(exc=exc, formatted=traceback.format_exc()))
            return
        self._safe_put(_END)

    def _build_unit(self, path: PathRecord) -> AuditUnit:
        business_context = path.business_context
        trust_boundary = path.trust_boundary
        if self._llm_client is not None and (not business_context or not trust_boundary):
            enrichment = self._llm_client.summarize_path(
                path.entry_function, path.function_names
            )
            business_context = business_context or enrichment["business_context"]
            trust_boundary = trust_boundary or enrichment["trust_boundary"]
        return AuditUnit(
            path=PathRecord(
                entry_function=path.entry_function,
                function_names=path.function_names,
                file_paths=path.file_paths,
                path_fingerprint=path.path_fingerprint,
                function_ids=path.function_ids,
                business_context=business_context,
                trust_boundary=trust_boundary,
            ),
            function_ids=path.function_ids,
        )

    def _safe_put(self, item: object) -> None:
        # Best-effort terminal sentinel; if the consumer has already
        # drained-and-bailed (close), the queue may be uninteresting,
        # but we still want to try once so a waiting ``get`` wakes up.
        while True:
            try:
                self._queue.put(item, timeout=0.1)
                return
            except queue.Full:
                if self._stop_event.is_set():
                    return
                continue


class EagerPathStreamer:
    """In-memory adapter exposing the ``PathStreamer`` consumer surface.

    Wraps a pre-materialised tuple of ``AuditUnit``s. Used by tests
    that construct an ``AuditPlan`` directly and by the legacy
    ``workflow.run(plan=...)`` entry, which passes the plan's
    ``audit_units`` tuple through this adapter so the sliding-window
    loop can treat eager and streamed inputs identically.
    """

    def __init__(self, units: tuple[AuditUnit, ...]) -> None:
        self._units = units
        self._cursor = 0

    def next_or_none(self) -> AuditUnit | None:
        if self._cursor >= len(self._units):
            return None
        unit = self._units[self._cursor]
        self._cursor += 1
        return unit

    def close(self, *, wait: bool = True) -> None:  # noqa: ARG002 - shape parity
        self._cursor = len(self._units)

    @property
    def batch_size(self) -> int:
        return len(self._units) or 1

    @property
    def total(self) -> int:
        return len(self._units)

    @property
    def yielded(self) -> int:
        return self._cursor

    @property
    def exhausted(self) -> bool:
        return self._cursor >= len(self._units)
