"""Test-only ``AuditGraphSource`` implementation backed by dict storage.

**Production code MUST NOT import this module.** See
``tests/_helpers/__init__.py`` for the rationale and the CI lint
that enforces the rule.

Replaces the pre-0.9.0 pattern of constructing
``GraphBuildResult(...)`` directly and wrapping in
``InMemoryAuditGraphSource``. Tests that need a minimal graph
fixture construct ``_TestOnlyGraphSource(...)`` with whatever
records they care about and skip everything else (the helper
fills in empty tuples).

Spec change: ``consolidate-on-neo4j-source`` (xauditor 0.9.0).
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from xauditor.audit.source import (
    AuditGraphSourceDescriptor,
    Neo4jSourceDescriptor,
)
from xauditor.models import (
    AuditPlan,
    AuditedCallChain,
    ClassRecord,
    CoverageInventory,
    CoverageRecord,
    CoverageState,
    EdgeRecord,
    FunctionRecord,
    FunctionSymbolUseEdge,
    ModuleRecord,
    ModuleSymbolRecord,
    PathRecord,
    RepositoryScope,
)


class _TestOnlyGraphSource:
    """Minimal in-memory ``AuditGraphSource`` for unit tests.

    Implements the full Protocol via dict storage. Records can be
    supplied piecemeal at construction time; absent kinds default
    to empty tuples.

    The class is *intentionally* called ``_TestOnlyGraphSource`` —
    the leading underscore is a visual reminder to anyone scanning
    grep output that this is not a production type.
    """

    @classmethod
    def from_inmemory_repo(
        cls, repository, fingerprint: str
    ) -> "_TestOnlyGraphSource":
        """Construct a source from records the streaming
        ``canonical_finalize`` accumulated into an
        ``InMemoryNeo4jAdapter``.

        Tests that exercise the full graph-build pipeline pass an
        ``InMemoryNeo4jGraphRepository(InMemoryNeo4jAdapter())`` as
        the repository, run ``LangChainGraphBuilder.build(...)``,
        then call this classmethod to load a Protocol-compatible
        ``_TestOnlyGraphSource`` for the audit workflow.
        """

        build = repository._require_build(fingerprint)
        return cls(
            build_fingerprint=build.build_fingerprint,
            repo_root=build.repo_root,
            modules=build.modules,
            classes=build.classes,
            functions=build.functions,
            paths=build.paths,
            edges=build.edges,
            module_symbols=build.module_symbols,
            function_symbol_uses=build.function_symbol_uses,
            coverage=build.coverage or CoverageInventory(),
            scope=build.scope,
        )

    def __init__(
        self,
        *,
        build_fingerprint: str = "test-fp",
        repo_root: Path | None = None,
        modules: Iterable[ModuleRecord] = (),
        classes: Iterable[ClassRecord] = (),
        functions: Iterable[FunctionRecord] = (),
        paths: Iterable[PathRecord] = (),
        edges: Iterable[EdgeRecord] = (),
        module_symbols: Iterable[ModuleSymbolRecord] = (),
        function_symbol_uses: Iterable[FunctionSymbolUseEdge] = (),
        coverage: CoverageInventory | None = None,
        scope: RepositoryScope | None = None,
    ) -> None:
        self.build_fingerprint = build_fingerprint
        self.repo_root = repo_root or Path("/tmp/test-only")
        self._modules = tuple(modules)
        self._classes = tuple(classes)
        self._functions = tuple(functions)
        self._paths = tuple(paths)
        self._edges = tuple(edges)
        self._module_symbols = tuple(module_symbols)
        self._function_symbol_uses = tuple(function_symbol_uses)
        self._coverage = coverage or CoverageInventory()
        self._scope = scope or RepositoryScope(
            repo_root=self.repo_root,
            excludes=(),
            included_files=(),
            excluded_files=(),
        )

        # Indexes (built eagerly — fixtures are tiny).
        self._function_by_id = {f.function_id: f for f in self._functions}
        self._class_by_id = {c.class_id: c for c in self._classes}
        self._path_by_fingerprint = {
            p.path_fingerprint: p for p in self._paths
        }
        self._function_by_name: dict[str, FunctionRecord] = {}
        for function in self._functions:
            self._function_by_name.setdefault(function.name, function)
            self._function_by_name[function.qualified_name] = function

        self._symbol_by_id = {
            symbol.symbol_id: symbol for symbol in self._module_symbols
        }
        uses_by_function: dict[str, list[FunctionSymbolUseEdge]] = defaultdict(list)
        for use in self._function_symbol_uses:
            uses_by_function[use.function_id].append(use)
        self._uses_by_function = uses_by_function

    # --- Path-oriented (audit-time hot path) ---

    def iter_paths(self, *, page_size: int = 100) -> Iterator[PathRecord]:
        del page_size
        yield from self._paths

    def path_count(self) -> int:
        return len(self._paths)

    def load_path_functions(
        self, function_ids: Sequence[str]
    ) -> list[FunctionRecord]:
        return [
            self._function_by_id[function_id]
            for function_id in function_ids
            if function_id in self._function_by_id
        ]

    def load_path_symbols(
        self, function_ids: Sequence[str]
    ) -> tuple[tuple[ModuleSymbolRecord, ...], tuple[FunctionSymbolUseEdge, ...]]:
        function_id_set = set(function_ids)
        use_list: list[FunctionSymbolUseEdge] = []
        symbol_ids: list[str] = []
        seen_symbols: set[str] = set()
        for function_id in function_ids:
            for use in self._uses_by_function.get(function_id, ()):
                if use.symbol_id not in self._symbol_by_id:
                    continue
                use_list.append(use)
                if use.symbol_id not in seen_symbols:
                    seen_symbols.add(use.symbol_id)
                    symbol_ids.append(use.symbol_id)
        symbols = tuple(self._symbol_by_id[sid] for sid in symbol_ids)
        return symbols, tuple(
            use for use in use_list if use.function_id in function_id_set
        )

    def build_coverage(
        self,
        *,
        plan: AuditPlan,
        skipped_paths: set[str],
        failed_paths: set[str] | frozenset[str] = frozenset(),
    ) -> CoverageInventory:
        # Lightweight stand-in for the in-memory source's
        # _build_coverage_from_graph: marks the units in `plan`
        # as audited, everything else as not_audited / interrupted /
        # failed depending on the input sets. Tests that need
        # detailed coverage assertions construct their own
        # CoverageInventory instead.
        audited_path_set = {
            unit.path.path_fingerprint for unit in plan.audit_units
        }
        coverage = CoverageInventory()
        for path in self._paths:
            fp = path.path_fingerprint
            if fp in audited_path_set:
                state = CoverageState.AUDITED
            elif fp in failed_paths:
                state = CoverageState.FAILED
            elif fp in skipped_paths:
                state = CoverageState.INTERRUPTED
            else:
                state = CoverageState.NOT_AUDITED
            coverage.add(
                CoverageRecord(category="path", identifier=fp, state=state)
            )
        for unit in plan.audit_units:
            coverage.audited_call_chains.append(
                AuditedCallChain(
                    path_fingerprint=unit.path.path_fingerprint,
                    entry_function=unit.path.entry_function,
                    function_chain=tuple(unit.path.function_names),
                )
            )
        return coverage

    # --- Catalog-oriented (replaces GraphBuildResult attribute reads) ---

    def iter_functions(
        self, *, page_size: int = 1000
    ) -> Iterator[FunctionRecord]:
        del page_size
        yield from self._functions

    def iter_classes(self, *, page_size: int = 1000) -> Iterator[ClassRecord]:
        del page_size
        yield from self._classes

    def iter_edges(self, *, page_size: int = 1000) -> Iterator[EdgeRecord]:
        del page_size
        yield from self._edges

    def iter_module_symbols(
        self, *, page_size: int = 1000
    ) -> Iterator[ModuleSymbolRecord]:
        del page_size
        yield from self._module_symbols

    def lookup_function_by_id(
        self, function_id: str
    ) -> FunctionRecord | None:
        return self._function_by_id.get(function_id)

    def lookup_class_by_id(self, class_id: str) -> ClassRecord | None:
        return self._class_by_id.get(class_id)

    def lookup_path_by_fingerprint(
        self, path_fingerprint: str
    ) -> PathRecord | None:
        return self._path_by_fingerprint.get(path_fingerprint)

    def lookup_function_by_name(self, name: str) -> FunctionRecord | None:
        return self._function_by_name.get(name)

    def coverage_inventory(self) -> CoverageInventory:
        return self._coverage

    # --- Lifecycle (subprocess pool integration) ---

    def pool_descriptor(self) -> AuditGraphSourceDescriptor:
        # 0.9.0: only ``Neo4jSourceDescriptor`` is supported by
        # ``rebuild_source``. Tests that exercise the subprocess
        # pool with a ``_TestOnlyGraphSource`` cannot actually
        # round-trip records through the descriptor — the descriptor
        # is a connection-params bundle, not a graph payload. Tests
        # that need pool-spawn coverage construct a sentinel
        # descriptor with a fake host/fingerprint and exercise pool
        # construction / shutdown only.
        raise NotImplementedError(
            "_TestOnlyGraphSource.pool_descriptor: subprocess pool "
            "requires a Neo4jSourceDescriptor pointing at a real "
            "Neo4j instance. Construct one explicitly in your test "
            "(see tests/audit/test_worker_pool.py for the pattern)."
        )

    def close(self) -> None:
        return None


__all__ = ["_TestOnlyGraphSource"]
