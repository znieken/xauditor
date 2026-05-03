"""WorkerPool — pluggable transport for per-path agent execution.

Two implementations ship today:

- ``InlineExecutor`` (used when ``audit.worker_count == 1``) runs
  the runner synchronously on master's thread.
- ``LocalSubprocessPool`` (used when ``audit.worker_count >= 2``)
  spawns N subprocess workers via ``multiprocessing.spawn`` and
  routes paths over a queue. Each subprocess processes one path at
  a time in a persistent loop; spawn cost is paid once per pool
  lifetime.

A future change (``distribute-audit-workers-across-hosts``) is
expected to add ``RemoteQueuePool``. All implementations honor the
same ``WorkerPool`` Protocol so the audit workflow's main loop is
implementation-agnostic.

Design contract — see
``openspec/changes/parallelize-audit-path-execution/design.md`` for
the original Protocol-seam rationale (D1), deterministic-ordering
contract (D9), pickle-friendly payload (D10), and TM1–TM5+TM4b
thread-model invariants. The 0.8.0 simplification
(``flatten-path-concurrency-into-worker-count``) removed the
internal thread pool inside subprocess workers; the 0.10.0
simplification (``consolidate-on-worker-count``) removed
``LocalThreadPool`` entirely (replaced by ``InlineExecutor``)
and dropped ``audit.path_concurrency``.

Quick summary:

- ``submit_path(unit, ctx)`` schedules one path's agent chain for
  execution on whichever transport the pool wraps and returns a
  ``Future[PathResult]``.
- ``cancel_all()`` is best-effort cancellation: drops unstarted work
  and signals running work to bail at the next stage boundary.
- ``shutdown(wait=...)`` releases the pool's resources.

The aggregator on the master thread consumes ``PathResult`` payloads
in plan order, holds the single-writer pen for shared mutable state
(``findings``, ``checkpoints``, ``shared_state``, …), and dispatches
the coder stage on the main side. Per-path workers do not touch
master-side state directly — that's TM5 from the spec.
"""

from __future__ import annotations

import multiprocessing as mp
import queue as _queue
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from xauditor.audit._cancellation import RunCancellation


class PathOutcome(str, Enum):
    """How a path's agent chain terminated."""

    COMPLETED = "completed"
    CONTEXT_SKIPPED = "context_skipped"
    LLM_FAILED = "llm_failed"
    CANCELLED = "cancelled"
    WORKER_CRASHED = "worker_crashed"


@dataclass(frozen=True)
class PathResult:
    """Per-path agent-chain result returned to the master aggregator.

    Designed to be pickle-friendly: every field is either a stdlib
    primitive, a frozen dataclass (``AuditUnit``, ``AnalyzerResult``,
    etc. — all already frozen + stdlib-typed), a tuple of those, or
    a dict of strings → primitives. ``LocalSubprocessPool`` (Phase 3)
    pickles this across ``multiprocessing.spawn``-spawned workers;
    ``RemoteQueuePool`` (option C) JSONB-encodes it for the Postgres
    queue.

    The aggregator uses ``index`` for plan-order finding-id
    assignment (D9) and ``outcome`` to route the per-path side
    effects:
    - ``COMPLETED`` → build Findings, dispatch coder, append to
      ``findings`` / ``shared_state`` / ``checkpoints``.
    - ``CONTEXT_SKIPPED`` / ``LLM_FAILED`` / ``WORKER_CRASHED`` →
      record into ``failed_paths`` or ``context_skipped_paths``
      with the failure block in ``shared_state[fp]``.
    - ``CANCELLED`` → skip silently; the cancellation handler at
      the run level surfaces the reason.
    """

    index: int
    """1-based plan-order index. Matches the ``for index, … in
    enumerate(plan.audit_units, start=1)`` convention used in the
    serial workflow."""

    path_fingerprint: str
    """Mirror of ``unit.path.path_fingerprint`` for the aggregator's
    convenience — saves repeated attribute lookups in the hot
    aggregation loop."""

    outcome: PathOutcome
    """How the agent chain terminated. Routes the aggregator's
    side-effect dispatch."""

    # Set when ``outcome == COMPLETED`` (or partially completed and
    # has a checkpoint to record). Empty otherwise.
    unit: Any | None = None
    """Enriched ``AuditUnit`` returned from ``_enrich_unit``. Carries
    the LLM-summarised business_context / trust_boundary if the
    enrichment ran."""

    path_functions: tuple = ()
    """``tuple[FunctionRecord, ...]`` — the resolved function
    definitions on this path. Needed by the aggregator for
    ``_build_finding``."""

    path_context: dict[str, object] = field(default_factory=dict)
    """``build_path_context`` output — needed by ``_dispatch_coder``
    for the coder payload."""

    path_shared_state: dict[str, object] = field(default_factory=dict)
    """Per-stage agent payloads (``analyzer`` / ``exploitation`` /
    ``validator`` / teaming subagent records) keyed by stage. The
    aggregator stores this under
    ``run.shared_state[path_fingerprint]``."""

    checkpoint_status: str = ""
    """One of: ``"no_finding"``, ``"Valid"``, ``"Partial Valid"``,
    ``"Inconclusive"``, ``"False Positive"``,
    ``"skipped_context_window"``, ``"failed_llm_error"``,
    ``"resumed"``."""

    per_finding: tuple = ()
    """``tuple[tuple[AnalyzerResult, ExploitationResult | None,
    ValidationResult | None, bool], ...]`` — one entry per
    candidate finding the analyzer team reported. The aggregator
    iterates and calls ``_build_finding`` per entry."""

    error_info: dict[str, str] | None = None
    """For ``CONTEXT_SKIPPED`` / ``LLM_FAILED`` /
    ``WORKER_CRASHED`` outcomes: the failure metadata
    (``operation``, ``provider``, ``model``, ``cause``). Mirrors
    the ``shared_state[fp]["skipped"|"failed"]`` block the serial
    workflow already writes."""


@runtime_checkable
class WorkerPool(Protocol):
    """Pluggable transport for per-path agent execution.

    Implementations (since 0.10.0,
    ``consolidate-on-worker-count``):

    - ``InlineExecutor`` — used when ``audit.worker_count == 1``.
      Runs the runner synchronously on the master thread.
      No threads, no subprocess, no queue.
    - ``LocalSubprocessPool`` — used when
      ``audit.worker_count >= 2``. Spawns N subprocess workers via
      ``multiprocessing.spawn``; each processes one path at a time
      in a persistent loop. Host-wide in-flight = ``worker_count``.
    - ``RemoteQueuePool`` (option C) — Postgres ``audit_jobs``
      table with SKIP-LOCKED claim semantics.

    All implementations honor the same ordering / cancellation /
    shutdown contract so the aggregator's loop is identical
    regardless of the underlying transport.
    """

    def submit_path(
        self,
        runner: Callable[[int, Any], PathResult],
        index: int,
        unit: Any,
    ) -> "Future[PathResult]":
        """Schedule a single path for execution.

        ``runner`` is the per-path callable (typically the workflow's
        ``_process_one_path`` bound method). ``index`` is the
        1-based plan-order index. ``unit`` is the
        ``AuditUnit`` to process.

        Returns a ``Future`` that resolves to a ``PathResult`` (or
        raises ``WorkerCrashedError`` for subprocess crashes —
        ``LocalSubprocessPool`` only).
        """
        ...

    def cancel_all(self) -> None:
        """Best-effort cancellation. Drops unstarted work; signals
        running work to bail at next stage boundary."""
        ...

    def shutdown(self, *, wait: bool = True) -> None:
        """Release pool resources. ``wait=True`` blocks until in-flight
        work completes; ``wait=False`` returns immediately."""
        ...


class WorkerCrashedError(Exception):
    """Raised by ``LocalSubprocessPool`` when a subprocess worker
    exits abnormally during a path's execution. The aggregator
    catches this and marks the affected path ``WORKER_CRASHED``.

    Attributes:
        path_index: 1-based plan-order index of the affected path.
        exit_code: subprocess exit code (negative = killed by signal).
        worker_id: ``"<hostname>/<pid>"`` for log attribution.
    """

    def __init__(
        self,
        *,
        path_index: int,
        exit_code: int,
        worker_id: str,
    ) -> None:
        super().__init__(
            f"audit-worker {worker_id} crashed during path {path_index} "
            f"(exit code {exit_code})"
        )
        self.path_index = path_index
        self.exit_code = exit_code
        self.worker_id = worker_id


class InlineExecutor:
    """Inline (synchronous) ``WorkerPool`` for ``audit.worker_count == 1``.

    ``submit_path(runner, index, unit)`` calls ``runner(index, unit)``
    on the master thread synchronously and returns a ``Future`` whose
    result is already set (or whose exception is already set when the
    runner raised). No threads, no subprocess, no queue, no executor.

    Cancellation: the runner runs on master's thread, so a
    ``KeyboardInterrupt`` propagates naturally from inside
    ``_process_one_path``. The workflow's ``_cancelling`` flag plus
    its ``on_cancel`` hook handle the rest. ``cancel_all()`` and
    ``shutdown()`` are no-ops.

    See ``consolidate-on-worker-count`` design D1: this replaces the
    pre-0.10.0 ``LocalThreadPool`` (in-process thread pool of size
    ``audit.path_concurrency``); the latter is gone now that the
    single concurrency knob is ``worker_count``.
    """

    def submit_path(
        self,
        runner: Callable[[int, Any], PathResult],
        index: int,
        unit: Any,
    ) -> "Future[PathResult]":
        future: "Future[PathResult]" = Future()
        try:
            future.set_result(runner(index, unit))
        except BaseException as exc:  # noqa: BLE001 - mirror future contract
            future.set_exception(exc)
        return future

    def cancel_all(self) -> None:
        # No queued or in-flight work to cancel — runs synchronously
        # on the calling thread. The workflow's ``_cancelling`` flag
        # is what stops the runner mid-stage.
        return None

    def shutdown(self, *, wait: bool = True) -> None:
        del wait
        return None


def _subprocess_worker_main(
    *,
    request_queue: "mp.Queue",
    result_queue: "mp.Queue",
    config: Any,
    source_descriptor: Any,
    worker_id: str,
) -> None:
    """Subprocess audit worker entry point.

    Spawned by ``LocalSubprocessPool`` via ``multiprocessing.spawn``.
    Reconstructs a coder-less ``AuditWorkflow`` and an
    ``AuditGraphSource`` (from the supplied descriptor) on entry,
    then pulls path-execution requests from ``request_queue`` one at
    a time and pushes ``("result", PathResult, worker_id)`` tuples
    back to ``result_queue``.

    Sequential per-worker (0.8.0): each subprocess processes one
    path synchronously in its own main thread before dequeuing the
    next request. The host-wide peak in-flight path count is
    therefore ``worker_count`` (no internal thread pool, no nested
    concurrency). Pre-0.8.0 each worker ran its own
    ``LocalThreadPool``; that layer was removed by
    ``flatten-path-concurrency-into-worker-count`` (D3).

    Heartbeats: subprocess workers do NOT emit heartbeats
    (the master aggregator emits per integrated PathResult).

    Coder dispatch: subprocess workers DO NOT dispatch the coder
    stage — they only run analyzer/exploitation/validator and
    return the per-finding tuple to the master, which dispatches
    coder centrally so there's exactly one ``CoderDispatcher`` per
    audit run regardless of how many subprocess workers exist.

    Source reconstruction: ``rebuild_source(source_descriptor)``
    builds a fresh ``Neo4jAuditGraphSource`` from the descriptor's
    connection params (the host-wide Neo4j connection count is
    therefore ``worker_count + 1``).

    Per-path claim protocol: before each path execution, the worker
    pushes ``("claim", index, worker_id)`` to the result queue so
    master can route orphaned futures to ``WORKER_CRASHED`` if the
    worker dies before pushing the path's result. A worker holds at
    most one in-flight claim at any instant (D2).

    Sentinel: ``request_queue.get()`` returning ``None`` triggers
    clean exit.
    """
    # Lazy imports — keeps master pool construction lightweight.
    from xauditor.audit.workflow import AuditWorkflow
    from xauditor.audit.source import rebuild_source
    from xauditor.runtime_logging import RuntimeLogger

    # Each subprocess audit worker gets its own ``RuntimeLogger``
    # tagged with ``worker_id`` so the operator can tell
    # interleaved multi-worker output apart in master's stderr
    # (subprocess stderr is inherited by ``multiprocessing.spawn``)
    # and in the shared log_file. Without this, workers would
    # silently drop every structured log call because
    # ``AuditWorkflow.from_config(..., logger=None)`` leaves
    # ``self.logger`` as ``None`` and every emit site short-
    # circuits behind an ``if self.logger is not None`` guard.
    worker_logger = RuntimeLogger.from_config(config, tag=worker_id)
    workflow = AuditWorkflow.from_config(
        config, logger=worker_logger, with_coder=False
    )
    source = rebuild_source(source_descriptor)

    try:
        while True:
            try:
                msg = request_queue.get(timeout=0.5)
            except _queue.Empty:
                continue
            if msg is None:
                break
            kind, index, unit = msg
            if kind != "path":
                continue

            # Tell master "I own this index now" BEFORE we start
            # running the path. If we crash any time after this
            # point and before the matching ``("result", ...)``
            # arrives, master's monitor thread uses this claim to
            # route the orphaned future to
            # ``PathOutcome.WORKER_CRASHED`` instead of hanging on
            # ``future.result()`` forever.
            try:
                result_queue.put(("claim", index, worker_id))
            except Exception:  # noqa: BLE001 - queue may be closed
                pass

            # Synchronous, one-at-a-time execution. ``total_units``
            # isn't known per-task in subprocess context (only
            # master knows the plan size); 0 means "unknown" and is
            # fine because subprocess workers don't emit heartbeats.
            try:
                result = workflow._process_one_path(
                    candidate_unit=unit,
                    index=index,
                    total_units=0,
                    source=source,
                )
            except BaseException:  # noqa: BLE001 - re-raise after shutdown
                # Let the exception terminate this worker; master
                # will detect the dead process and route the orphan
                # claim to WORKER_CRASHED. Don't catch-and-continue
                # here because we don't know if the workflow state
                # is still consistent.
                raise

            # Result message carries worker_id so master can pop the
            # claim entry by key in O(1) without scanning. See D2.
            try:
                result_queue.put(("result", result, worker_id))
            except Exception:  # noqa: BLE001 - queue may be closed during shutdown
                pass
    finally:
        # Release source-side resources (Neo4j BoltDriver socket,
        # etc.) before the subprocess terminates; in-memory sources
        # treat this as a no-op.
        try:
            source.close()
        except Exception:  # noqa: BLE001 - shutdown best-effort
            pass
        try:
            result_queue.close()
            result_queue.join_thread()
        except Exception:  # noqa: BLE001 - shutdown best-effort
            pass


class LocalSubprocessPool:
    """Subprocess-backed worker pool.

    Spawns ``worker_count`` subprocess workers via
    ``multiprocessing.spawn`` (NOT ``fork`` — clean Python interpreter
    per worker, no inherited file descriptors / locks). Each worker
    processes paths sequentially — one path in flight at any instant
    (0.8.0; pre-0.8.0 workers ran their own internal thread pool,
    removed by ``flatten-path-concurrency-into-worker-count``).
    Used when ``audit.worker_count >= 2``.

    Master process is a pure orchestrator: it submits paths via the
    request queue, fans incoming PathResults out to per-index
    Futures via a monitor thread, dispatches coder centrally, and
    aggregates findings. The master itself does NOT run the agent
    pipeline.

    Pickling contract: workers receive ``config`` (XAuditorConfig,
    frozen dataclass tree of primitives) and a ``source_descriptor``
    (frozen-dataclass union, see ``audit.source``) at construction.
    AuditUnits are pickled per-task. PathResults are pickled back.
    All payload types must remain pickle-friendly — see PathResult
    docstring.

    Source compatibility: 0.9.0 ``consolidate-on-neo4j-source`` —
    ``Neo4jAuditGraphSource`` is the only production source. Master
    calls ``source.pool_descriptor()`` (returns
    ``Neo4jSourceDescriptor`` — ~200 bytes regardless of graph
    size); workers call ``rebuild_source(descriptor)`` on their end.
    Worker-side memory is bounded by the Bolt session buffers, NOT
    the graph size. The pre-0.9.0 in-memory descriptor +
    ``_check_in_memory_descriptor_budget`` WARN are gone — the failure
    mode they protected against (linear graph copies per worker) is
    architecturally impossible now.
    """

    def __init__(
        self,
        *,
        worker_count: int,
        config: Any,
        source_descriptor: Any,
        logger: Any = None,
    ) -> None:
        if worker_count < 2:
            raise ValueError(
                f"LocalSubprocessPool: worker_count must be >= 2, "
                f"got {worker_count} (use InlineExecutor for 1)"
            )
        if source_descriptor is None:
            raise ValueError(
                "LocalSubprocessPool: source_descriptor is required so "
                "subprocess workers can rebuild their AuditGraphSource. "
                "Call source.pool_descriptor() on the master and pass "
                "the result here."
            )
        del logger  # logger no longer used at construction time
        self._worker_count = worker_count
        self._ctx = mp.get_context("spawn")
        self._request_q: "mp.Queue" = self._ctx.Queue()
        self._result_q: "mp.Queue" = self._ctx.Queue()
        self._futures: dict[int, "Future[PathResult]"] = {}
        # ``_claims`` tracks "which path-index does each worker own
        # right now". Keyed by worker_id (0.8.0; pre-0.8.0 was the
        # inverse). A worker holds at most one in-flight claim,
        # so ``len(_claims) <= worker_count`` always. Reuses
        # ``_futures_lock`` for single-writer access.
        self._claims: dict[str, int] = {}
        self._futures_lock = threading.Lock()
        # Workers we've already routed orphans for, so the same dead
        # worker isn't scanned twice across monitor ticks.
        self._dead_workers_seen: set[str] = set()
        self._closed = threading.Event()
        self._shutdown_called = False
        # Run-level cancellation token — injected by ``AuditWorkflow``
        # via :py:meth:`set_cancellation`. ``None`` in unit-test paths
        # that don't go through the CLI; ``shutdown`` then keeps its
        # original 10 s graceful join window.
        self._cancellation: "RunCancellation | None" = None

        self._workers: list[mp.Process] = []
        for i in range(worker_count):
            worker_name = f"audit-worker-{i}"
            p = self._ctx.Process(
                target=_subprocess_worker_main,
                kwargs={
                    "request_queue": self._request_q,
                    "result_queue": self._result_q,
                    "config": config,
                    "source_descriptor": source_descriptor,
                    "worker_id": worker_name,
                },
                name=worker_name,
                daemon=False,
            )
            p.start()
            self._workers.append(p)

        self._monitor = threading.Thread(
            target=self._monitor_loop,
            name="audit-result-monitor",
            daemon=True,
        )
        self._monitor.start()

    @property
    def worker_count(self) -> int:
        return self._worker_count

    def submit_path(
        self,
        runner: Callable[[int, Any], PathResult],
        index: int,
        unit: Any,
    ) -> "Future[PathResult]":
        # ``runner`` is intentionally ignored: subprocess workers
        # cannot pickle a master-side bound method, so they
        # reconstruct the workflow from the ``config`` they received
        # at startup and run their own ``_process_one_path``. The
        # ``runner`` parameter exists only so this transport
        # satisfies the same Protocol shape as ``InlineExecutor``.
        del runner
        future: "Future[PathResult]" = Future()
        with self._futures_lock:
            self._futures[index] = future
        self._request_q.put(("path", index, unit))
        return future

    def cancel_all(self) -> None:
        # SIGTERM every live worker. Anything they had in flight
        # (including LLM calls in progress) gets interrupted; the
        # monitor thread's "all workers dead" detector will fail
        # outstanding futures with WORKER_CRASHED.
        for p in self._workers:
            if p.is_alive():
                p.terminate()

    def shutdown(self, *, wait: bool = True) -> None:
        if self._shutdown_called:
            return
        self._shutdown_called = True
        # Send a sentinel (None) per worker so they exit cleanly.
        for _ in self._workers:
            try:
                self._request_q.put(None)
            except Exception:  # noqa: BLE001 - queue may already be closed
                pass
        if wait:
            cancellation = self._cancellation
            for p in self._workers:
                # If run-level cancellation is set or its deadline has
                # elapsed, jump straight to SIGTERM rather than burning
                # the 10 s graceful budget per worker. Operators who
                # asked to bail out should not have to wait
                # ``10 s × worker_count`` for the per-worker grace
                # before the escalation kicks in.
                if cancellation is not None and (
                    cancellation.is_cancelled() or cancellation.deadline_reached()
                ):
                    grace = 0.5
                else:
                    grace = 10.0
                p.join(timeout=grace)
                if p.is_alive():
                    # Worker didn't honor sentinel within grace —
                    # escalate to SIGTERM, then SIGKILL.
                    p.terminate()
                    p.join(timeout=2.0)
                    if p.is_alive():
                        p.kill()
                        p.join(timeout=1.0)
        self._closed.set()
        self._monitor.join(timeout=2.0)
        try:
            self._request_q.close()
            self._request_q.join_thread()
        except Exception:  # noqa: BLE001 - shutdown best-effort
            pass

    def set_cancellation(self, cancellation: "RunCancellation | None") -> None:
        """Inject the run-level cancellation token; called by ``AuditWorkflow``.

        When set, :py:meth:`shutdown` short-circuits the per-worker
        graceful-join window from 10 s to 0.5 s once the cancel event
        is observed — operators don't wait ``10 s × worker_count`` for
        SIGTERM escalation after asking to bail out.
        """
        self._cancellation = cancellation

    def _monitor_loop(self) -> None:
        # Drain ``result_q`` and fan PathResults out to per-index
        # Futures. The queue carries two message kinds:
        #
        #   ("claim", index, worker_id) — pushed by a worker right
        #       after dequeuing a path message and before invoking
        #       ``_process_one_path``. Master records "worker_id is
        #       processing this index" so it can route the future
        #       to ``WORKER_CRASHED`` if the worker dies before
        #       pushing a matching ``result``.
        #
        #   ("result", PathResult, worker_id) — the path's outcome.
        #       Master pops both the future and the worker's claim,
        #       then resolves the future with the result. The
        #       worker_id field lets master pop the claim entry by
        #       key in O(1) (D2).
        #
        # On every empty-queue tick we also scan for individual
        # workers that have died since the last tick; orphaned
        # claims route to ``WORKER_CRASHED`` so the master never
        # hangs on ``future.result()``. The "all workers dead"
        # check stays as a fallback for the rare case where every
        # worker exits between two ticks.
        while not self._closed.is_set():
            try:
                msg = self._result_q.get(timeout=0.5)
            except _queue.Empty:
                self._check_dead_workers()
                if self._all_workers_dead() and self._has_outstanding():
                    self._fail_outstanding(
                        "all subprocess workers exited"
                    )
                    break
                continue
            if msg is None:
                break
            try:
                kind = msg[0]
            except (TypeError, IndexError):
                continue
            if kind == "claim":
                _, claim_index, claim_worker_id = msg
                with self._futures_lock:
                    self._claims[claim_worker_id] = claim_index
            elif kind == "result":
                # Result message shape (0.8.0): ("result",
                # PathResult, worker_id). Pre-0.8.0 was
                # ("result", PathResult); accept both shapes for
                # one release of forward-compat just in case a
                # rolling upgrade ships mismatched master/worker
                # binaries (shouldn't happen; the wheel ships both
                # together, but the conditional is cheap).
                if len(msg) >= 3:
                    _, result, result_worker_id = msg[0], msg[1], msg[2]
                else:
                    _, result = msg[0], msg[1]
                    result_worker_id = None
                with self._futures_lock:
                    future = self._futures.pop(result.index, None)
                    if result_worker_id is not None:
                        self._claims.pop(result_worker_id, None)
                    else:
                        # Fallback: scan claims for the matching
                        # path index (cost O(worker_count)).
                        for wid, idx in list(self._claims.items()):
                            if idx == result.index:
                                self._claims.pop(wid, None)
                                break
                if future is not None and not future.done():
                    future.set_result(result)

    def _all_workers_dead(self) -> bool:
        return all(not p.is_alive() for p in self._workers)

    def _has_outstanding(self) -> bool:
        with self._futures_lock:
            return bool(self._futures)

    def _check_dead_workers(self) -> None:
        # For every worker that transitioned from alive → dead since
        # the last tick, look up its (at-most-one) in-flight claim
        # and resolve that future as ``WORKER_CRASHED``. With the
        # 0.8.0 claim-dict shape (worker_id → path_index) this is
        # O(1) per dead worker — no linear scan over the dict (D2).
        # ``_dead_workers_seen`` keeps the call idempotent across
        # monitor ticks.
        for worker in self._workers:
            if worker.is_alive():
                continue
            if worker.name in self._dead_workers_seen:
                continue
            self._dead_workers_seen.add(worker.name)
            with self._futures_lock:
                orphan_index = self._claims.pop(worker.name, None)
                future = (
                    self._futures.pop(orphan_index, None)
                    if orphan_index is not None
                    else None
                )
            if orphan_index is None or future is None or future.done():
                continue
            exit_code = worker.exitcode
            future.set_result(
                PathResult(
                    index=orphan_index,
                    path_fingerprint="",
                    outcome=PathOutcome.WORKER_CRASHED,
                    error_info={
                        "reason": (
                            f"worker {worker.name} died with this "
                            f"path in flight (exit code {exit_code})"
                        ),
                        "worker_id": worker.name,
                        "exit_code": str(exit_code),
                    },
                )
            )

    def _fail_outstanding(self, reason: str) -> None:
        with self._futures_lock:
            outstanding = list(self._futures.items())
            self._futures.clear()
            self._claims.clear()
        for idx, fut in outstanding:
            if not fut.done():
                fut.set_result(
                    PathResult(
                        index=idx,
                        path_fingerprint="",
                        outcome=PathOutcome.WORKER_CRASHED,
                        error_info={"reason": reason},
                    )
                )


def build_worker_pool(
    *,
    worker_count: int,
    config: Any = None,
    source_descriptor: Any = None,
    logger: Any = None,
) -> WorkerPool:
    """Factory that picks the right ``WorkerPool`` by ``worker_count``.

    - ``worker_count == 1`` → ``InlineExecutor`` (synchronous, on
      master's thread). ``config`` and ``source_descriptor`` are
      ignored.
    - ``worker_count >= 2`` → ``LocalSubprocessPool``. Requires both
      ``config`` (``XAuditorConfig`` for workers to rebuild their own
      workflow) and ``source_descriptor`` (an
      ``AuditGraphSourceDescriptor`` from
      ``source.pool_descriptor()`` so workers can call
      ``rebuild_source(descriptor)``). ``ValueError`` raised when
      either is missing.
    - ``logger`` is forwarded to ``LocalSubprocessPool``; ignored for
      ``InlineExecutor``.
    """

    if worker_count == 1:
        return InlineExecutor()
    if config is None or source_descriptor is None:
        raise ValueError(
            "LocalSubprocessPool (worker_count >= 2) requires both `config` "
            "and `source_descriptor` so subprocess workers can rebuild "
            "their workflow + graph source. Pass them via "
            "build_worker_pool(...)."
        )
    return LocalSubprocessPool(
        worker_count=worker_count,
        config=config,
        source_descriptor=source_descriptor,
        logger=logger,
    )


__all__ = [
    "InlineExecutor",
    "LocalSubprocessPool",
    "PathOutcome",
    "PathResult",
    "WorkerCrashedError",
    "WorkerPool",
    "build_worker_pool",
]
