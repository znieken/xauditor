from __future__ import annotations

import threading
from concurrent.futures import Future
from dataclasses import replace
from typing import Callable

from xauditor.config import XAuditorConfig
from xauditor.audit.agents import (
    AnalyzerAgent,
    AnalyzerResult,
    AnalyzerTeam,
    DebateTranscript,
    ExploitationAgent,
    ExploitationResult,
    ExploiterTeam,
    ValidationResult,
    ValidatorAgent,
    ValidatorTeam,
)
from xauditor.audit._cancellation import RunCancellation
from xauditor.audit.coder import CoderDispatcher, CoderResult, render_coder_payload
from xauditor.audit.context import build_path_context
from xauditor.audit.source import AuditGraphSource
from xauditor.audit.worker_pool import (
    PathOutcome,
    PathResult,
    WorkerPool,
    build_worker_pool,
)
from xauditor.errors import ContextWindowExceededError, LLMError, UserCancelledError
from xauditor.llm import LLMClient
from xauditor.model_factory import SamplingParams, resolve_chat_model
from xauditor.models import (
    CODER_STATUS_PENDING,
    CODER_STATUS_SKIPPED,
    AuditPlan,
    AuditRun,
    AuditUnit,
    ConfidenceLevel,
    Finding,
    PathRecord,
    SourceReference,
    ValidationStatus,
)
from xauditor.reporting.sinks import ProgressEvent, _is_run_deleted_externally
from xauditor.reporting.snippets import language_for_path, render_source_snippet
from xauditor.runtime_logging import RuntimeLogger


HeartbeatCallback = Callable[[ProgressEvent], None]


def _shutdown_pool_with_deadline(
    pool,
    *,
    cancellation: "RunCancellation | None",
    logger: "RuntimeLogger | None",
    stage_name: str,
) -> None:
    """Shut down ``pool`` with a wall-clock deadline + cancel-event short-circuit.

    Runs ``pool.shutdown(wait=True, cancel_futures=True)`` on a worker
    thread and polls every 0.5 s for one of three exits:

    1. Worker thread completes — graceful shutdown succeeded; emits
       ``audit.shutdown.<stage_name>_done`` with ``forced: false`` and
       the elapsed ``duration_ms``.
    2. The run-level shutdown deadline elapses — escalates to
       ``pool.shutdown(wait=False)`` so the helper returns even though
       the underlying drain hasn't converged. Emits the same event with
       ``forced: true`` plus a ``timeout_seconds`` field naming what
       was waited on.
    3. ``cancellation.is_cancelled()`` becomes true (only meaningful
       when the helper was entered without cancellation already armed
       — in normal use the workflow's ``except KeyboardInterrupt``
       sets the cancel state before calling this helper).

    With ``cancellation=None`` (unit-test paths), the helper degrades
    to plain ``pool.shutdown(wait=True)`` with no deadline.
    """
    import threading
    from time import monotonic

    if cancellation is None:
        # Unit-test path — keep the original blocking semantics so
        # tests that don't set up a SIGINT handler don't see a
        # behaviour change.
        try:
            pool.shutdown(wait=True)
        except Exception:  # noqa: BLE001 - shutdown best-effort
            pass
        return

    start = monotonic()
    done = threading.Event()
    error: list[BaseException] = []

    def _drain() -> None:
        try:
            pool.shutdown(wait=True)
        except Exception as exc:  # noqa: BLE001 - record + return
            error.append(exc)
        finally:
            done.set()

    drain_thread = threading.Thread(
        target=_drain, name=f"audit-shutdown-{stage_name}", daemon=True
    )
    drain_thread.start()

    forced = False
    while True:
        if done.wait(0.5):
            break
        remaining = cancellation.remaining_seconds()
        if remaining is not None and remaining <= 0.0:
            forced = True
            try:
                pool.shutdown(wait=False)
            except Exception:  # noqa: BLE001 - escalation best-effort
                pass
            break

    duration_ms = int((monotonic() - start) * 1000)
    if logger is not None:
        try:
            logger.info_kv(
                f"audit.shutdown.{stage_name}_done",
                duration_ms=duration_ms,
                forced=forced,
            )
        except Exception:  # noqa: BLE001
            pass


def _is_neo4j_transport_error(exc: BaseException) -> bool:
    """True iff ``exc`` is a recoverable Neo4j connection failure.

    Lazy-imports neo4j-driver exception types so installs without
    that dependency (or workers running with InMemorySource only)
    don't take the import. Returns False if the import fails for
    any reason — those paths fall through to the master's
    WORKER_CRASHED routing, which is the conservative choice.
    """
    try:
        from neo4j.exceptions import (
            ServiceUnavailable,
            SessionExpired,
        )
    except Exception:  # noqa: BLE001 - lazy import safety
        return False
    return isinstance(exc, (ServiceUnavailable, SessionExpired))


class AuditWorkflow:
    def __init__(
        self,
        *,
        config: XAuditorConfig,
        llm_client: LLMClient,
        analyzer_agent: AnalyzerAgent | None = None,
        exploitation_agent: ExploitationAgent | None = None,
        validator_agent: ValidatorAgent | None = None,
        analyzer_team: AnalyzerTeam | None = None,
        validator_team: ValidatorTeam | None = None,
        exploiter_team: ExploiterTeam | None = None,
        coder_dispatcher: CoderDispatcher | None = None,
        logger: RuntimeLogger | None = None,
        cancellation: "RunCancellation | None" = None,
    ) -> None:
        self.config = config
        self.llm_client = llm_client
        self.teaming_enabled = bool(config.teaming.enabled)
        self.analyzer_agent = analyzer_agent or AnalyzerAgent()
        self.exploitation_agent = exploitation_agent or ExploitationAgent()
        self.validator_agent = validator_agent or ValidatorAgent()
        self.analyzer_team = analyzer_team
        self.validator_team = validator_team
        self.exploiter_team = exploiter_team
        if self.teaming_enabled and (
            self.analyzer_team is None
            or self.validator_team is None
            or self.exploiter_team is None
        ):
            raise ValueError(
                "AuditWorkflow: teaming.enabled is true but one or more teams are missing."
            )
        self.coder_dispatcher = coder_dispatcher
        self.coder_enabled = bool(config.coder.enabled) and coder_dispatcher is not None
        # Run-level cancellation token. ``None`` in unit-test paths that
        # don't go through the CLI; the workflow falls back to today's
        # behaviour (default-handler KeyboardInterrupt) in that case.
        self.cancellation = cancellation
        # Propagate to teams and coder dispatcher so every subagent /
        # coder wait point can poll the same event.
        for team in (self.analyzer_team, self.validator_team, self.exploiter_team):
            if team is not None and hasattr(team, "set_cancellation"):
                team.set_cancellation(cancellation)
        if self.coder_dispatcher is not None and hasattr(
            self.coder_dispatcher, "set_cancellation"
        ):
            self.coder_dispatcher.set_cancellation(cancellation)
        # Set when the workflow enters its cancellation handler. Once
        # raised, ANY coder ``on_settled`` callback that fires
        # afterwards (typically from a worker thread whose claude
        # subprocess just got SIGTERM'd by ``cancel_all``) MUST snap
        # the result to ``Skipped (cancelled by user)`` instead of
        # letting the worker's "transport error: exit -SIGTERM"
        # Inconclusive verdict overwrite the explicit cancellation
        # state. Without this, in-flight findings flip from Skipped
        # back to Inconclusive after we've already set them, and the
        # portal displays a confusing mix of cancellation and
        # transport-error states for one cancelled run.
        self._cancelling: threading.Event = threading.Event()
        self.logger = logger

    @classmethod
    def from_config(
        cls,
        config: XAuditorConfig,
        logger: RuntimeLogger | None = None,
        *,
        with_coder: bool = True,
        cancellation: RunCancellation | None = None,
    ) -> "AuditWorkflow":
        # ``with_coder=False`` is the path subprocess workers take
        # under ``LocalSubprocessPool``: workers run the agent chain
        # but DO NOT dispatch the coder stage themselves — coder
        # dispatch lives on the master so that there's exactly one
        # ``CoderDispatcher`` (and one claude subprocess) per audit
        # run, regardless of how many subprocess workers are spawned.
        auditor_model = resolve_chat_model(config.llm, agent="auditor")
        exploitation_override = config.llm.agent_overrides.get("exploitation")
        if exploitation_override is not None and (
            exploitation_override.provider or exploitation_override.has_sampling()
        ):
            exploitation_model = resolve_chat_model(config.llm, agent="exploitation")
        else:
            exploitation_model = auditor_model
        validator_model = resolve_chat_model(config.llm, agent="validator")
        analyzer_team = validator_team = exploiter_team = None
        if config.teaming.enabled:
            analyzer_team = AnalyzerTeam(teaming=config.teaming, llm=config.llm, logger=logger)
            validator_team = ValidatorTeam(teaming=config.teaming, llm=config.llm, logger=logger)
            exploiter_team = ExploiterTeam(teaming=config.teaming, llm=config.llm, logger=logger)
        coder_dispatcher: CoderDispatcher | None = None
        if with_coder and config.coder.enabled:
            coder_dispatcher = CoderDispatcher.from_config(config.coder, logger=logger)
        return cls(
            config=config,
            llm_client=LLMClient.from_config(config.llm, logger=logger, agent="auditor"),
            analyzer_agent=AnalyzerAgent(chat_model=auditor_model, logger=logger),
            exploitation_agent=ExploitationAgent(chat_model=exploitation_model, logger=logger),
            validator_agent=ValidatorAgent(chat_model=validator_model, logger=logger),
            analyzer_team=analyzer_team,
            validator_team=validator_team,
            exploiter_team=exploiter_team,
            coder_dispatcher=coder_dispatcher,
            logger=logger,
            cancellation=cancellation,
        )

    def run(
        self,
        *,
        source: AuditGraphSource,
        plan: AuditPlan,
        on_progress: Callable[[AuditRun], None] | None = None,
        on_heartbeat: HeartbeatCallback | None = None,
        on_finding_upsert: Callable[[Finding], None] | None = None,
        on_cancel: Callable[[AuditRun, str], None] | None = None,
        skip_paths: frozenset[str] = frozenset(),
        prior_shared_state: dict[str, dict[str, object]] | None = None,
    ) -> AuditRun:
        findings: list[Finding] = []
        checkpoints: dict[str, str] = {}
        shared_state: dict[str, dict[str, object]] = {}
        completed_units: list[AuditUnit] = []
        context_skipped_paths: set[str] = set()
        failed_paths: set[str] = set()
        pending_coder: set[str] = set()
        total_units = len(plan.audit_units)
        all_fingerprints = [item.path.path_fingerprint for item in plan.audit_units]
        prior_state = dict(prior_shared_state or {})

        def _emit(kind: str, stage: str, *, index: int | None = None, message: str = "") -> None:
            if on_heartbeat is None:
                return
            try:
                on_heartbeat(
                    ProgressEvent(
                        stage=stage,
                        heartbeat_kind=kind,
                        current_path_index=index,
                        total_paths=total_units if total_units else None,
                        message=message,
                    )
                )
            except Exception:  # noqa: BLE001 - heartbeats are best-effort
                pass

        _emit("started", "analyzer", message=f"Starting audit of {total_units} paths")

        def _current_snapshot() -> AuditRun:
            return self._build_audit_run(
                source=source,
                audit_units=tuple(completed_units),
                findings=tuple(findings),
                checkpoints=dict(checkpoints),
                shared_state=dict(shared_state),
                skipped_paths=(
                    {fp for fp in all_fingerprints if fp not in checkpoints}
                    | context_skipped_paths
                ),
                failed_paths=set(failed_paths),
            )

        # Build the WorkerPool from config and route per-path work
        # through it. ``worker_count == 1`` (the default) yields an
        # ``InlineExecutor`` (synchronous-on-master); ``>= 2`` yields
        # a ``LocalSubprocessPool``. The aggregator runs on this
        # thread in plan order so finding-ids stay deterministic
        # across topologies.
        worker_count = self.config.audit.worker_count
        source_descriptor = None
        if worker_count >= 2:
            # Every ``AuditGraphSource`` impl exposes
            # ``pool_descriptor()`` returning a pickle-friendly
            # frozen dataclass that subprocess workers feed into
            # ``rebuild_source(...)`` to reconstruct an equivalent
            # source on their end. 0.9.0+: only
            # ``Neo4jAuditGraphSource`` is reachable from production
            # config; tests use ``_TestOnlyGraphSource``.
            source_descriptor = source.pool_descriptor()
        pool: WorkerPool = build_worker_pool(
            worker_count=worker_count,
            config=self.config if worker_count >= 2 else None,
            source_descriptor=source_descriptor,
            logger=self.logger,
        )
        # Inject the run-level cancellation token so ``pool.shutdown``
        # can short-circuit its 10 s graceful join window when the
        # operator has asked to bail out. ``hasattr`` guards the
        # ``InlineExecutor`` path which has no executor state to
        # cancel.
        if self.cancellation is not None and hasattr(pool, "set_cancellation"):
            pool.set_cancellation(self.cancellation)
        if self.logger is not None:
            kind = "inline" if worker_count == 1 else "subprocess"
            paths_word = "path" if worker_count == 1 else "paths"
            self.logger.info(
                "Audit pool: "
                f"workers={worker_count} ({kind}; "
                f"host total in-flight={worker_count} {paths_word})"
            )

        def _runner(idx: int, candidate: AuditUnit) -> PathResult:
            return self._process_one_path(
                candidate_unit=candidate,
                index=idx,
                total_units=total_units,
                source=source,
            )

        try:
            # First pass: submit every non-resume path to the pool;
            # handle resume-skip paths inline. ``submitted`` keeps
            # the plan-order list of (index, unit, future|None).
            submitted: list[tuple[int, AuditUnit, "Future[PathResult] | None"]] = []
            for index, candidate_unit in enumerate(plan.audit_units, start=1):
                path_fp = candidate_unit.path.path_fingerprint
                if path_fp in skip_paths:
                    prior_entry = prior_state.get(path_fp)
                    if prior_entry:
                        # Fully-resumable path: carry the prior state
                        # forward without re-invoking any agent.
                        shared_state[path_fp] = dict(prior_entry)
                        checkpoints[path_fp] = str(
                            prior_entry.get("resumed_checkpoint")
                            or prior_entry.get("checkpoint_status")
                            or "resumed"
                        )
                        completed_units.append(candidate_unit)
                        if self.logger is not None:
                            self.logger.info(
                                f"Resume: skipped completed path {index}/{total_units}"
                            )
                        _emit(
                            "progress",
                            "analyzer",
                            index=index,
                            message=f"Resume: skipped completed path {index}/{total_units}",
                        )
                        if on_progress is not None:
                            on_progress(_current_snapshot())
                        submitted.append((index, candidate_unit, None))
                        continue
                    if self.logger is not None:
                        self.logger.warning(
                            f"Resume: path {path_fp} marked skip but no prior state; "
                            "re-executing from scratch."
                        )
                future = pool.submit_path(_runner, index, candidate_unit)
                submitted.append((index, candidate_unit, future))

            # Second pass: walk in plan order, blocking on each
            # future and aggregating its outcome into shared state.
            # Plan-order traversal keeps finding-ids deterministic
            # regardless of which thread (or, in Phase 3, which
            # worker) actually produced each PathResult.
            for index, candidate_unit, future in submitted:
                # Drain coder verdicts that landed since the previous
                # path's aggregation, if any.
                if self.coder_enabled:
                    self._drain_coder_results(
                        findings=findings,
                        pending_coder=pending_coder,
                        on_progress=on_progress,
                        snapshot_factory=_current_snapshot,
                        on_finding_upsert=on_finding_upsert,
                    )
                if future is None:
                    # Resume-skip path; already handled above.
                    continue
                result = future.result()
                fp = result.path_fingerprint
                if result.outcome is PathOutcome.COMPLETED:
                    unit = result.unit
                    path_functions = list(result.path_functions)
                    path_context = result.path_context
                    path_shared_state = result.path_shared_state
                    shared_state[fp] = path_shared_state
                    checkpoints[fp] = result.checkpoint_status
                    finding_index_in_path = 0
                    for analyzer, exploitation, validator, is_candidate in result.per_finding:
                        if not is_candidate:
                            continue
                        finding = self._build_finding(
                            index=len(findings) + 1,
                            unit=unit,
                            path_functions=path_functions,
                            analyzer=analyzer,
                            exploitation=exploitation,
                            validator=validator,
                            referenced_symbols=path_context.get("referenced_symbols", ()),
                        )
                        if finding is not None:
                            if self.coder_enabled and self.coder_dispatcher is not None:
                                finding = self._dispatch_coder(
                                    finding=finding,
                                    unit=unit,
                                    path_context=path_context,
                                    path_shared_state=path_shared_state,
                                    finding_index_in_path=finding_index_in_path,
                                    on_settled=self._make_stream_callback(
                                        findings=findings,
                                        pending_coder=pending_coder,
                                        on_progress=on_progress,
                                        snapshot_factory=_current_snapshot,
                                        on_finding_upsert=on_finding_upsert,
                                    ),
                                )
                                pending_coder.add(finding.finding_id)
                            findings.append(finding)
                            if self.logger is not None:
                                self.logger.debug_kv(
                                    "Finding emitted",
                                    finding_id=finding.finding_id,
                                    path=fp,
                                    validation_status=finding.validation_status.value,
                                    coder_status=finding.coder_status,
                                )
                        finding_index_in_path += 1
                    completed_units.append(unit)
                elif result.outcome is PathOutcome.CONTEXT_SKIPPED:
                    context_skipped_paths.add(fp)
                    checkpoints[fp] = "skipped_context_window"
                    shared_state[fp] = {
                        "skipped": {
                            "reason": "context_window_exceeded",
                            **(result.error_info or {}),
                        }
                    }
                elif result.outcome is PathOutcome.LLM_FAILED:
                    failed_paths.add(fp)
                    checkpoints[fp] = "failed_llm_error"
                    shared_state[fp] = {
                        "failed": {
                            "reason": "llm_error",
                            **(result.error_info or {}),
                        }
                    }
                elif result.outcome is PathOutcome.CANCELLED:
                    # Worker bailed at a stage boundary because
                    # ``self._cancelling`` was set; the cancellation
                    # handler below will surface the partial run.
                    continue
                # Emit one ``"progress"`` heartbeat per integrated
                # path. Behavior matches the 0.5.x serial workflow:
                # heartbeats fire in plan order. With concurrency,
                # they may arrive in bursts (paths after path_k may
                # already be done by the time path_k completes); the
                # message itself reflects the integration order.
                if result.outcome is PathOutcome.COMPLETED:
                    msg = f"Completed path {index}/{total_units}"
                elif result.outcome is PathOutcome.CONTEXT_SKIPPED:
                    msg = f"Skipped path {index}/{total_units} (context window)"
                else:
                    msg = f"Failed path {index}/{total_units} (LLM error)"
                _emit("progress", "analyzer", index=index, message=msg)
                if on_progress is not None:
                    on_progress(_current_snapshot())
        except KeyboardInterrupt as exc:
            # Set BEFORE cancel_all so any worker / coder callback
            # that fires between cancel_all (SIGTERM) and the
            # snapshot emit below sees the flag and snaps its
            # result to Skipped instead of Inconclusive (see
            # ``_make_stream_callback``).
            self._cancelling.set()
            # Flip the portal status to "cancelled" RIGHT NOW —
            # before pool.cancel_all and the long pool.shutdown
            # drain. If the user is impatient and hammers Ctrl+C
            # several more times during the drain, the process
            # may exit hard before the rest of the cancellation
            # bookkeeping completes; doing this hot-path write
            # first guarantees the portal at least reflects
            # "cancelled" instead of being stuck on "in progress".
            cancel_message = "cancelled by user"
            early_partial = _current_snapshot()
            if on_cancel is not None:
                try:
                    on_cancel(early_partial, cancel_message)
                except Exception:  # noqa: BLE001 - best-effort flush
                    pass
            pool.cancel_all()
            if self.coder_enabled and self.coder_dispatcher is not None:
                self.coder_dispatcher.cancel_all()
                self._mark_pending_coder_skipped(
                    findings=findings,
                    pending_coder=pending_coder,
                    reason=cancel_message,
                )
            partial_run = _current_snapshot()
            if on_progress is not None:
                try:
                    on_progress(partial_run)
                except Exception:  # noqa: BLE001 - best-effort final emit
                    pass
            raise UserCancelledError(
                "Audit cancelled by user.", partial_audit_run=partial_run
            ) from exc
        except Exception as exc:
            # Cascade-cancel on external run deletion: when
            # ``RunDeletedExternallyError`` propagates out of the per-path
            # loop, set the same primitives the ``KeyboardInterrupt``
            # handler above sets so the immediately-following ``finally``
            # block's ``_shutdown_pool_with_deadline`` escalates within
            # ``audit.shutdown_timeout_seconds`` instead of waiting for
            # path workers to drain naturally. Without this, the audit
            # appears stuck (the workflow has cleanly raised, but the
            # pool drain in ``finally`` blocks on in-flight LLM calls).
            #
            # Any other exception class falls through unchanged.
            if _is_run_deleted_externally(exc):
                pool.cancel_all()
                if self.coder_enabled and self.coder_dispatcher is not None:
                    self.coder_dispatcher.cancel_all()
                if self.cancellation is not None:
                    self.cancellation.set_cancelled()
                self._cancelling.set()
            raise
        finally:
            # Drain the worker pool with a bounded wall-clock deadline
            # observed via the run-level cancellation token. The CLI's
            # SIGINT handler installed by ``install_sigint_handler``
            # turns repeat Ctrl+C into ``os._exit(130)`` so we no
            # longer need to mask SIGINT here — the previous
            # ``signal.SIG_IGN`` block actively defeated the operator's
            # ability to escape a stalled drain.
            _shutdown_pool_with_deadline(
                pool,
                cancellation=self.cancellation,
                logger=self.logger,
                stage_name="worker_pool",
            )

        # Per-path loop is finished; drain any coder tasks still in flight
        # before declaring the run complete.
        if (
            self.coder_enabled
            and self.coder_dispatcher is not None
            and not self._cancelling.is_set()
        ):
            try:
                self._drain_coder_results(
                    findings=findings,
                    pending_coder=pending_coder,
                    block_until_empty=True,
                    on_progress=on_progress,
                    snapshot_factory=_current_snapshot,
                    on_finding_upsert=on_finding_upsert,
                )
            except KeyboardInterrupt as exc:
                # Set BEFORE cancel_all (see comment in the
                # per-path cancel handler above).
                self._cancelling.set()
                self.coder_dispatcher.cancel_all()
                self._mark_pending_coder_skipped(
                    findings=findings,
                    pending_coder=pending_coder,
                    reason="cancelled by user",
                )
                partial_run = _current_snapshot()
                if on_progress is not None:
                    try:
                        on_progress(partial_run)
                    except Exception:  # noqa: BLE001 - best-effort final emit
                        pass
                raise UserCancelledError(
                    "Audit cancelled by user.", partial_audit_run=partial_run
                ) from exc

        _emit(
            "stage_completed",
            "analyzer",
            index=total_units,
            message=f"All {total_units} paths processed",
        )
        return self._build_audit_run(
            source=source,
            audit_units=tuple(completed_units),
            findings=tuple(findings),
            checkpoints=checkpoints,
            shared_state=shared_state,
            skipped_paths=set(context_skipped_paths),
            failed_paths=set(failed_paths),
        )

    def _process_one_path(
        self,
        *,
        candidate_unit: AuditUnit,
        index: int,
        total_units: int,
        source: AuditGraphSource,
    ) -> PathResult:
        # Per-path agent chain — runs in a worker thread (Phase 2,
        # ``LocalThreadPool``) or a worker subprocess (Phase 3,
        # ``LocalSubprocessPool``). Pure with respect to the
        # workflow's mutable state: only reads ``self`` (config,
        # logger, agents, teaming flags) and the per-path ``source``
        # reader. Returns a ``PathResult`` for the aggregator on the
        # master thread to integrate.
        #
        # Heartbeats are emitted by the master aggregator (not from
        # workers) so the behavior is identical for thread pools and
        # subprocess pools — workers in subprocess pools don't have
        # an in-memory channel back to the master's ``on_heartbeat``
        # closure, so master-side emit keeps the contract uniform.
        path_fp = candidate_unit.path.path_fingerprint
        if self._cancelling.is_set():
            return PathResult(
                index=index,
                path_fingerprint=path_fp,
                outcome=PathOutcome.CANCELLED,
            )
        try:
            if self.logger is not None:
                self.logger.debug_kv(
                    "Audit path start",
                    index=index,
                    total=total_units,
                    path=path_fp,
                    entry_function=candidate_unit.path.entry_function,
                    functions=candidate_unit.path.function_names,
                )
            unit = self._enrich_unit(candidate_unit)
            path_functions = source.load_path_functions(unit.function_ids)
            module_symbols, function_symbol_uses = source.load_path_symbols(unit.function_ids)
            path_context = build_path_context(
                repo_root=self.config.repo_root,
                path_functions=path_functions,
                module_symbols=module_symbols,
                function_symbol_uses=function_symbol_uses,
            )
            if self._cancelling.is_set():
                return PathResult(
                    index=index,
                    path_fingerprint=path_fp,
                    outcome=PathOutcome.CANCELLED,
                )
            if self.teaming_enabled:
                per_finding, path_shared_state, checkpoint_status = self._process_unit_teaming(
                    unit=unit,
                    path_functions=path_functions,
                    path_context=path_context,
                )
            else:
                per_finding, path_shared_state, checkpoint_status = self._process_unit_single(
                    unit=unit,
                    path_functions=path_functions,
                    path_context=path_context,
                )
            if self.logger is not None:
                self.logger.debug_kv(
                    "Recorded audit checkpoint",
                    path=path_fp,
                    status=checkpoint_status,
                )
                self.logger.info(f"Completed audit path {index}/{total_units}")
            return PathResult(
                index=index,
                path_fingerprint=path_fp,
                outcome=PathOutcome.COMPLETED,
                unit=unit,
                path_functions=tuple(path_functions),
                path_context=path_context,
                path_shared_state=path_shared_state,
                checkpoint_status=checkpoint_status,
                per_finding=tuple(per_finding),
            )
        except ContextWindowExceededError as exc:
            if self.logger is not None:
                self.logger.error_kv(
                    "Audit path skipped: LLM context window exceeded",
                    path=path_fp,
                    entry_function=candidate_unit.path.entry_function,
                    operation=exc.operation,
                    provider=exc.provider,
                    model=exc.model_name,
                    cause=str(exc.__cause__ or exc),
                )
            return PathResult(
                index=index,
                path_fingerprint=path_fp,
                outcome=PathOutcome.CONTEXT_SKIPPED,
                error_info={
                    "operation": exc.operation,
                    "provider": exc.provider,
                    "model": exc.model_name,
                    "cause": str(exc.__cause__ or exc),
                },
            )
        except LLMError as exc:
            if self.logger is not None:
                self.logger.error_kv(
                    "Audit path failed: LLM error",
                    path=path_fp,
                    entry_function=candidate_unit.path.entry_function,
                    operation=exc.operation,
                    provider=exc.provider,
                    model=exc.model_name,
                    cause=str(exc.__cause__ or exc),
                )
            return PathResult(
                index=index,
                path_fingerprint=path_fp,
                outcome=PathOutcome.LLM_FAILED,
                error_info={
                    "operation": exc.operation,
                    "provider": exc.provider,
                    "model": exc.model_name,
                    "cause": str(exc.__cause__ or exc),
                },
            )
        except Exception as exc:  # noqa: BLE001 - taxonomy reclassifier
            # Recoverable Neo4j transport failures get reclassified
            # as ``PathOutcome.LLM_FAILED`` so the path lands in
            # ``failed_paths`` and ``xauditor audit resume`` re-runs
            # it on the next pass — same UX as a transient LLM
            # failure. Anything else propagates to the future and
            # ultimately to the master's WORKER_CRASHED routing.
            if not _is_neo4j_transport_error(exc):
                raise
            if self.logger is not None:
                self.logger.error_kv(
                    "Audit path failed: Neo4j transport error",
                    path=path_fp,
                    entry_function=candidate_unit.path.entry_function,
                    cause=str(exc),
                )
            return PathResult(
                index=index,
                path_fingerprint=path_fp,
                outcome=PathOutcome.LLM_FAILED,
                error_info={
                    "reason": "neo4j_unavailable",
                    "operation": "source_query",
                    "provider": "neo4j",
                    "model": "",
                    "cause": str(exc),
                },
            )

    def _model_settings_for_role(self, role: str) -> dict[str, object]:
        provider_name = self.config.llm.selected_provider(role) or "default"
        sampling = SamplingParams.from_mapping(self.config.llm.sampling_for(role))
        return {
            "role": role,
            "provider_name": provider_name,
            "sampling": sampling.as_manifest(),
        }

    def _teaming_model_settings(self, team_cfg, *, role: str, records) -> list[dict[str, object]]:
        override = self.config.llm.agent_overrides.get(role)
        entries: list[dict[str, object]] = []
        for record in records:
            provider_name = record.provider_name
            provider = self.config.llm.providers.get(provider_name)
            if provider is None:
                sampling_dict: dict[str, float | int | None] = {
                    field_name: None
                    for field_name in ("temperature", "top_p", "top_k", "repetition_penalty")
                }
            elif override is not None:
                sampling_dict = override.sampling_overlay(provider)
            else:
                sampling_dict = provider.sampling_dict()
            entries.append(
                {
                    "role": role,
                    "subagent_index": record.subagent_index,
                    "provider_name": provider_name,
                    "sampling": sampling_dict,
                }
            )
        return entries

    def _process_unit_single(
        self,
        *,
        unit: AuditUnit,
        path_functions,
        path_context: dict[str, object],
    ) -> tuple[
        list[tuple[AnalyzerResult, ExploitationResult | None, ValidationResult | None, bool]],
        dict[str, object],
        str,
    ]:
        analyzer = self.analyzer_agent.run(
            unit=unit, path_functions=path_functions, path_context=path_context
        )
        if self.logger is not None:
            self.logger.debug_kv(
                "Analyzer completed",
                path=unit.path.path_fingerprint,
                status=analyzer.status,
                evidence=analyzer.evidence_strength,
                reason=analyzer.reason,
            )
        analyzer_payload = self._analyzer_payload(
            analyzer=analyzer, path_functions=path_functions
        )
        single_model_settings = {
            "analyzer": self._model_settings_for_role("auditor"),
            "exploitation": self._model_settings_for_role("exploitation"),
            "validator": self._model_settings_for_role("validator"),
        }
        if analyzer.status != "candidate":
            if self.logger is not None:
                self.logger.debug_kv(
                    "Downstream agents skipped",
                    path=unit.path.path_fingerprint,
                    reason="analyzer reported no finding",
                    analyzer_status=analyzer.status,
                )
            exploitation_payload = {
                "status": "skipped",
                "steps": "Skipped because analyzer did not report a candidate finding.",
            }
            validator_payload = {
                "status": "skipped",
                "analysis": "Skipped because analyzer did not report a candidate finding.",
            }
            return (
                [(analyzer, None, None, False)],
                {
                    "analyzer": analyzer_payload,
                    "exploitation": exploitation_payload,
                    "validator": validator_payload,
                    "model_settings": single_model_settings,
                },
                "no_finding",
            )
        exploitation = self.exploitation_agent.run(
            unit=unit, analyzer=analyzer, path_context=path_context
        )
        if self.logger is not None:
            self.logger.debug_kv(
                "Exploitation completed",
                path=unit.path.path_fingerprint,
                status=exploitation.status,
            )
        validator = self.validator_agent.run(
            unit=unit, analyzer=analyzer, exploitation=exploitation, path_context=path_context
        )
        if self.logger is not None:
            self.logger.debug_kv(
                "Validator verdict",
                path=unit.path.path_fingerprint,
                status=validator.status.value,
                analysis=validator.analysis,
            )
        return (
            [(analyzer, exploitation, validator, True)],
            {
                "analyzer": analyzer_payload,
                "exploitation": {"status": exploitation.status, "steps": exploitation.steps},
                "validator": {"status": validator.status.value, "analysis": validator.analysis},
                "model_settings": single_model_settings,
            },
            validator.status.value,
        )

    def _process_unit_teaming(
        self,
        *,
        unit: AuditUnit,
        path_functions,
        path_context: dict[str, object],
    ) -> tuple[
        list[tuple[AnalyzerResult, ExploitationResult | None, ValidationResult | None, bool]],
        dict[str, object],
        str,
    ]:
        assert self.analyzer_team is not None
        assert self.validator_team is not None
        assert self.exploiter_team is not None
        consolidated, analyzer_records = self.analyzer_team.run(
            unit=unit, path_functions=path_functions, path_context=path_context
        )
        analyzer_subagents = [
            self._analyzer_record_to_dict(record) for record in analyzer_records
        ]
        analyzer_model_settings = self._teaming_model_settings(
            self.config.teaming.analyzer, role="auditor", records=analyzer_records
        )
        if not consolidated:
            representative = analyzer_records[0].result if analyzer_records else AnalyzerResult(
                status="no_issue"
            )
            return (
                [(representative, None, None, False)],
                {
                    "analyzer": self._analyzer_payload(
                        analyzer=representative, path_functions=path_functions
                    ),
                    "exploitation": {
                        "status": "skipped",
                        "steps": "Skipped because analyzer team produced no candidate finding.",
                    },
                    "validator": {
                        "status": "skipped",
                        "analysis": "Skipped because analyzer team produced no candidate finding.",
                    },
                    "analyzer_subagents": analyzer_subagents,
                    "validator_subagents": {},
                    "exploiter_subagents": {},
                    "validator_debates": {},
                    "model_settings": {
                        "analyzer_subagents": analyzer_model_settings,
                        "validator_subagents": {},
                        "exploiter_subagents": {},
                    },
                },
                "no_finding",
            )

        validator_subagents: dict[str, list[dict[str, object]]] = {}
        exploiter_subagents: dict[str, list[dict[str, object]]] = {}
        validator_debates: dict[str, dict[str, object]] = {}
        validator_model_settings: dict[str, list[dict[str, object]]] = {}
        exploiter_model_settings: dict[str, list[dict[str, object]]] = {}
        per_finding: list[tuple[AnalyzerResult, ExploitationResult | None, ValidationResult | None, bool]] = []
        worst_status_rank = -1
        status_rank = {
            "Valid": 4,
            "Partial Valid": 3,
            "Inconclusive": 2,
            "False Positive": 1,
        }
        final_checkpoint = "no_finding"
        consolidated_analyzer_payloads: list[dict[str, object]] = []
        consolidated_exploit_payloads: list[dict[str, object]] = []
        consolidated_validator_payloads: list[dict[str, object]] = []

        for finding_index, analyzer in enumerate(consolidated):
            finding_fingerprint = f"{unit.path.path_fingerprint}::f{finding_index}"
            validator_result, validator_records, debate = self.validator_team.run_for_finding(
                unit=unit,
                analyzer=analyzer,
                path_context=path_context,
                finding_fingerprint=finding_fingerprint,
            )
            validator_subagents[finding_fingerprint] = [
                self._validator_record_to_dict(record) for record in validator_records
            ]
            validator_model_settings[finding_fingerprint] = self._teaming_model_settings(
                self.config.teaming.validator, role="validator", records=validator_records
            )
            if debate is not None:
                validator_debates[finding_fingerprint] = self._debate_to_dict(debate)

            all_false_positive = bool(validator_records) and all(
                record.result.status == ValidationStatus.FALSE_POSITIVE for record in validator_records
            )
            if all_false_positive:
                if self.logger is not None:
                    self.logger.debug_kv(
                        "Exploiter team skipped",
                        path=unit.path.path_fingerprint,
                        finding=finding_fingerprint,
                        reason="all validator subagents voted false positive",
                    )
                exploitation_result = ExploitationResult(
                    status="not_applicable",
                    steps="Skipped because every validator subagent returned False Positive.",
                )
                exploiter_records: list = []
            else:
                validator_context = {
                    "validator_verdict": validator_result.status.value,
                    "validator_analysis": validator_result.analysis,
                }
                exploitation_result, exploiter_records = self.exploiter_team.run_for_finding(
                    unit=unit,
                    analyzer=analyzer,
                    path_context=path_context,
                    validator_context=validator_context,
                )
            exploiter_subagents[finding_fingerprint] = [
                self._exploiter_record_to_dict(record) for record in exploiter_records
            ]
            exploiter_model_settings[finding_fingerprint] = self._teaming_model_settings(
                self.config.teaming.exploiter, role="exploitation", records=exploiter_records
            )

            consolidated_analyzer_payloads.append(
                self._analyzer_payload(analyzer=analyzer, path_functions=path_functions)
            )
            consolidated_exploit_payloads.append(
                {"status": exploitation_result.status, "steps": exploitation_result.steps}
            )
            consolidated_validator_payloads.append(
                {"status": validator_result.status.value, "analysis": validator_result.analysis}
            )
            per_finding.append((analyzer, exploitation_result, validator_result, True))

            current_rank = status_rank.get(validator_result.status.value, 0)
            if current_rank > worst_status_rank:
                worst_status_rank = current_rank
                final_checkpoint = validator_result.status.value

        path_shared_state: dict[str, object] = {
            "analyzer": (
                consolidated_analyzer_payloads[0]
                if len(consolidated_analyzer_payloads) == 1
                else {"findings": consolidated_analyzer_payloads}
            ),
            "exploitation": (
                consolidated_exploit_payloads[0]
                if len(consolidated_exploit_payloads) == 1
                else {"findings": consolidated_exploit_payloads}
            ),
            "validator": (
                consolidated_validator_payloads[0]
                if len(consolidated_validator_payloads) == 1
                else {"findings": consolidated_validator_payloads}
            ),
            "analyzer_subagents": analyzer_subagents,
            "validator_subagents": validator_subagents,
            "exploiter_subagents": exploiter_subagents,
            "validator_debates": validator_debates,
            "model_settings": {
                "analyzer_subagents": analyzer_model_settings,
                "validator_subagents": validator_model_settings,
                "exploiter_subagents": exploiter_model_settings,
            },
        }
        return per_finding, path_shared_state, final_checkpoint

    def _analyzer_payload(self, *, analyzer, path_functions) -> dict[str, object]:
        payload = {
            "status": analyzer.status,
            "finding_name": analyzer.finding_name,
            "description": analyzer.description,
            "analysis": analyzer.analysis,
            "reason": analyzer.reason,
            "context_notes": analyzer.context_notes,
            "suspect_function_id": analyzer.suspect_function_id,
            "suspect_line": analyzer.suspect_line,
            "evidence_strength": analyzer.evidence_strength,
        }
        if analyzer.status == "candidate":
            payload["chain_source"] = self._render_chain_snippets(
                path_functions=path_functions,
                suspect_function_id=analyzer.suspect_function_id,
                suspect_line=analyzer.suspect_line,
                reason=analyzer.reason,
            )
        return payload

    @staticmethod
    def _analyzer_record_to_dict(record) -> dict[str, object]:
        result = record.result
        return {
            "subagent_index": record.subagent_index,
            "provider_name": record.provider_name,
            "model_name": record.model_name,
            "result": {
                "status": result.status,
                "finding_name": result.finding_name,
                "description": result.description,
                "analysis": result.analysis,
                "reason": result.reason,
                "context_notes": result.context_notes,
                "suspect_function_id": result.suspect_function_id,
                "suspect_line": result.suspect_line,
                "evidence_strength": result.evidence_strength,
            },
        }

    @staticmethod
    def _validator_record_to_dict(record) -> dict[str, object]:
        return {
            "subagent_index": record.subagent_index,
            "provider_name": record.provider_name,
            "model_name": record.model_name,
            "result": {
                "status": record.result.status.value,
                "analysis": record.result.analysis,
            },
        }

    @staticmethod
    def _exploiter_record_to_dict(record) -> dict[str, object]:
        return {
            "subagent_index": record.subagent_index,
            "provider_name": record.provider_name,
            "model_name": record.model_name,
            "result": {
                "status": record.result.status,
                "steps": record.result.steps,
            },
        }

    @staticmethod
    def _debate_to_dict(debate: DebateTranscript) -> dict[str, object]:
        return {
            "finding_fingerprint": debate.finding_fingerprint,
            "max_rounds": debate.max_rounds,
            "initial_verdicts": dict(debate.initial_verdicts),
            "final_verdict": debate.final_verdict,
            "converged": debate.converged,
            "turns": [
                {
                    "round_index": turn.round_index,
                    "subagent_index": turn.subagent_index,
                    "provider_name": turn.provider_name,
                    "system_prompt": turn.system_prompt,
                    "user_message": turn.user_message,
                    "raw_response": turn.raw_response,
                    "verdict": turn.verdict,
                    "rebuttal": turn.rebuttal,
                }
                for turn in debate.turns
            ],
        }

    def _enrich_unit(self, unit: AuditUnit) -> AuditUnit:
        if unit.path.business_context and unit.path.trust_boundary:
            if self.logger is not None:
                self.logger.debug_kv(
                    "Path enrichment skipped",
                    path=unit.path.path_fingerprint,
                    business_context=unit.path.business_context,
                    trust_boundary=unit.path.trust_boundary,
                )
            return unit
        if self.logger is not None:
            self.logger.debug_kv(
                "Path enrichment request",
                path=unit.path.path_fingerprint,
                entry_function=unit.path.entry_function,
                functions=unit.path.function_names,
            )
        enrichment = self.llm_client.summarize_path(unit.path.entry_function, unit.path.function_names)
        if self.logger is not None:
            self.logger.debug_kv(
                "Path enrichment response",
                path=unit.path.path_fingerprint,
                business_context=enrichment["business_context"],
                trust_boundary=enrichment["trust_boundary"],
            )
        return AuditUnit(
            path=PathRecord(
                entry_function=unit.path.entry_function,
                function_names=unit.path.function_names,
                file_paths=unit.path.file_paths,
                path_fingerprint=unit.path.path_fingerprint,
                function_ids=unit.path.function_ids,
                business_context=enrichment["business_context"],
                trust_boundary=enrichment["trust_boundary"],
            ),
            function_ids=unit.function_ids,
        )

    def _build_finding(self, *, index: int, unit, path_functions, analyzer, exploitation, validator, referenced_symbols=()):
        if analyzer.status != "candidate":
            if self.logger is not None:
                self.logger.debug_kv(
                    "Finding suppressed",
                    path=unit.path.path_fingerprint,
                    reason="analyzer result is not a candidate",
                    analyzer_status=analyzer.status,
                )
            return None

        suspect = next(
            (function for function in path_functions if function.function_id == analyzer.suspect_function_id),
            None,
        )
        if suspect is None and self.logger is not None:
            self.logger.debug_kv(
                "Suspect function unresolved; emitting finding without suspect annotation",
                path=unit.path.path_fingerprint,
                suspect_function_id=analyzer.suspect_function_id,
            )
        references = self._build_chain_references(
            path_functions=path_functions,
            suspect_function_id=analyzer.suspect_function_id,
            suspect_line=analyzer.suspect_line,
            reason=analyzer.reason,
        )
        call_stack_line = "Call stack: " + " -> ".join(unit.path.function_names)
        return Finding(
            finding_id=f"F-{index:04d}",
            finding_name=analyzer.finding_name,
            finding_description=analyzer.description,
            confidence_level=self._assign_confidence(analyzer=analyzer, validator=validator),
            source_references=references,
            analysis=analyzer.analysis,
            reason=analyzer.reason,
            context=call_stack_line,
            business_context=unit.path.business_context or f"Path from {unit.path.entry_function}.",
            exploitation_status=exploitation.status,
            exploitation_steps=exploitation.steps,
            validation_status=validator.status,
            validation_analysis=validator.analysis,
            path_fingerprint=unit.path.path_fingerprint,
            function_names=unit.path.function_names,
            analyzer_status=analyzer.status,
            evidence_strength=analyzer.evidence_strength,
            suspect_function_id=analyzer.suspect_function_id,
            suspect_line=analyzer.suspect_line,
            context_notes=analyzer.context_notes,
            referenced_symbols=tuple(dict(symbol) for symbol in referenced_symbols),
        )

    def _render_chain_snippets(
        self,
        *,
        path_functions,
        suspect_function_id: str,
        suspect_line: int,
        reason: str,
    ) -> tuple[dict[str, object], ...]:
        snippets: list[dict[str, object]] = []
        for function in path_functions:
            absolute = str((self.config.repo_root / function.file_path).resolve())
            language = language_for_path(function.file_path)
            annotations = (
                {suspect_line: reason}
                if function.function_id == suspect_function_id and suspect_line
                else None
            )
            snippets.append(
                {
                    "function_id": function.function_id,
                    "qualified_name": function.qualified_name,
                    "file_path": absolute,
                    "start_line": function.start_line,
                    "end_line": function.end_line,
                    "is_suspect": function.function_id == suspect_function_id,
                    "snippet": render_source_snippet(
                        file_path=absolute,
                        start_line=function.start_line,
                        end_line=function.end_line,
                        focus_lines=(suspect_line,) if function.function_id == suspect_function_id else (),
                        language=language,
                        annotations=annotations,
                    ),
                }
            )
        return tuple(snippets)

    def _build_chain_references(
        self,
        *,
        path_functions,
        suspect_function_id: str,
        suspect_line: int,
        reason: str,
    ) -> tuple[SourceReference, ...]:
        references: list[SourceReference] = []
        for function in path_functions:
            absolute = str((self.config.repo_root / function.file_path).resolve())
            language = language_for_path(function.file_path)
            is_suspect = function.function_id == suspect_function_id
            focus_lines = (suspect_line,) if is_suspect and suspect_line else ()
            annotations = {suspect_line: reason} if is_suspect and suspect_line else None
            references.append(
                SourceReference(
                    file_path=absolute,
                    start_line=function.start_line,
                    end_line=function.end_line,
                    focus_lines=focus_lines,
                    language=language,
                    snippet=render_source_snippet(
                        file_path=absolute,
                        start_line=function.start_line,
                        end_line=function.end_line,
                        focus_lines=focus_lines,
                        language=language,
                        annotations=annotations,
                    ),
                )
            )
        return tuple(references)

    def _build_audit_run(
        self,
        *,
        source: AuditGraphSource,
        audit_units: tuple[AuditUnit, ...],
        findings: tuple[Finding, ...],
        checkpoints: dict[str, str],
        shared_state: dict[str, dict[str, object]],
        skipped_paths: set[str],
        failed_paths: set[str] | frozenset[str] = frozenset(),
    ) -> AuditRun:
        coverage = source.build_coverage(
            plan=AuditPlan(audit_units=audit_units),
            skipped_paths=skipped_paths,
            failed_paths=failed_paths,
        )
        return AuditRun(
            build_fingerprint=source.build_fingerprint,
            findings=findings,
            coverage=coverage,
            checkpoints=checkpoints,
            shared_state=shared_state,
        )

    def _assign_confidence(self, *, analyzer, validator) -> ConfidenceLevel:
        if validator.status == ValidationStatus.FALSE_POSITIVE:
            return ConfidenceLevel.LOW
        if (
            validator.status in (ValidationStatus.PARTIAL_VALID, ValidationStatus.INCONCLUSIVE)
            or analyzer.evidence_strength == "medium"
        ):
            return ConfidenceLevel.MEDIUM
        return ConfidenceLevel.HIGH

    # ------------------------------------------------------------------ coder

    def _dispatch_coder(
        self,
        *,
        finding: Finding,
        unit: AuditUnit,
        path_context: dict[str, object],
        path_shared_state: dict[str, object],
        finding_index_in_path: int,
        on_settled: Callable[[str, CoderResult], None] | None = None,
    ) -> Finding:
        """Submit a coder verification task and return the Pending finding.

        Returns the finding unchanged when ``coder.enabled`` is false (so
        the default ``Skipped`` carries through). When enabled, transitions
        the finding to ``Pending`` and dispatches the task asynchronously.
        """

        if not self.coder_enabled or self.coder_dispatcher is None:
            return finding
        analyzer_payload = path_shared_state.get("analyzer")
        if isinstance(analyzer_payload, dict) and "findings" in analyzer_payload:
            payloads = analyzer_payload.get("findings") or []
            analyzer_block = payloads[finding_index_in_path] if finding_index_in_path < len(payloads) else None
        else:
            analyzer_block = analyzer_payload
        exploitation_payload = path_shared_state.get("exploitation")
        if isinstance(exploitation_payload, dict) and "findings" in exploitation_payload:
            payloads = exploitation_payload.get("findings") or []
            exploitation_block = payloads[finding_index_in_path] if finding_index_in_path < len(payloads) else None
        else:
            exploitation_block = exploitation_payload
        validator_payload = path_shared_state.get("validator")
        if isinstance(validator_payload, dict) and "findings" in validator_payload:
            payloads = validator_payload.get("findings") or []
            validator_block = payloads[finding_index_in_path] if finding_index_in_path < len(payloads) else None
        else:
            validator_block = validator_payload
        debate_block = None
        validator_debates = path_shared_state.get("validator_debates")
        if isinstance(validator_debates, dict):
            debate_key = f"{unit.path.path_fingerprint}::f{finding_index_in_path}"
            candidate = validator_debates.get(debate_key)
            if isinstance(candidate, dict):
                debate_block = candidate
        finding_payload = {
            "finding_id": finding.finding_id,
            "finding_name": finding.finding_name,
            "finding_description": finding.finding_description,
            "confidence_level": finding.confidence_level.value,
            "context": finding.context,
            "business_context": finding.business_context,
            "path_fingerprint": finding.path_fingerprint,
            "function_names": list(finding.function_names),
            "suspect_function_id": finding.suspect_function_id,
            "suspect_line": finding.suspect_line,
        }
        payload = render_coder_payload(
            finding=finding_payload,
            path_context=path_context,
            analyzer=analyzer_block if isinstance(analyzer_block, dict) else None,
            exploitation=exploitation_block if isinstance(exploitation_block, dict) else None,
            validator=validator_block if isinstance(validator_block, dict) else None,
            validator_debate=debate_block,
        )
        self.coder_dispatcher.submit(
            finding.finding_id, payload, on_settled=on_settled,
        )
        if self.logger is not None:
            self.logger.debug_kv("Coder task submitted", finding_id=finding.finding_id)
        return replace(finding, coder_status=CODER_STATUS_PENDING)

    def _make_stream_callback(
        self,
        *,
        findings: list[Finding],
        pending_coder: set[str],
        on_progress: Callable[[AuditRun], None] | None,
        snapshot_factory: Callable[[], AuditRun],
        on_finding_upsert: Callable[[Finding], None] | None = None,
    ) -> Callable[[str, CoderResult], None]:
        """Build the per-finding ``on_settled`` callback for the dispatcher.

        Fires from a worker thread when a coder task settles. Updates
        the in-place ``findings`` list and notifies the report sinks of
        the changed finding so the portal / Markdown see the verdict the
        moment it lands. When ``on_finding_upsert`` is supplied we send
        only the changed row (per-row UPSERT, O(N) total writes); the
        legacy ``on_progress(snapshot_factory())`` path is kept as a
        fallback so existing callers / unit tests that have not migrated
        yet keep working.

        Thread safety:
        - ``findings[idx] = ...`` is atomic under the GIL.
        - ``pending_coder.discard`` is atomic under the GIL.
        - ``on_finding_upsert`` / ``on_progress`` both go through
          ``ReportSinkBus``, which holds its own ``threading.Lock`` so
          concurrent worker settlements serialise into the sinks.
        """

        def _on_settled(finding_id: str, result: CoderResult) -> None:
            # If the workflow is in its cancellation handler, override
            # whatever the worker produced (almost certainly an
            # Inconclusive transport error from the SIGTERM we just
            # sent its claude subprocess) with a clean Skipped verdict.
            # Without this snap, late-firing worker callbacks race
            # `_mark_pending_coder_skipped` and the portal sees a
            # confusing mix of "Skipped (cancelled)" and "Inconclusive
            # (transport error: exit -15)" for findings of the same
            # cancelled run.
            if self._cancelling.is_set():
                result = CoderResult(
                    status=CODER_STATUS_SKIPPED,
                    analysis="",
                    reason="cancelled by user",
                    evidence=(),
                    cli_exit_code=None,
                    cli_stderr=None,
                    duration_ms=None,
                )
            index_by_id = {f.finding_id: idx for idx, f in enumerate(findings)}
            idx = index_by_id.get(finding_id)
            if idx is None:
                if self.logger is not None:
                    self.logger.warning(
                        f"Coder result for unknown finding {finding_id}; ignoring"
                    )
                return
            findings[idx] = self._apply_coder_verdict(findings[idx], result)
            pending_coder.discard(finding_id)
            if self.logger is not None:
                self.logger.debug_kv(
                    "Coder verdict streamed",
                    finding_id=finding_id,
                    status=result.status,
                )
            if on_finding_upsert is not None:
                try:
                    on_finding_upsert(findings[idx])
                except Exception as exc:  # noqa: BLE001 - observer must not break worker
                    # RunDeletedExternallyError is a terminal abort
                    # signal; forward it so the workflow exits cleanly
                    # instead of logging a warning and continuing to
                    # waste coder verifications against a deleted run.
                    if _is_run_deleted_externally(exc):
                        raise
                    if self.logger is not None:
                        self.logger.warning(
                            f"Per-finding upsert for streamed coder verdict failed: {exc}"
                        )
            elif on_progress is not None:
                try:
                    on_progress(snapshot_factory())
                except Exception as exc:  # noqa: BLE001 - observer must not break worker
                    if _is_run_deleted_externally(exc):
                        raise
                    if self.logger is not None:
                        self.logger.warning(
                            f"Snapshot emission for streamed coder verdict failed: {exc}"
                        )

        return _on_settled

    @staticmethod
    def _apply_coder_verdict(finding: Finding, result: CoderResult) -> Finding:
        return replace(
            finding,
            coder_status=result.status,
            coder_analysis=result.analysis,
            coder_reason=result.reason,
            coder_call_chain_evidence=tuple(result.evidence),
        )

    def _drain_coder_results(
        self,
        *,
        findings: list[Finding],
        pending_coder: set[str],
        block_until_empty: bool = False,
        on_progress: Callable[[AuditRun], None] | None = None,
        snapshot_factory: Callable[[], AuditRun] | None = None,
        on_finding_upsert: Callable[[Finding], None] | None = None,
    ) -> bool:
        """Drain completed coder tasks; return True iff any landed.

        When ``block_until_empty`` is true, delegates to
        ``CoderDispatcher.drain`` so the wait blocks on the executor's
        condition variable instead of busy-polling the workflow thread.
        Each settlement updates the matching finding in ``findings``
        (in-place by index) and notifies the report sinks of the changed
        finding. When ``on_finding_upsert`` is supplied we publish only
        the changed row (per-row UPSERT); otherwise the legacy snapshot
        emit fires for back-compat.
        """

        if self.coder_dispatcher is None:
            return False
        any_settled = False

        def _apply(finding_id: str, result: CoderResult) -> None:
            nonlocal any_settled
            pending_coder.discard(finding_id)
            index_by_id = {f.finding_id: idx for idx, f in enumerate(findings)}
            idx = index_by_id.get(finding_id)
            if idx is None:
                if self.logger is not None:
                    self.logger.warning(
                        f"Coder result for unknown finding {finding_id}; ignoring"
                    )
                return
            findings[idx] = self._apply_coder_verdict(findings[idx], result)
            any_settled = True
            if self.logger is not None:
                self.logger.debug_kv(
                    "Coder verdict applied",
                    finding_id=finding_id,
                    status=result.status,
                )
            if on_finding_upsert is not None:
                try:
                    on_finding_upsert(findings[idx])
                except Exception as exc:  # noqa: BLE001 - observer must not break drain
                    if _is_run_deleted_externally(exc):
                        raise
                    if self.logger is not None:
                        self.logger.warning(
                            f"Per-finding upsert after coder verdict failed: {exc}"
                        )
            elif on_progress is not None and snapshot_factory is not None:
                try:
                    on_progress(snapshot_factory())
                except Exception as exc:  # noqa: BLE001 - observer must not break drain
                    if _is_run_deleted_externally(exc):
                        raise
                    if self.logger is not None:
                        self.logger.warning(
                            f"Snapshot emission after coder verdict failed: {exc}"
                        )

        # Always drain anything that has already settled.
        for finding_id, result in self.coder_dispatcher.poll_completed():
            _apply(finding_id, result)
        if block_until_empty:
            # Delegate the wait to the dispatcher so we block on the
            # executor instead of spinning here. Bounded by
            # ``audit.coder.shutdown_timeout_seconds`` so a stalled
            # coder transport cannot block run completion forever; the
            # dispatcher also exits the moment the run-level
            # cancellation event is set.
            self.coder_dispatcher.drain(
                timeout=float(self.config.audit.coder_shutdown_timeout_seconds),
                on_settled=_apply,
            )
        return any_settled

    def _mark_pending_coder_skipped(
        self,
        *,
        findings: list[Finding],
        pending_coder: set[str],
        reason: str,
    ) -> None:
        if not pending_coder:
            return
        index_by_id = {finding.finding_id: idx for idx, finding in enumerate(findings)}
        for finding_id in list(pending_coder):
            idx = index_by_id.get(finding_id)
            if idx is None:
                pending_coder.discard(finding_id)
                continue
            findings[idx] = replace(
                findings[idx],
                coder_status=CODER_STATUS_SKIPPED,
                coder_analysis="",
                coder_reason=reason,
                coder_call_chain_evidence=(),
            )
            pending_coder.discard(finding_id)
