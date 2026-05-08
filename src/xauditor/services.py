from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from xauditor.audit.planner import stream_audit_units
from xauditor.audit.streamer import PathStreamer
from xauditor.audit.coder import startup_log_line
from xauditor.audit.preflight import check_coder_runtime
from xauditor.audit._cancellation import RunCancellation
from xauditor.audit.workflow import AuditWorkflow
from xauditor.config import XAuditorConfig, load_config
from xauditor.errors import LLMError, UserCancelledError, XAuditorError
from xauditor.graph.builder import LangChainGraphBuilder
from xauditor.graph.cache import FileStateStore, GRAPH_ENRICHMENT_STAGE, InMemoryStateStore
from xauditor.graph.fingerprint import compute_build_fingerprint
from xauditor.graph.scope import resolve_repository_scope
from xauditor.integrations.docker import DockerManager, InMemoryDockerManager
from xauditor.integrations.lsp import LanguageServerRegistry
from xauditor.integrations.neo4j import InMemoryNeo4jAdapter, Neo4jAdapter
from xauditor.integrations.neo4j_driver import Neo4jDriver
from xauditor.integrations.audit_source_neo4j import Neo4jAuditGraphSource
from xauditor.integrations.neo4j_repository import InMemoryNeo4jGraphRepository, Neo4jGraphRepository
from xauditor.integrations.coder import (
    CoderRuntimeManager,
    CoderRuntimeStatus,
    InMemoryCoderRuntimeManager,
    endpoint_is_local,
)
from xauditor.integrations.portal import (
    InMemoryPortalRuntimeManager,
    PortalRuntimeManager,
    PortalStatus,
)
from xauditor.integrations.reportdb import (
    InMemoryPostgresContainerManager,
    PostgresContainerManager,
)
from xauditor.model_factory import ensure_provider_runtime_available, missing_temperature_warning
from xauditor.models import AuditRun, GraphBuildStatus, ValidationStatus
from xauditor.reporting.sinks import (
    ProgressEvent,
    ReportSinkBus,
    RunMeta,
)
from xauditor.runtime_logging import RuntimeLogger
from xauditor.runtime import RuntimeLayout


# Wire format for the resume_state JSONB column. Bumped when the inner
# shape changes in a way that breaks readers.
RESUME_STATE_SCHEMA_VERSION = 1


def _default_coder_health_probe(coder_cfg) -> dict:
    """Probe ``GET <coder.endpoint>/health`` and return the JSON body.

    Used by both the lifecycle status verb and ``init`` / ``start``'s
    readiness wait. Lives at module level so the test double doesn't
    inherit it (in-memory tests don't actually probe).

    Wraps httpx transport errors as :class:`PreflightError` so the
    init-time retry loop in
    :meth:`xauditor.integrations.coder.runtime.CoderRuntimeManager._wait_until_healthy`
    treats a transient connection failure (e.g. RST during container
    startup) as a normal "not ready yet" condition rather than a fatal
    error. Without this wrapping, a single ``httpx.ReadError`` from the
    very first probe would bubble straight through the retry catch
    (``httpx.HTTPError`` is not an ``OSError`` subclass) and kill
    ``xauditor coder init`` on otherwise-healthy containers.
    """

    import httpx
    from xauditor.errors import PreflightError

    endpoint = (coder_cfg.endpoint or "").strip()
    timeout = httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0)
    headers = {"Accept": "application/json"}
    if endpoint.startswith("unix://"):
        socket_path = endpoint[len("unix://"):]
        kwargs = {
            "base_url": "http://localhost",
            "transport": httpx.HTTPTransport(uds=socket_path),
        }
    else:
        kwargs = {"base_url": endpoint.rstrip("/")}
    try:
        with httpx.Client(headers=headers, timeout=timeout, **kwargs) as client:
            resp = client.get("/health")
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise PreflightError(f"coder /health probe failed: {exc}") from exc


def _rate_limited_heartbeat(sink, *, interval_seconds: float):
    """Wrap a ProgressEvent consumer with a rate limiter for `progress` kind.

    Events whose `heartbeat_kind` is anything other than `progress`
    (started / stage_completed / finished / failed) always pass through. A
    `progress` event is dropped if the previous `progress` event fired less
    than ``interval_seconds`` ago.
    """

    import time

    state = {"last": 0.0}

    def emit(event):
        if event.heartbeat_kind != "progress":
            sink(event)
            return
        now = time.monotonic()
        if now - state["last"] < interval_seconds:
            return
        state["last"] = now
        sink(event)

    return emit


def _audit_run_for_persistence(
    audit_run: AuditRun, *, persist_false_positives: bool
) -> AuditRun:
    """Return ``audit_run`` minus its False-Positive findings unless
    ``persist_false_positives`` is true.

    Implements the ``short-circuit-validator-fp`` persistence
    boundary: FPs stay in the in-memory snapshot (so coverage and
    per-stage counts remain accurate) but are dropped from the Neo4j
    ``persist_audit_run`` payload by default. Operators who want
    validator-quality triage from reportdb / Neo4j set
    ``audit.persist_false_positives: true``.
    """

    if persist_false_positives:
        return audit_run
    return AuditRun(
        build_fingerprint=audit_run.build_fingerprint,
        findings=tuple(
            f
            for f in audit_run.findings
            if f.validation_status != ValidationStatus.FALSE_POSITIVE
        ),
        coverage=audit_run.coverage,
        checkpoints=audit_run.checkpoints,
        shared_state=audit_run.shared_state,
    )


def _run_reportdb_migrations_if_available(
    config: XAuditorConfig, *, logger: RuntimeLogger | None
) -> None:
    """Run Alembic migrations when the portal package exposes them, else skip.

    The portal package is optional; when it is not installed, `reportdb init`
    still brings up the container, and migrations run at first portal start.
    If the portal is installed but the database is not yet reachable (for
    example, in-memory test doubles, or a container that has not bound the
    expected host port), the failure is logged and swallowed so that
    `reportdb init` is not blocked by DB connectivity. A later `xauditor-portal
    migrate` run will advance the schema when the DB is actually ready.
    """

    try:
        from xauditor_portal.db.migrations import upgrade_to_head
    except ImportError:
        if logger is not None:
            logger.info(
                "xauditor-portal is not installed; skipping report database migrations. "
                "Migrations will run when the portal starts."
            )
        return
    try:
        upgrade_to_head(config.reportdb)
    except Exception as exc:  # noqa: BLE001 - graceful degradation; see docstring
        if logger is not None:
            logger.info(
                f"Report database migrations were skipped: {exc.__class__.__name__}: {exc}"
            )


@dataclass
class ApplicationServices:
    repo_root: Path
    env: dict[str, str]
    config: XAuditorConfig
    runtime: RuntimeLayout
    docker: Any
    neo4j: Any
    graph_repository: Any
    state_store: Any
    lsp_registry: LanguageServerRegistry
    reportdb: Any = None
    portal: Any = None
    coder_runtime: Any = None
    # Sink factory: production builds the real Postgres sink; tests
    # inject ``InMemoryReportSink`` so audits run end-to-end without a
    # live Postgres. Set by ``default()`` / ``for_testing()``.
    report_sink_factory: Any = None
    # Resume target lookup: production reads from Postgres; tests
    # currently have no resumable runs, so they get a function that
    # always returns ``None``.
    resume_target_lookup: Any = None
    # Report-DB pre-flight: production probes Postgres connectivity +
    # alembic version (raising ``PreflightError`` on failure); tests
    # bind a no-op so audits run end-to-end without a live DB.
    report_db_preflight: Any = None

    @classmethod
    def default(cls, *, repo_root: Path, env: dict[str, str] | None = None) -> "ApplicationServices":
        merged_env = dict(os.environ)
        if env:
            merged_env.update(env)
        config = load_config(repo_root=repo_root, env=merged_env, require_llm=False)
        runtime = config.runtime.layout.ensure()
        driver = Neo4jDriver(config.graphdb)
        docker = DockerManager(config.graphdb, readiness_probe=driver.verify_connectivity)
        neo4j = Neo4jAdapter(config=config.graphdb, driver=driver)
        reportdb = PostgresContainerManager(config.reportdb)
        portal = PortalRuntimeManager(
            config.portal,
            reportdb_config=config.reportdb,
            graphdb_config=config.graphdb,
        )
        coder_runtime = CoderRuntimeManager(
            config.coder,
            repo_root=repo_root,
            runtime_root=runtime.root_dir,
            http_probe=_default_coder_health_probe,
        )
        from xauditor.audit.preflight import check_report_database
        from xauditor_portal.sinks.postgres_sink import (
            PostgresReportSink,
            fetch_resume_target,
        )

        def _production_sink_factory():
            return PostgresReportSink(config.reportdb)

        def _production_resume_lookup(run_label):
            return fetch_resume_target(config.reportdb, run_label=run_label)

        def _production_report_db_preflight(*, runtime_logger=None):
            check_report_database(config.reportdb, runtime_logger=runtime_logger)

        return cls(
            repo_root=repo_root,
            env=merged_env,
            config=config,
            runtime=runtime,
            docker=docker,
            neo4j=neo4j,
            graph_repository=Neo4jGraphRepository(config=config.graphdb, driver=driver),
            state_store=FileStateStore(runtime),
            lsp_registry=LanguageServerRegistry(),
            reportdb=reportdb,
            portal=portal,
            coder_runtime=coder_runtime,
            report_sink_factory=_production_sink_factory,
            resume_target_lookup=_production_resume_lookup,
            report_db_preflight=_production_report_db_preflight,
        )

    @classmethod
    def for_testing(
        cls,
        *,
        repo_root: Path,
        env: dict[str, str],
        state_store: InMemoryStateStore | None = None,
        portal_package_installed: bool = True,
    ) -> "ApplicationServices":
        config = load_config(repo_root=repo_root, env=env, require_llm=True)
        runtime = config.runtime.layout.ensure()
        docker = InMemoryDockerManager(config.graphdb)
        neo4j = InMemoryNeo4jAdapter()
        reportdb = InMemoryPostgresContainerManager(config.reportdb)
        portal = InMemoryPortalRuntimeManager(
            config.portal,
            portal_package_installed=portal_package_installed,
        )
        coder_runtime = InMemoryCoderRuntimeManager(config.coder)
        from xauditor.reporting.sinks import InMemoryReportSink

        # Tests don't run against a live Postgres, so the sink is a
        # collect-and-drop double, the resume picker has no truth
        # source (always returns ``None``), and the pre-flight is a
        # no-op (production semantics — abort if Postgres unreachable —
        # would make every test that boots the audit pipeline fail).
        def _test_sink_factory():
            return InMemoryReportSink()

        def _test_resume_lookup(run_label):
            return None

        def _test_report_db_preflight(*, runtime_logger=None):
            return None

        return cls(
            repo_root=repo_root,
            env=env,
            config=config,
            runtime=runtime,
            docker=docker,
            neo4j=neo4j,
            graph_repository=InMemoryNeo4jGraphRepository(neo4j),
            state_store=state_store or InMemoryStateStore(),
            lsp_registry=LanguageServerRegistry(),
            reportdb=reportdb,
            portal=portal,
            coder_runtime=coder_runtime,
            report_sink_factory=_test_sink_factory,
            resume_target_lookup=_test_resume_lookup,
            report_db_preflight=_test_report_db_preflight,
        )

    def require_llm_config(self) -> XAuditorConfig:
        self.config = load_config(repo_root=self.repo_root, env=self.env, require_llm=True)
        self.runtime = self.config.runtime.layout.ensure()
        return self.config

    def graphdb_init(self, *, logger: RuntimeLogger | None = None) -> str:
        if logger is not None:
            logger.info(
                f"Initializing managed Neo4j runtime "
                f"(image={self.config.graphdb.image}, container={self.config.graphdb.container_name})"
            )
        result = self.docker.init_runtime()
        if logger is not None:
            logger.info(f"Graph database ready: {result}")
        return result

    def graphdb_start(self, *, logger: RuntimeLogger | None = None) -> str:
        if logger is not None:
            logger.info(f"Starting managed Neo4j container {self.config.graphdb.container_name}")
        result = self.docker.start_runtime()
        if logger is not None:
            logger.info(f"Graph database started: {result}")
        return result

    def graphdb_stop(self, *, logger: RuntimeLogger | None = None) -> str:
        if logger is not None:
            logger.info(f"Stopping managed Neo4j container {self.config.graphdb.container_name}")
        result = self.docker.stop_runtime()
        if logger is not None:
            logger.info(f"Graph database stopped: {result}")
        return result

    def graphdb_reset(self, *, confirmed: bool, logger: RuntimeLogger | None = None) -> list[str]:
        if logger is not None:
            if confirmed:
                logger.info(
                    f"Resetting managed Neo4j runtime "
                    f"(container={self.config.graphdb.container_name}, volume={self.config.graphdb.volume_name})"
                )
            else:
                logger.info("Graph database reset requested without confirmation; nothing will be deleted")
        deleted = self.docker.reset_runtime(confirmed=confirmed)
        if logger is not None:
            if deleted:
                logger.info(f"Graph database reset deleted: {', '.join(deleted)}")
            else:
                logger.info("Graph database reset deleted no managed resources")
        return deleted

    # Report database lifecycle ---------------------------------------------

    def reportdb_init(self, *, logger: RuntimeLogger | None = None) -> str:
        if logger is not None:
            logger.info(
                f"Initializing managed PostgreSQL runtime "
                f"(image={self.config.reportdb.image}, container={self.config.reportdb.container_name})"
            )
        result = self.reportdb.init_runtime()
        if logger is not None:
            logger.info(f"Report database ready: {result}")
        _run_reportdb_migrations_if_available(self.config, logger=logger)
        return result

    def reportdb_start(self, *, logger: RuntimeLogger | None = None) -> str:
        if logger is not None:
            logger.info(
                f"Starting managed PostgreSQL container {self.config.reportdb.container_name}"
            )
        result = self.reportdb.start_runtime()
        if logger is not None:
            logger.info(f"Report database started: {result}")
        return result

    def reportdb_stop(self, *, logger: RuntimeLogger | None = None) -> str:
        if logger is not None:
            logger.info(
                f"Stopping managed PostgreSQL container {self.config.reportdb.container_name}"
            )
        result = self.reportdb.stop_runtime()
        if logger is not None:
            logger.info(f"Report database stopped: {result}")
        return result

    def reportdb_reset(
        self, *, confirmed: bool, logger: RuntimeLogger | None = None
    ) -> list[str]:
        if logger is not None:
            if confirmed:
                logger.info(
                    f"Resetting managed PostgreSQL runtime "
                    f"(container={self.config.reportdb.container_name}, "
                    f"volume={self.config.reportdb.volume_name})"
                )
            else:
                logger.info(
                    "Report database reset requested without confirmation; nothing will be deleted"
                )
        deleted = self.reportdb.reset_runtime(confirmed=confirmed)
        if logger is not None:
            if deleted:
                logger.info(f"Report database reset deleted: {', '.join(deleted)}")
            else:
                logger.info("Report database reset deleted no managed resources")
        return deleted

    # `xauditor init` umbrella ---------------------------------------------

    def init_all(self, *, logger: RuntimeLogger | None = None) -> list[str]:
        """Run graphdb init, reportdb init, portal init, then coder init.

        Honors remote-bypass per database. Skips coder init when:
          - coder.enabled is false, OR
          - coder.transport is "subprocess", OR
          - coder.endpoint resolves to a remote target.

        Portal init runs unconditionally — xauditor 0.5.0+ writes every
        audit run into Postgres, and the portal is the canonical UI for
        triaging persisted runs. Operators who need a headless setup
        can still run individual ``graphdb init`` / ``reportdb init``
        verbs directly and skip ``init`` (the umbrella).

        Each skip emits a one-line status entry naming the reason so the
        operator never wonders whether xauditor forgot a step.
        """

        results: list[str] = []
        graph_remote = self.config.graphdb.remote is not None
        if graph_remote:
            message = (
                "Graph database: remote endpoint configured; skipping managed init."
            )
            if logger is not None:
                logger.info(message)
            results.append(message)
        else:
            results.append(self.graphdb_init(logger=logger))
        report_remote = self.config.reportdb.remote is not None
        if report_remote:
            message = (
                "Report database: remote endpoint configured; skipping managed init."
            )
            if logger is not None:
                logger.info(message)
            results.append(message)
        else:
            results.append(self.reportdb_init(logger=logger))
        # portal step — added in 0.5.0 (Phase 2 of
        # make-postgres-the-canonical-sink). Runs after reportdb so the
        # FastAPI backend's first read sees the freshly-migrated schema.
        if logger is not None:
            logger.info("Portal: initialising managed runtime…")
        results.append(self.portal_init(logger=logger))
        # coder step
        coder_cfg = self.config.coder
        if not coder_cfg.enabled:
            message = "Coder: skipped (coder.enabled is false)."
        elif coder_cfg.transport != "http":
            message = (
                f"Coder: skipped (coder.transport is `{coder_cfg.transport}`; "
                "lifecycle commands manage the HTTP-transport container only)."
            )
        elif not endpoint_is_local(coder_cfg):
            message = (
                f"Coder: skipped (endpoint `{coder_cfg.endpoint}` is remote; "
                "managed by your deployment, not xauditor)."
            )
        else:
            if logger is not None:
                logger.info(message := "Coder: initialising managed runtime…")
            results.append(self.coder_init(logger=logger))
            return results
        if logger is not None:
            logger.info(message)
        results.append(message)
        return results

    # Portal runtime lifecycle ---------------------------------------------

    def portal_init(self, *, logger: RuntimeLogger | None = None) -> str:
        """Build (if missing) → create → start → healthcheck the portal stack.

        Mirrors :meth:`coder_init` and :meth:`graphdb_init` so the four
        runtime managers (graphdb, reportdb, portal, coder) all expose
        the same ``init`` verb for first-time setup.
        """

        if logger is not None:
            logger.info(
                f"Initialising managed portal (network={self.config.portal.network_name}, "
                f"host_port={self.config.portal.host_port})"
            )
        result = self.portal.init_runtime()
        if logger is not None:
            logger.info(f"Portal ready: {result}")
        return result

    def portal_start(self, *, logger: RuntimeLogger | None = None) -> str:
        """Start existing portal containers. Refuses if either is missing.

        Strict counterpart to :meth:`portal_init`; mirrors
        :meth:`coder_start`. Operators call this to bounce a running
        portal — first-time setup goes through ``portal init``.
        """

        if logger is not None:
            logger.info(
                f"Starting managed portal (network={self.config.portal.network_name}, "
                f"host_port={self.config.portal.host_port})"
            )
        result = self.portal.start()
        if logger is not None:
            logger.info(f"Portal started: {result}")
        return result

    def portal_stop(self, *, logger: RuntimeLogger | None = None) -> str:
        if logger is not None:
            logger.info("Stopping managed portal containers")
        result = self.portal.stop()
        if logger is not None:
            logger.info(f"Portal stopped: {result}")
        return result

    def portal_reset(
        self, *, confirmed: bool, logger: RuntimeLogger | None = None
    ) -> list[str]:
        if logger is not None:
            if confirmed:
                logger.info(
                    "Resetting managed portal runtime (removes containers, images, network)"
                )
            else:
                logger.info(
                    "Portal reset requested without confirmation; nothing will be deleted"
                )
        deleted = self.portal.reset(confirmed=confirmed)
        if logger is not None:
            if deleted:
                logger.info(f"Portal reset deleted: {', '.join(deleted)}")
            else:
                logger.info("Portal reset deleted no managed resources")
        return deleted

    def portal_status(self, *, logger: RuntimeLogger | None = None) -> PortalStatus:
        return self.portal.status()

    # Coder runtime lifecycle ----------------------------------------------

    def coder_build(
        self, *, push: bool = False, logger: RuntimeLogger | None = None
    ) -> str:
        self._ensure_coder_runtime_applicable()
        if logger is not None:
            logger.info(
                f"Building coder image (image={self.config.coder.container_image}, push={push})"
            )
        result = self.coder_runtime.build(push=push)
        if logger is not None:
            logger.info(f"Coder image built: {result}")
        return result

    def coder_init(self, *, logger: RuntimeLogger | None = None) -> str:
        self._ensure_coder_runtime_applicable()
        if logger is not None:
            logger.info(
                f"Initialising coder runtime (image={self.config.coder.container_image}, "
                f"container={self.config.coder.container_name}, "
                f"endpoint={self.config.coder.endpoint})"
            )
        result = self.coder_runtime.init_runtime()
        if logger is not None:
            logger.info(result)
        return result

    def coder_start(self, *, logger: RuntimeLogger | None = None) -> str:
        self._ensure_coder_runtime_applicable()
        if logger is not None:
            logger.info(
                f"Starting coder container `{self.config.coder.container_name}`"
            )
        result = self.coder_runtime.start_runtime()
        if logger is not None:
            logger.info(result)
        return result

    def coder_stop(self, *, logger: RuntimeLogger | None = None) -> str:
        self._ensure_coder_runtime_applicable()
        if logger is not None:
            logger.info(
                f"Stopping coder container `{self.config.coder.container_name}`"
            )
        result = self.coder_runtime.stop_runtime()
        if logger is not None:
            logger.info(result)
        return result

    def coder_reset(
        self, *, confirmed: bool, logger: RuntimeLogger | None = None
    ) -> list[str]:
        self._ensure_coder_runtime_applicable()
        if logger is not None:
            if confirmed:
                logger.info(
                    "Resetting coder runtime (removes container, image, socket file)"
                )
            else:
                logger.info(
                    "Coder reset requested without confirmation; nothing will be deleted"
                )
        deleted = self.coder_runtime.reset_runtime(confirmed=confirmed)
        if logger is not None:
            if deleted:
                logger.info(f"Coder reset deleted: {', '.join(deleted)}")
            else:
                logger.info("Coder reset deleted no managed resources")
        return deleted

    def coder_status(
        self, *, logger: RuntimeLogger | None = None
    ) -> CoderRuntimeStatus:
        # status is informative-only — applicable verb that works for
        # subprocess transport, disabled coder, AND remote endpoints.
        return self.coder_runtime.runtime_status()

    def _ensure_coder_runtime_applicable(self) -> None:
        """Lifecycle verbs (except status) require coder.transport: http."""

        if not self.config.coder.enabled:
            raise XAuditorError(
                "Coder verification is disabled (coder.enabled: false). "
                "The lifecycle commands manage the HTTP-transport service "
                "container only; enable coder first."
            )
        if self.config.coder.transport != "http":
            raise XAuditorError(
                f"coder.transport is `{self.config.coder.transport}`. "
                "The lifecycle commands manage the HTTP-transport service "
                "container only; set coder.transport: http to use them."
            )

    def build_graph(
        self,
        *,
        excludes: tuple[str, ...],
        force: bool,
        logger: RuntimeLogger | None = None,
    ) -> tuple[str, GraphBuildStatus]:
        """Run the graph build pipeline. Returns the build fingerprint
        plus a ``GraphBuildStatus`` (completed / resumed / reused).

        0.9.0: returns ``str`` (the fingerprint), not a ``GraphBuildResult``.
        Records live in Neo4j post-canonical_finalize; downstream callers
        construct ``Neo4jAuditGraphSource`` from the fingerprint to read
        them back.
        """

        if logger is not None:
            logger.info("Starting graph build")
        if hasattr(self.neo4j, "set_logger"):
            self.neo4j.set_logger(logger)
        config = self.require_llm_config()
        if config.graph.build.enable_llm_enrichment:
            provider_name = config.llm.selected_provider("graph_builder") or "default"
            ensure_provider_runtime_available(
                config.llm.provider_for("graph_builder"),
                provider_name=provider_name,
            )
        scope = resolve_repository_scope(self.repo_root, excludes or config.repository.excludes)
        fingerprint = compute_build_fingerprint(scope, config, workflow_version=config.llm.graph_workflow_version)
        if logger is not None:
            logger.info(
                f"Resolved repository scope: {len(scope.included_files)} included, {len(scope.excluded_files)} excluded"
            )
            logger.debug(f"Graph build fingerprint: {fingerprint}")
            logger.debug_kv(
                "Graph build context",
                fingerprint=fingerprint,
                force=force,
                included_files=len(scope.included_files),
                excluded_files=len(scope.excluded_files),
            )
        if not force and self.state_store.is_build_reusable(fingerprint):
            if self._neo4j_has_build(fingerprint):
                if logger is not None:
                    logger.info(
                        f"Graph build already completed for {fingerprint}; skipping rebuild. "
                        f"Pass --force to rebuild."
                    )
                return fingerprint, GraphBuildStatus.REUSED
            # Neo4j was wiped but the per-stage cache is intact —
            # clear the canonical-complete flag so the rebuild re-runs
            # canonical_finalize and re-streams records into Neo4j.
            self.state_store.clear_build(fingerprint)
            if logger is not None:
                logger.info(
                    f"Graph build {fingerprint} cache present but missing in Neo4j; "
                    "rebuilding canonical stage."
                )
        if force:
            if logger is not None:
                logger.info(f"Forcing graph rebuild for {fingerprint}")
            self.state_store.clear_build(fingerprint)
        self.neo4j.ping()
        self.neo4j.bootstrap_schema()
        resumed_from_checkpoint = self.state_store.has_started_build(fingerprint)
        if resumed_from_checkpoint and logger is not None:
            logger.info(f"Resuming graph build from checkpoint: {fingerprint}")
            logger.debug_kv("Graph build resumed", fingerprint=fingerprint)

        if not self.state_store.is_final_build_ready(fingerprint):
            builder = LangChainGraphBuilder.from_config(
                config,
                repository=self.graph_repository,
                lsp_registry=self.lsp_registry,
                logger=logger,
            )
            builder.build(
                repo_root=self.repo_root,
                scope=scope,
                build_fingerprint=fingerprint,
                state_store=self.state_store,
            )
        if logger is not None:
            logger.info("Completed graph build stage: canonical_finalize")
            logger.debug(f"Streamed graph build to Neo4j for {fingerprint}")
        if config.graph.build.enable_llm_enrichment and not self.state_store.is_stage_complete(
            fingerprint, GRAPH_ENRICHMENT_STAGE
        ):
            try:
                self._run_graph_enrichment(fingerprint, logger=logger)
            except XAuditorError:
                raise
            except Exception as exc:
                raise LLMError(str(exc), operation="graph_enrichment") from exc
            self.state_store.mark_graph_enrichment_complete(fingerprint)
            if logger is not None:
                logger.info("Completed graph build stage: graph_enrichment")
        status = GraphBuildStatus.RESUMED if resumed_from_checkpoint else GraphBuildStatus.COMPLETED
        if logger is not None:
            logger.info(f"Graph build {status.value}: {fingerprint}")
        return fingerprint, status

    def _neo4j_has_build(self, fingerprint: str) -> bool:
        try:
            self.graph_repository.require_build(fingerprint)
        except Exception:
            return False
        return True

    def list_graph_builds(self):
        return self.graph_repository.list_builds(self.repo_root)

    def resolve_build(self, build_fingerprint: str | None = None) -> str:
        if build_fingerprint is not None:
            self.graph_repository.require_build(build_fingerprint)
            return build_fingerprint
        return self.graph_repository.get_latest_build(self.repo_root)

    def find_functions(self, build_fingerprint: str, query: str):
        return self.graph_repository.find_functions(build_fingerprint, query)

    def get_call_stack(self, build_fingerprint: str, function_id: str):
        return self.graph_repository.get_call_stack(build_fingerprint, function_id)

    def list_graph_paths(self, build_fingerprint: str):
        return self.graph_repository.list_paths(build_fingerprint)

    def run_audit(
        self,
        *,
        build_fingerprint: str | None = None,
        logger: RuntimeLogger | None = None,
        cancellation: RunCancellation | None = None,
    ) -> tuple[AuditRun, str]:
        """Run an audit. Returns the AuditRun and its persisted run label.

        The Markdown sink is gone (Phase 2 of make-postgres-the-canonical-sink);
        operators export reports on demand via ``xauditor audit export
        <run_label>``. The run label is the per-invocation
        ``YYYYMMDD-HHMMSS`` timestamp, also stored as
        ``audit_runs.report_dir`` in Postgres.
        """

        if hasattr(self.neo4j, "set_logger"):
            self.neo4j.set_logger(logger)
        config = self.require_llm_config()
        if self.report_db_preflight is not None:
            self.report_db_preflight(runtime_logger=logger)
        auditor_provider_name, validator_provider_name = self._ensure_audit_providers(
            config, logger=logger
        )
        coder_preflight = self._coder_preflight(config, logger=logger)
        if build_fingerprint is None:
            build_fingerprint = self.graph_repository.get_latest_build(self.repo_root)
        else:
            self.graph_repository.require_build(build_fingerprint)
        source = Neo4jAuditGraphSource(
            repository=self.graph_repository,
            build_fingerprint=build_fingerprint,
            repo_root=self.repo_root,
        )
        if logger is not None:
            logger.info(f"Starting audit for {source.build_fingerprint}")
        streamer = stream_audit_units(
            source, batch_size=config.audit.path_batch_size
        )
        if logger is not None:
            logger.info(
                f"Planned {streamer.total} audit paths "
                f"(streaming, batch_size={streamer.batch_size})"
            )

        run_label = datetime.now().strftime("%Y%m%d-%H%M%S")
        sink_bus = ReportSinkBus(self._build_postgres_sink(logger=logger))
        run_meta = self._build_run_meta(
            source=source,
            run_label=run_label,
            config=config,
            providers=(auditor_provider_name, validator_provider_name, coder_preflight),
            resumed=False,
        )
        sink_bus.open_run(run_meta)
        sink_bus.write_progress(
            ProgressEvent(stage="reporting", heartbeat_kind="started", message="audit started")
        )

        return self._drive_workflow(
            config=config,
            source=source,
            streamer=streamer,
            sink_bus=sink_bus,
            run_label=run_label,
            skip_paths=frozenset(),
            prior_shared_state=None,
            logger=logger,
            cancel_message="cancelled by user",
            cancellation=cancellation,
        )

    # ------------------------------------------------------------------ resume

    def resume_audit(
        self,
        *,
        run_id: str | None = None,
        build_fingerprint: str | None = None,
        logger: RuntimeLogger | None = None,
        cancellation: RunCancellation | None = None,
    ) -> tuple[AuditRun, str]:
        """Resume a previously-interrupted audit run from the report database.

        ``run_id`` is the run label (the ``YYYYMMDD-HHMMSS`` string stored
        in ``audit_runs.report_dir``). When ``None``, the most recent
        failed / cancelled / in-progress row is used.

        Raises ``XAuditorError`` when no resumable run is found or when
        the row's ``resume_state`` column is NULL (indicating a run
        started on xauditor 0.4.x or earlier — operators must re-run
        from scratch since the legacy ``<report_dir>/resume-state.json``
        file is no longer consulted).
        """

        if self.report_db_preflight is not None:
            self.report_db_preflight(runtime_logger=logger)
        if self.resume_target_lookup is None:
            from xauditor_portal.sinks.postgres_sink import fetch_resume_target

            target = fetch_resume_target(self.config.reportdb, run_label=run_id)
        else:
            target = self.resume_target_lookup(run_id)
        if target is None:
            if run_id is not None:
                raise XAuditorError(
                    f"Run id {run_id!r} was not found in the report database. "
                    "Use `xauditor audit list` (or check the portal) to discover "
                    "available run labels."
                )
            raise XAuditorError(
                "No resumable audit runs (failed / cancelled / in_progress) "
                "were found in the report database. Run `xauditor audit run` "
                "to start a fresh audit."
            )
        if target.status == "completed":
            if logger is not None:
                logger.info(
                    f"Run {target.run_label} already completed; nothing to resume."
                )
            return self._build_completed_audit_run(target), target.run_label
        if target.resume_state is None:
            raise XAuditorError(
                f"Run {target.run_label} has no in-DB resume state. It was "
                "started on xauditor 0.4.x (which kept resume state in "
                "<report_dir>/resume-state.json on disk) and cannot be "
                "resumed by xauditor 0.5.0+. Please re-run from scratch with "
                "`xauditor audit run`."
            )
        if (
            build_fingerprint is not None
            and build_fingerprint != target.build_fingerprint
        ):
            raise XAuditorError(
                f"Requested --build {build_fingerprint} does not match the "
                f"resumed run's recorded fingerprint {target.build_fingerprint}."
            )

        config = self.require_llm_config()
        auditor_provider_name, validator_provider_name = self._ensure_audit_providers(
            config, logger=logger
        )
        coder_preflight = self._coder_preflight(config, logger=logger)
        self.graph_repository.require_build(target.build_fingerprint)
        source = Neo4jAuditGraphSource(
            repository=self.graph_repository,
            build_fingerprint=target.build_fingerprint,
            repo_root=self.repo_root,
        )
        prior_shared_state = dict(target.resume_state.get("shared_state") or {})
        if logger is not None:
            logger.info(
                f"Resuming audit {target.run_label} for {target.build_fingerprint} "
                f"({len(prior_shared_state)} paths already recorded)"
            )
        streamer = stream_audit_units(
            source, batch_size=config.audit.path_batch_size
        )

        sink_bus = ReportSinkBus(self._build_postgres_sink(logger=logger))
        run_meta = self._build_run_meta(
            source=source,
            run_label=target.run_label,
            config=config,
            providers=(auditor_provider_name, validator_provider_name, coder_preflight),
            resumed=True,
        )
        sink_bus.open_run(run_meta)
        sink_bus.write_progress(
            ProgressEvent(
                stage="reporting",
                heartbeat_kind="resumed",
                message="audit resumed",
            )
        )

        return self._drive_workflow(
            config=config,
            source=source,
            streamer=streamer,
            sink_bus=sink_bus,
            run_label=target.run_label,
            skip_paths=frozenset(prior_shared_state.keys()),
            prior_shared_state=prior_shared_state,
            logger=logger,
            cancel_message="cancelled by user during resume",
            cancellation=cancellation,
        )

    # ------------------------------------------------------------------ shared

    def _ensure_audit_providers(
        self, config: XAuditorConfig, *, logger: RuntimeLogger | None
    ) -> tuple[str, str]:
        """Resolve + warm the auditor / validator LLM provider runtimes."""

        auditor_provider_name = config.llm.selected_provider("auditor") or "default"
        ensure_provider_runtime_available(
            config.llm.provider_for("auditor"),
            provider_name=auditor_provider_name,
        )
        validator_provider_name = config.llm.selected_provider("validator") or "default"
        ensure_provider_runtime_available(
            config.llm.provider_for("validator"),
            provider_name=validator_provider_name,
        )
        if logger is not None:
            warning = missing_temperature_warning(config.llm)
            if warning is not None:
                logger.warning(warning)
        return auditor_provider_name, validator_provider_name

    def _coder_preflight(
        self, config: XAuditorConfig, *, logger: RuntimeLogger | None
    ):
        coder_preflight = check_coder_runtime(
            config.coder,
            env=self.env,
            coder_runtime=self.coder_runtime,
            runtime_logger=logger,
            repo_root=self.repo_root,
        )
        if coder_preflight is not None and logger is not None:
            logger.info(
                f"Coder verification enabled (cli={coder_preflight.resolved_path}, "
                f"version={coder_preflight.version or 'unknown'})"
            )
            posture_line = startup_log_line(config.coder)
            if posture_line is not None:
                logger.info(posture_line)
        return coder_preflight

    def _build_run_meta(
        self,
        *,
        source: Neo4jAuditGraphSource,
        run_label: str,
        config: XAuditorConfig,
        providers: tuple[str, str, Any],
        resumed: bool,
    ) -> RunMeta:
        auditor_provider_name, validator_provider_name, coder_preflight = providers
        # The mode literal persisted onto ``audit_runs.mode`` is the
        # canonical post-rename value (`"fast"` / `"deep"`). Migration
        # 0011 has already rewritten any historical `"single"` /
        # `"team"` rows in place. Read from ``config.audit_mode.mode``
        # rather than the legacy ``config.teaming.enabled`` flag —
        # operators using the legacy yaml shape have already had
        # their config translated by ``_migrate_legacy_teaming`` at
        # ``load_config`` time.
        audit_mode = getattr(config, "audit_mode", None)
        mode_label = audit_mode.mode if audit_mode is not None else "fast"
        providers_used: dict[str, Any] = {
            "auditor": auditor_provider_name,
            "validator": validator_provider_name,
        }
        if coder_preflight is not None:
            # Flat display string composed by the preflight result —
            # matches the auditor / validator entries' shape so the
            # portal's "LLM providers" header renders all three
            # entries homogeneously: ``"claude-code <version>, <model>"``.
            providers_used["coder"] = coder_preflight.as_provider_summary(
                model_name=self.config.coder.model_name,
            )
        # Phase 5A: compute the CoverageGaps payload at run start
        # so the sink (Postgres) can persist it the moment the run
        # row is opened. `compute_coverage_gaps` is data-only and
        # cheap (~microseconds), no LLM, no Neo4j.
        coverage_gaps_payload: dict[str, Any] | None = None
        if audit_mode is not None and audit_mode.coverage_gaps_report:
            from xauditor.coverage_gaps import compute_coverage_gaps

            coverage_gaps_payload = compute_coverage_gaps(
                mode=mode_label
            ).to_payload()

        stages_form = (
            audit_mode.stages_form if audit_mode is not None else "prompt"
        )

        return RunMeta(
            repo_root=str(self.repo_root),
            project_name=self.repo_root.name,
            build_fingerprint=source.build_fingerprint,
            mode=mode_label,
            run_label=run_label,
            started_at=datetime.now(),
            llm_providers_used=providers_used,
            resumed=resumed,
            coverage_gaps=coverage_gaps_payload,
            stages_form=stages_form,
        )

    def _build_postgres_sink(self, *, logger: RuntimeLogger | None):
        """Build the production sink (or the test double when running in tests).

        Production wiring builds ``PostgresReportSink`` directly via
        ``self.report_sink_factory`` (set by ``default()``). Tests get
        ``InMemoryReportSink`` via ``for_testing()``. Connectivity /
        schema preflight runs separately in ``audit/preflight.py``;
        if it passed, sink construction is expected to succeed.
        """

        if self.report_sink_factory is None:
            # Backwards compatibility for callers that constructed
            # ApplicationServices directly without going through
            # ``default()`` / ``for_testing()`` — fall through to the
            # production factory.
            from xauditor_portal.sinks.postgres_sink import PostgresReportSink

            sink = PostgresReportSink(self.config.reportdb)
        else:
            sink = self.report_sink_factory()
        if logger is not None:
            logger.debug(
                f"Report sink wired ({sink.__class__.__name__}; "
                "xauditor 0.5.0+ — Markdown sink retired)"
            )
        return sink

    def _drive_workflow(
        self,
        *,
        config: XAuditorConfig,
        source: Neo4jAuditGraphSource,
        streamer: PathStreamer,
        sink_bus: ReportSinkBus,
        run_label: str,
        skip_paths: frozenset[str],
        prior_shared_state: dict[str, dict[str, object]] | None,
        logger: RuntimeLogger | None,
        cancel_message: str,
        cancellation: "RunCancellation | None" = None,
    ) -> tuple[AuditRun, str]:
        """Common workflow driver: open / run / finalize for both
        ``run_audit`` and ``resume_audit``.

        Threads the per-row upsert path, the path-boundary snapshot
        emit, and the in-DB resume-state UPDATE through the sink bus.
        """

        workflow = AuditWorkflow.from_config(
            config, logger=logger, cancellation=cancellation
        )
        # Lazy import: keeps the xauditor → xauditor_portal dependency
        # contained to the workflow path that actually persists. If the
        # portal package is not installed (no portal sink in the bus),
        # ``RunDeletedExternallyError`` will simply never be raised.
        try:
            from xauditor_portal.sinks import (
                RunDeletedExternallyError as _RunDeletedExternallyError,
            )
        except ImportError:
            _RunDeletedExternallyError = None  # type: ignore[assignment]
        heartbeat_cb = _rate_limited_heartbeat(
            sink_bus.write_progress, interval_seconds=2.0
        )

        persist_fp = config.audit.persist_false_positives

        def _on_finding_upsert(finding):
            # ``short-circuit-validator-fp``: drop FP findings at the
            # reportdb sink-bus boundary unless the operator opts in
            # via ``audit.persist_false_positives``. The in-memory
            # snapshot is unaffected, so coverage and per-stage counts
            # remain accurate.
            if (
                not persist_fp
                and finding.validation_status == ValidationStatus.FALSE_POSITIVE
            ):
                return
            sink_bus.upsert_finding(run_label, finding)

        def _on_progress(snapshot):
            sink_bus.emit_snapshot(snapshot)
            sink_bus.write_resume_state(
                {
                    "schema_version": RESUME_STATE_SCHEMA_VERSION,
                    "shared_state": dict(snapshot.shared_state or {}),
                }
            )

        # Track whether the workflow's ``on_cancel`` hot-path hook
        # already flushed the "cancelled" portal status — if so, the
        # ``UserCancelledError`` handler below skips the duplicate
        # ``sink_bus.fail_run`` write.
        cancel_state = {"flushed": False}

        def _on_cancel(snapshot, message: str) -> None:
            """Called by ``AuditWorkflow.run`` the instant a
            ``KeyboardInterrupt`` is observed, BEFORE any cleanup
            drain. Flushes the cancellation status to the portal so
            even if the user hammers Ctrl+C and the process gets
            hard-killed during the ensuing drain, the portal already
            shows ``status=cancelled`` instead of being stuck on
            ``in_progress``."""
            try:
                sink_bus.write_progress(
                    ProgressEvent(
                        stage="reporting",
                        heartbeat_kind="failed",
                        message=message,
                    )
                )
                sink_bus.fail_run(status="cancelled", error=message)
                cancel_state["flushed"] = True
            except Exception:  # noqa: BLE001 - best-effort hot-path flush
                if logger is not None:
                    logger.warning(
                        "Failed to flush cancellation status to portal; "
                        "the UserCancelledError handler will retry below."
                    )

        try:
            audit_run = workflow.run(
                source=source,
                streamer=streamer,
                on_progress=_on_progress,
                on_heartbeat=heartbeat_cb,
                on_finding_upsert=_on_finding_upsert,
                on_cancel=_on_cancel,
                skip_paths=skip_paths,
                prior_shared_state=prior_shared_state,
            )
        except UserCancelledError as exc:
            if exc.partial_audit_run is not None:
                self.state_store.save_audit_run(exc.partial_audit_run)
                if logger is not None:
                    logger.debug(
                        f"Saved partial audit state for {source.build_fingerprint} "
                        f"with {len(exc.partial_audit_run.checkpoints)} checkpoints"
                    )
                # Snapshot resume state from the cancellation-time partial
                # so the run is resumable even when the user Ctrl-C'd
                # mid-path (the workflow's per-path on_progress had no
                # chance to fire for the in-flight path).
                sink_bus.write_resume_state(
                    {
                        "schema_version": RESUME_STATE_SCHEMA_VERSION,
                        "shared_state": dict(
                            exc.partial_audit_run.shared_state or {}
                        ),
                    }
                )
            # Belt-and-suspenders: if the workflow's ``on_cancel``
            # hook already flushed status=cancelled to the portal we
            # don't need to repeat. If it failed (sink momentarily
            # unreachable, etc.), retry now — sink layer is
            # idempotent on duplicate fail_run within the same
            # run_label.
            if not cancel_state["flushed"]:
                sink_bus.write_progress(
                    ProgressEvent(
                        stage="reporting",
                        heartbeat_kind="failed",
                        message=cancel_message,
                    )
                )
                sink_bus.fail_run(status="cancelled", error=cancel_message)
            raise
        except Exception as exc:
            # If the run row was deleted from under us by a portal admin,
            # the sink raises ``RunDeletedExternallyError``. Don't try to
            # write progress events or call ``fail_run`` — the row is gone
            # and every write would either no-op silently or crash again.
            # Just log and propagate so the CLI can print a friendly
            # message and exit non-zero.
            if _RunDeletedExternallyError is not None and isinstance(
                exc, _RunDeletedExternallyError
            ):
                if logger is not None:
                    logger.error(str(exc))
                raise
            summary = f"{exc.__class__.__name__}: {exc}"
            sink_bus.write_progress(
                ProgressEvent(
                    stage="reporting",
                    heartbeat_kind="failed",
                    message=summary,
                )
            )
            sink_bus.fail_run(status="failed", error=summary)
            raise

        # ``short-circuit-validator-fp``: when persistence is opted-out,
        # filter FPs out of the Neo4j payload only. The in-memory
        # snapshot (``state_store.save_audit_run`` + ``emit_snapshot``)
        # keeps every finding so resume state and portal SSE updates
        # remain complete; downstream stage counts stay accurate.
        persisted_run = _audit_run_for_persistence(
            audit_run, persist_false_positives=persist_fp
        )
        self.neo4j.persist_audit_run(persisted_run)
        self.state_store.save_audit_run(audit_run)
        sink_bus.emit_snapshot(audit_run)
        # On clean completion the run is no longer resumable; clear the
        # resume_state column so the portal does not show a stale
        # "resumable" badge.
        sink_bus.write_resume_state(
            {"schema_version": RESUME_STATE_SCHEMA_VERSION, "shared_state": {}}
        )
        sink_bus.write_progress(
            ProgressEvent(
                stage="reporting",
                heartbeat_kind="finished",
                message="audit completed",
            )
        )
        sink_bus.close_run(audit_run)
        if logger is not None:
            logger.info(f"Audit completed for {source.build_fingerprint}")
        return audit_run, run_label

    # ------------------------------------------------------------------ export

    def audit_export(
        self,
        *,
        run_label: str,
        fmt: str = "json",
        output_dir: Path | None = None,
        include_debug: bool = False,
        logger: RuntimeLogger | None = None,
    ):
        """Render a persisted audit run as JSON (stdout) or Markdown (files).

        Reads from Postgres only — does not touch the original repo or
        any on-disk run directory (that directory format is gone in
        0.5.0). Apply the same secrets-redaction filter as run-time
        logs so the export never leaks API keys / passwords.
        """

        from xauditor.reporting.exporter import export_audit_run

        # Tests with no live DB skip via a sentinel: the test factory
        # returns ``None`` from resume lookup; an export against an
        # unknown run produces a clear LookupError that the CLI surfaces.
        return export_audit_run(
            config=self.config.reportdb,
            run_label=run_label,
            fmt=fmt,
            output_dir=output_dir,
            include_debug=include_debug,
            logger=logger,
            persist_false_positives=self.config.audit.persist_false_positives,
        )

    @staticmethod
    def _build_completed_audit_run(target) -> AuditRun:
        """Reconstruct a minimal AuditRun for an already-completed resume target.

        ``resume_audit`` is sometimes invoked against a run that finished
        on its own — return a stub AuditRun + the run label so the CLI
        prints a "nothing to resume" message rather than silently re-running.
        """

        from xauditor.models import CoverageInventory

        shared = (
            dict(target.resume_state.get("shared_state") or {})
            if target.resume_state is not None
            else {}
        )
        return AuditRun(
            build_fingerprint=target.build_fingerprint,
            findings=(),
            coverage=CoverageInventory(),
            shared_state=shared,
        )

    def _run_graph_enrichment(
        self,
        build_fingerprint: str,
        logger: RuntimeLogger | None = None,
    ) -> None:
        # 0.9.0: graph enrichment lives outside canonical_finalize. The
        # streaming pipeline already wrote enriched / un-enriched
        # records depending on ``graph.build.enable_llm_enrichment``;
        # this hook is reserved for an eventual second-pass enrichment
        # that decorates ``(:Function)`` / ``(:Class)`` / ``(:Path)``
        # nodes with LLM-derived summaries via UPDATE Cypher.
        del build_fingerprint, logger
