from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from xauditor.config import Neo4jConfig
from xauditor.errors import CircuitOpenError, GraphdbError, Neo4jError, RetryExhaustedError, XAuditorError
from xauditor.models import AuditRun, CoverageInventory
from xauditor.resilience import CircuitBreaker, retry
from xauditor.runtime_logging import RuntimeLogger


_CYPHER_ESCAPES = {
    "\\": "\\\\",
    "'": "\\'",
    "\"": "\\\"",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
    "\0": "\\u0000",
}


def _cypher_quote(value: str) -> str:
    """Escape *value* for use inside a single-quoted Cypher string literal.

    Every character that could close the literal, inject a newline-based
    comment, or embed a control character is escaped — preventing Cypher
    injection from attacker-controlled repository contents (file paths,
    function names, or LLM-provided evidence strings).
    """
    value = str(value)
    return "".join(_CYPHER_ESCAPES.get(ch, ch) for ch in value)


def _scoped_key(build_fingerprint: str, value: str) -> str:
    return f"{build_fingerprint}:{value}"


class Neo4jAdapter:
    graph_batch_size = 1000
    schema_timeout = 30
    graph_import_timeout = 300
    audit_import_timeout = 60

    def __init__(
        self,
        *,
        config: Neo4jConfig,
        driver,
        logger: RuntimeLogger | None = None,
    ) -> None:
        self.config = config
        self.driver = driver
        self.logger = logger
        self.circuit_breaker = CircuitBreaker(service_name="neo4j")

    def set_logger(self, logger: RuntimeLogger | None) -> None:
        self.logger = logger

    def ping(self) -> None:
        self._run_with_resilience(
            "neo4j.ping",
            lambda: self.driver.verify_connectivity(),
        )

    def bootstrap_schema(self) -> None:
        queries = [
            "DROP CONSTRAINT xauditor_repository IF EXISTS;",
            "CREATE CONSTRAINT xauditor_repository IF NOT EXISTS FOR (r:Repository) REQUIRE r.name IS UNIQUE;",
            "CREATE CONSTRAINT xauditor_graph_build IF NOT EXISTS FOR (b:GraphBuild) REQUIRE b.build_fingerprint IS UNIQUE;",
            # Legacy schema versions reused these constraint names for unscoped
            # properties like Module.name and Function.function_id. Replace them
            # in-place so the build-scoped constraints below actually take over.
            "DROP CONSTRAINT xauditor_function IF EXISTS;",
            "CREATE CONSTRAINT xauditor_function IF NOT EXISTS FOR (f:Function) REQUIRE f.function_key IS UNIQUE;",
            "DROP CONSTRAINT xauditor_file IF EXISTS;",
            "CREATE CONSTRAINT xauditor_file IF NOT EXISTS FOR (f:File) REQUIRE f.file_key IS UNIQUE;",
            "DROP CONSTRAINT xauditor_module IF EXISTS;",
            "DROP CONSTRAINT xauditor_class IF EXISTS;",
            "CREATE CONSTRAINT xauditor_class IF NOT EXISTS FOR (c:Class) REQUIRE c.class_key IS UNIQUE;",
            "DROP CONSTRAINT xauditor_class_member IF EXISTS;",
            "CREATE CONSTRAINT xauditor_class_member IF NOT EXISTS FOR (c:ClassMember) REQUIRE c.class_member_key IS UNIQUE;",
            "DROP CONSTRAINT xauditor_path IF EXISTS;",
            "CREATE CONSTRAINT xauditor_path IF NOT EXISTS FOR (p:Path) REQUIRE p.path_key IS UNIQUE;",
            "DROP CONSTRAINT xauditor_module_symbol IF EXISTS;",
            "CREATE CONSTRAINT xauditor_module_symbol IF NOT EXISTS FOR (s:ModuleSymbol) REQUIRE s.symbol_key IS UNIQUE;",
            "DROP CONSTRAINT xauditor_coverage_record IF EXISTS;",
            # Backing indexes for the constraints above populate asynchronously
            # in Neo4j 5.x. Block until every one is ONLINE before returning so
            # the subsequent `persist_graph` MERGE lookups do not race the
            # `Neo.ClientError.Schema.IndexNotFound` ("index is still populating")
            # error that the retry layer otherwise has to paper over.
            "CALL db.awaitIndexes();",
        ]
        self._execute_queries(queries, timeout=self.schema_timeout, operation="neo4j.bootstrap_schema")

    def persist_graph(self, graph: Any) -> None:
        # Legacy bulk-persist entry point. Pre-0.9.0 ``services.build_graph``
        # called this with a fully-assembled ``GraphBuildResult`` after
        # ``canonical_finalize`` returned. In 0.9.0 the streaming
        # ``canonical_finalize`` writes records directly via
        # ``Neo4jGraphRepository.write_*_chunk`` methods, and this
        # legacy method is retained only for older code paths that
        # construct fresh GraphBuild-shaped objects (e.g. test
        # double rehydrations). Production no longer reaches this.
        repository_name = _cypher_quote(graph.repo_root.name or str(graph.repo_root))
        repository_full_path = _cypher_quote(str(graph.repo_root))
        build_fingerprint = _cypher_quote(graph.build_fingerprint)
        queries: list[str] = []
        queries.append(
            f"MERGE (r:Repository {{name: '{repository_name}'}}) "
            f"SET r.path = '{repository_full_path}';"
        )
        queries.append(
            f"MERGE (build:GraphBuild {{build_fingerprint: '{build_fingerprint}'}}) "
            "ON CREATE SET build.created_at = timestamp() "
            f"SET build.repo_path = '{repository_full_path}', build.status = 'ready';"
        )
        queries.append(
            f"MATCH (r:Repository {{name: '{repository_name}'}}), "
            f"(build:GraphBuild {{build_fingerprint: '{build_fingerprint}'}}) "
            "MERGE (r)-[:HAS_BUILD]->(build);"
        )
        for file_record in graph.files:
            file_key = _cypher_quote(_scoped_key(graph.build_fingerprint, file_record.path))
            queries.append(
                f"MERGE (f:File {{file_key: '{file_key}'}}) "
                f"SET f.build_fingerprint = '{build_fingerprint}', "
                f"f.path = '{_cypher_quote(file_record.path)}', "
                f"f.language = '{_cypher_quote(file_record.language)}', "
                f"f.module_name = '{_cypher_quote(file_record.module_name)}';"
            )
            queries.append(
                f"MATCH (build:GraphBuild {{build_fingerprint: '{build_fingerprint}'}}), "
                f"(f:File {{file_key: '{file_key}'}}) "
                "MERGE (build)-[:CONTAINS_FILE]->(f);"
            )
        for class_record in graph.classes:
            class_key = _cypher_quote(_scoped_key(graph.build_fingerprint, class_record.class_id))
            file_key = _cypher_quote(_scoped_key(graph.build_fingerprint, class_record.file_path))
            queries.append(
                f"MERGE (c:Class {{class_key: '{class_key}'}}) "
                f"SET c.build_fingerprint = '{build_fingerprint}', "
                f"c.class_id = '{_cypher_quote(class_record.class_id)}', "
                f"c.name = '{_cypher_quote(class_record.name)}', "
                f"c.file_path = '{_cypher_quote(class_record.file_path)}', "
                f"c.module_name = '{_cypher_quote(class_record.module_name)}', "
                f"c.start_line = {class_record.start_line}, "
                f"c.end_line = {class_record.end_line}, "
                f"c.summary = '{_cypher_quote(class_record.summary)}', "
                f"c.business_context = '{_cypher_quote(class_record.business_context)}';"
            )
            queries.append(
                f"MATCH (f:File {{file_key: '{file_key}'}}), "
                f"(c:Class {{class_key: '{class_key}'}}) "
                "MERGE (f)-[:DECLARES_CLASS]->(c);"
            )
        for class_member in graph.class_members:
            class_member_key = _cypher_quote(_scoped_key(graph.build_fingerprint, class_member.member_id))
            class_key = _cypher_quote(_scoped_key(graph.build_fingerprint, class_member.class_id))
            queries.append(
                f"MERGE (m:ClassMember {{class_member_key: '{class_member_key}'}}) "
                f"SET m.build_fingerprint = '{build_fingerprint}', "
                f"m.member_id = '{_cypher_quote(class_member.member_id)}', "
                f"m.name = '{_cypher_quote(class_member.name)}', "
                f"m.class_id = '{_cypher_quote(class_member.class_id)}', "
                f"m.file_path = '{_cypher_quote(class_member.file_path)}', "
                f"m.module_name = '{_cypher_quote(class_member.module_name)}', "
                f"m.line_number = {class_member.line_number}, "
                f"m.summary = '{_cypher_quote(class_member.summary)}';"
            )
            queries.append(
                f"MATCH (c:Class {{class_key: '{class_key}'}}), "
                f"(m:ClassMember {{class_member_key: '{class_member_key}'}}) "
                "MERGE (c)-[:DECLARES_MEMBER]->(m);"
            )
        for function in graph.functions:
            function_key = _cypher_quote(_scoped_key(graph.build_fingerprint, function.function_id))
            queries.append(
                f"MERGE (fn:Function {{function_key: '{function_key}'}}) "
                f"SET fn.build_fingerprint = '{build_fingerprint}', "
                f"fn.function_id = '{_cypher_quote(function.function_id)}', "
                f"fn.name = '{_cypher_quote(function.name)}', "
                f"fn.qualified_name = '{_cypher_quote(function.qualified_name)}', "
                f"fn.file_path = '{_cypher_quote(function.file_path)}', "
                f"fn.module_name = '{_cypher_quote(function.module_name)}', "
                f"fn.start_line = {function.start_line}, "
                f"fn.end_line = {function.end_line}, "
                f"fn.summary = '{_cypher_quote(function.summary)}', "
                f"fn.business_context = '{_cypher_quote(function.business_context)}', "
                f"fn.trust_boundary = '{_cypher_quote(function.trust_boundary)}', "
                f"fn.class_id = '{_cypher_quote(function.class_id or '')}';"
            )
            if function.class_id:
                class_key = _cypher_quote(_scoped_key(graph.build_fingerprint, function.class_id))
                queries.append(
                    f"MATCH (c:Class {{class_key: '{class_key}'}}), "
                    f"(fn:Function {{function_key: '{function_key}'}}) "
                    "MERGE (c)-[:DECLARES_METHOD]->(fn);"
                )
            else:
                file_key = _cypher_quote(_scoped_key(graph.build_fingerprint, function.file_path))
                queries.append(
                    f"MATCH (f:File {{file_key: '{file_key}'}}), "
                    f"(fn:Function {{function_key: '{function_key}'}}) "
                    "MERGE (f)-[:DECLARES_FUNCTION]->(fn);"
                )
        for symbol in graph.module_symbols:
            symbol_key = _cypher_quote(_scoped_key(graph.build_fingerprint, symbol.symbol_id))
            file_key = _cypher_quote(_scoped_key(graph.build_fingerprint, symbol.file_path))
            queries.append(
                f"MERGE (s:ModuleSymbol {{symbol_key: '{symbol_key}'}}) "
                f"SET s.build_fingerprint = '{build_fingerprint}', "
                f"s.symbol_id = '{_cypher_quote(symbol.symbol_id)}', "
                f"s.name = '{_cypher_quote(symbol.name)}', "
                f"s.kind = '{_cypher_quote(symbol.kind)}', "
                f"s.module_name = '{_cypher_quote(symbol.module_name)}', "
                f"s.file_path = '{_cypher_quote(symbol.file_path)}', "
                f"s.start_line = {symbol.start_line}, "
                f"s.end_line = {symbol.end_line}, "
                f"s.type_annotation = '{_cypher_quote(symbol.type_annotation)}', "
                f"s.value_repr = '{_cypher_quote(symbol.value_repr)}', "
                f"s.is_placeholder = {'true' if symbol.is_placeholder else 'false'};"
            )
            queries.append(
                f"MATCH (f:File {{file_key: '{file_key}'}}), "
                f"(s:ModuleSymbol {{symbol_key: '{symbol_key}'}}) "
                "MERGE (f)-[:DECLARES_SYMBOL]->(s);"
            )
        for use in graph.function_symbol_uses:
            function_key = _cypher_quote(_scoped_key(graph.build_fingerprint, use.function_id))
            symbol_key = _cypher_quote(_scoped_key(graph.build_fingerprint, use.symbol_id))
            queries.append(
                "MATCH (fn:Function {function_key: '" + function_key + "'}), "
                "(s:ModuleSymbol {symbol_key: '" + symbol_key + "'}) "
                "MERGE (fn)-[r:USES_SYMBOL]->(s) "
                f"SET r.line_number = {use.line_number}, "
                f"r.evidence = '{_cypher_quote(use.evidence)}';"
            )
        for edge in graph.edges:
            source_key = _cypher_quote(_scoped_key(graph.build_fingerprint, edge.source_function))
            target_key = _cypher_quote(_scoped_key(graph.build_fingerprint, edge.target_function))
            queries.append(
                "MATCH (source:Function {function_key: '" + source_key + "'}), "
                "(target:Function {function_key: '" + target_key + "'}) "
                "MERGE (source)-[r:CALLS]->(target) "
                "SET r.file_path = '" + _cypher_quote(edge.provenance.file_path) + "', "
                "r.evidence = '" + _cypher_quote(edge.provenance.evidence) + "', "
                f"r.line_number = {edge.provenance.line_number}, "
                f"r.edge_type = '{_cypher_quote(edge.edge_type)}', "
                f"r.confidence = '{edge.confidence.value}';"
            )
        for path in graph.paths:
            path_key = _cypher_quote(_scoped_key(graph.build_fingerprint, path.path_fingerprint))
            queries.append(
                f"MERGE (p:Path {{path_key: '{path_key}'}}) "
                f"SET p.build_fingerprint = '{build_fingerprint}', "
                f"p.path_fingerprint = '{_cypher_quote(path.path_fingerprint)}', "
                f"p.entry_function = '{_cypher_quote(path.entry_function)}', "
                f"p.business_context = '{_cypher_quote(path.business_context)}', "
                f"p.trust_boundary = '{_cypher_quote(path.trust_boundary)}';"
            )
            queries.append(
                f"MATCH (build:GraphBuild {{build_fingerprint: '{build_fingerprint}'}}), "
                f"(p:Path {{path_key: '{path_key}'}}) "
                "MERGE (build)-[:CONTAINS_PATH]->(p);"
            )
            for position, function_id in enumerate(path.function_ids):
                function_key = _cypher_quote(_scoped_key(graph.build_fingerprint, function_id))
                queries.append(
                    "MATCH (p:Path {path_key: '" + path_key + "'}), "
                    "(fn:Function {function_key: '" + function_key + "'}) "
                    f"MERGE (p)-[:INCLUDES {{position: {position}}}]->(fn);"
                )
        self._execute_queries(
            queries,
            timeout=self.graph_import_timeout,
            batch_size=self.graph_batch_size,
            operation="neo4j.persist_graph",
            fingerprint=graph.build_fingerprint,
        )

    def persist_audit_run(self, audit_run: AuditRun) -> None:
        queries = [
            f"MERGE (run:AuditRun {{build_fingerprint: '{_cypher_quote(audit_run.build_fingerprint)}'}}) "
            "ON CREATE SET run.created_at = timestamp();",
            f"MATCH (run:AuditRun {{build_fingerprint: '{_cypher_quote(audit_run.build_fingerprint)}'}}), "
            f"(build:GraphBuild {{build_fingerprint: '{_cypher_quote(audit_run.build_fingerprint)}'}}) "
            "MERGE (run)-[:FOR_BUILD]->(build);",
        ]
        for finding in audit_run.findings:
            path_key = _cypher_quote(_scoped_key(audit_run.build_fingerprint, finding.path_fingerprint))
            queries.append(
                f"MATCH (run:AuditRun {{build_fingerprint: '{_cypher_quote(audit_run.build_fingerprint)}'}}), "
                f"(path:Path {{path_key: '{path_key}'}}) "
                f"MERGE (f:Finding {{finding_id: '{_cypher_quote(finding.finding_id)}'}}) "
                f"SET f.name = '{_cypher_quote(finding.finding_name)}', "
                f"f.build_fingerprint = '{_cypher_quote(audit_run.build_fingerprint)}', "
                f"f.confidence_level = '{finding.confidence_level.value}', "
                f"f.validation_status = '{finding.validation_status.value}', "
                f"f.path_fingerprint = '{_cypher_quote(finding.path_fingerprint)}' "
                "MERGE (run)-[:HAS_FINDING]->(f) "
                "MERGE (f)-[:ON_PATH]->(path);"
            )
        self._execute_queries(
            queries,
            timeout=self.audit_import_timeout,
            operation="neo4j.persist_audit_run",
            fingerprint=audit_run.build_fingerprint,
        )

    def _execute_queries(
        self,
        queries: list[str],
        *,
        timeout: int,
        batch_size: int | None = None,
        operation: str,
        fingerprint: str | None = None,
    ) -> None:
        if not queries:
            return
        chunk_size = batch_size or len(queries)
        for start in range(0, len(queries), chunk_size):
            chunk = queries[start : start + chunk_size]
            self._run_with_resilience(
                operation,
                lambda chunk=chunk: self.driver.run_batch(chunk, timeout=timeout),
                fingerprint=fingerprint,
            )

    def _run_with_resilience(
        self,
        operation: str,
        call: Any,
        *,
        fingerprint: str | None = None,
    ) -> Any:
        try:
            return self.circuit_breaker.call(
                lambda: retry(
                    call,
                    operation=operation,
                    attempts=3,
                    base_delay=0.5,
                    max_delay=2.0,
                    exceptions=(Exception,),
                    should_retry=lambda exc: isinstance(exc, GraphdbError) or not isinstance(exc, XAuditorError),
                    logger=self.logger,
                ),
                operation=operation,
                exceptions=(RetryExhaustedError,),
            )
        except (RetryExhaustedError, CircuitOpenError) as exc:
            if self.logger is not None:
                self.logger.error_kv(
                    "Neo4j operation failed",
                    operation=operation,
                    fingerprint=fingerprint,
                    attempts=getattr(exc, "attempts", None),
                    cause=str(getattr(exc, "last_error", exc)),
                )
            raise Neo4jError(
                f"Neo4j operation failed during {operation}.",
                operation=operation,
                fingerprint=fingerprint or "",
            ) from exc


class InMemoryNeo4jAdapter:
    """In-memory test double for ``Neo4jAdapter``.

    Backs the streaming ``write_*_chunk`` methods on
    ``InMemoryNeo4jGraphRepository`` (see ``neo4j_repository.py``).
    Storage shape is per-fingerprint ``_StoredBuild`` records that
    accumulate as the chunked writers fire. Replaces the pre-0.9.0
    ``graphs: list[GraphBuildResult]`` storage.
    """

    def __init__(self) -> None:
        # fingerprint → _StoredBuild (defined in neo4j_repository.py).
        # Lazy-initialised dict, populated by ``_upsert_build``.
        from xauditor.integrations.neo4j_repository import _StoredBuild as _SB

        self._StoredBuild = _SB
        self.builds_by_fingerprint: dict[str, Any] = {}
        self.repo_builds: dict[str, list[str]] = {}
        self.audits: list[AuditRun] = []

    # Pre-0.9.0 attribute name kept for back-compat with legacy tests
    # that count entries via ``len(adapter.graphs_by_fingerprint)`` or
    # clear them with ``adapter.graphs_by_fingerprint.clear()``. Points
    # at the same dict as ``builds_by_fingerprint`` so reads / mutations
    # transparently affect storage.
    @property
    def graphs_by_fingerprint(self) -> dict[str, Any]:
        return self.builds_by_fingerprint

    @property
    def graphs(self) -> list[Any]:
        # Snapshot list view; legacy tests that called ``.clear()`` on
        # this attribute should clear ``builds_by_fingerprint`` instead.
        return list(self.builds_by_fingerprint.values())

    def ping(self) -> None:
        return None

    def bootstrap_schema(self) -> None:
        return None

    def persist_audit_run(self, audit_run: AuditRun) -> None:
        self.audits.append(audit_run)

    # --- chunk-write accumulator helpers (called by InMemoryNeo4jGraphRepository) ---

    def _upsert_build(
        self, *, build_fingerprint: str, repo_root: Path
    ) -> None:
        existing = self.builds_by_fingerprint.get(build_fingerprint)
        if existing is None:
            self.builds_by_fingerprint[build_fingerprint] = self._StoredBuild(
                repo_root=repo_root,
                build_fingerprint=build_fingerprint,
            )
        repo_key = str(repo_root)
        builds = self.repo_builds.setdefault(repo_key, [])
        if build_fingerprint in builds:
            builds.remove(build_fingerprint)
        builds.append(build_fingerprint)

    def _extend_build(
        self,
        build_fingerprint: str,
        attribute: str,
        records: Sequence[Any],
    ) -> None:
        build = self.builds_by_fingerprint.get(build_fingerprint)
        if build is None:
            raise GraphdbError(
                f"InMemoryNeo4jAdapter: cannot extend `{attribute}` for "
                f"unknown build fingerprint `{build_fingerprint}`. "
                "Call begin_build() before any write_*_chunk()."
            )
        existing = getattr(build, attribute, ())
        setattr(build, attribute, tuple(existing) + tuple(records))

    def _finalize_build(
        self,
        *,
        build_fingerprint: str,
        paths_truncated: bool = False,
        paths_truncation_reason: str | None = None,
        paths_truncation_cap: int | None = None,
        coverage: CoverageInventory | None = None,
    ) -> None:
        build = self.builds_by_fingerprint.get(build_fingerprint)
        if build is None:
            raise GraphdbError(
                f"InMemoryNeo4jAdapter: cannot finalize unknown build "
                f"fingerprint `{build_fingerprint}`."
            )
        build.paths_truncated = paths_truncated
        build.paths_truncation_reason = paths_truncation_reason
        build.paths_truncation_cap = paths_truncation_cap
        if coverage is not None:
            build.coverage = coverage
