from __future__ import annotations

from pathlib import Path
from typing import Iterator, Sequence

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
    ModuleSymbolRecord,
    PathRecord,
)


class Neo4jAuditGraphSource:
    def __init__(
        self,
        *,
        repository,
        build_fingerprint: str,
        repo_root: Path,
        page_size: int = 100,
    ) -> None:
        self._repository = repository
        self.build_fingerprint = build_fingerprint
        self.repo_root = repo_root
        self._page_size = page_size
        # Lazy caches for the catalog-oriented iter_*/lookup_*
        # Protocol methods (added 0.9.0,
        # ``consolidate-on-neo4j-source``). Foundation phase keeps
        # the existing repository ``_load_*`` semantics (load-all
        # tuple) and builds dict indexes on first lookup. A future
        # change can swap these for paginated Cypher streams.
        self._functions_cache: tuple[FunctionRecord, ...] | None = None
        self._classes_cache: tuple[ClassRecord, ...] | None = None
        self._edges_cache: tuple[EdgeRecord, ...] | None = None
        self._module_symbols_cache: tuple[ModuleSymbolRecord, ...] | None = None
        self._function_by_id_cache: dict[str, FunctionRecord] | None = None
        self._class_by_id_cache: dict[str, ClassRecord] | None = None
        self._function_by_name_cache: dict[str, FunctionRecord] | None = None
        self._path_by_fingerprint_cache: dict[str, PathRecord] | None = None
        self._coverage_cache: CoverageInventory | None = None

    def iter_paths(self, *, page_size: int | None = None) -> Iterator[PathRecord]:
        yield from self._repository.iter_path_records(
            self.build_fingerprint, page_size=page_size or self._page_size
        )

    def path_count(self) -> int:
        return self._repository.count_paths(self.build_fingerprint)

    def load_path_functions(self, function_ids: Sequence[str]) -> list[FunctionRecord]:
        return self._repository.load_functions_by_ids(self.build_fingerprint, function_ids)

    def load_path_symbols(
        self, function_ids: Sequence[str]
    ) -> tuple[tuple[ModuleSymbolRecord, ...], tuple[FunctionSymbolUseEdge, ...]]:
        return self._repository.load_symbols_for_functions(self.build_fingerprint, function_ids)

    def fetch_decorators_for(self, function_ids: Sequence[str]):
        # `capture-decorators-and-registrations` Phase 2.1 —
        # delegate to the repository read method. Empty dict for
        # graphs built before Commit A (the repository returns
        # `{}` when no decorator rows exist for the function ids).
        if not hasattr(self._repository, "fetch_decorators_for"):
            return {}
        return self._repository.fetch_decorators_for(function_ids)

    def fetch_registrations_for(self, function_ids: Sequence[str]):
        # `capture-decorators-and-registrations` Commit C —
        # registration_context counterpart to fetch_decorators_for.
        if not hasattr(self._repository, "fetch_registrations_for"):
            return {}
        return self._repository.fetch_registrations_for(function_ids)

    def build_coverage(
        self,
        *,
        plan: AuditPlan,
        skipped_paths: set[str],
        failed_paths: set[str] | frozenset[str] = frozenset(),
    ) -> CoverageInventory:
        (
            modules,
            files,
            function_qualified_names,
            function_to_module,
            function_to_file,
            function_to_name,
            paths,
        ) = self._repository.coverage_baseline(self.build_fingerprint)

        audited_function_ids = {function_id for unit in plan.audit_units for function_id in unit.function_ids}
        audited_paths = {unit.path.path_fingerprint for unit in plan.audit_units}
        audited_modules = {
            function_to_module[function_id]
            for function_id in audited_function_ids
            if function_id in function_to_module
        }
        audited_files = {
            function_to_file[function_id]
            for function_id in audited_function_ids
            if function_id in function_to_file
        }
        audited_function_names = {
            function_to_name[function_id]
            for function_id in audited_function_ids
            if function_id in function_to_name
        }

        coverage = CoverageInventory()
        for identifier in modules:
            state = CoverageState.AUDITED if identifier in audited_modules else CoverageState.NOT_AUDITED
            coverage.add(CoverageRecord(category="module", identifier=identifier, state=state))
        for identifier in files:
            state = CoverageState.AUDITED if identifier in audited_files else CoverageState.NOT_AUDITED
            coverage.add(CoverageRecord(category="file", identifier=identifier, state=state))
        for identifier in function_qualified_names:
            state = (
                CoverageState.AUDITED if identifier in audited_function_names else CoverageState.NOT_AUDITED
            )
            coverage.add(CoverageRecord(category="function", identifier=identifier, state=state))
        for identifier in paths:
            if identifier in audited_paths:
                state = CoverageState.AUDITED
            elif identifier in failed_paths:
                state = CoverageState.FAILED
            elif identifier in skipped_paths:
                state = CoverageState.INTERRUPTED
            else:
                state = CoverageState.NOT_AUDITED
            coverage.add(CoverageRecord(category="path", identifier=identifier, state=state))

        for unit in plan.audit_units:
            coverage.audited_call_chains.append(
                AuditedCallChain(
                    path_fingerprint=unit.path.path_fingerprint,
                    entry_function=unit.path.entry_function,
                    function_chain=tuple(unit.path.function_names),
                )
            )
        return coverage

    def pool_descriptor(self):
        # Subprocess workers spawned by ``LocalSubprocessPool`` need
        # the connection params to rebuild a fresh ``Neo4jDriver`` on
        # their end. Inspect the underlying repository to recover
        # ``Neo4jConfig`` + host. The fallback raises informatively
        # for repositories that don't carry these fields (e.g. the
        # in-memory test stub) — those paths simply aren't compatible
        # with subprocess pool today.
        from xauditor.audit.source import Neo4jSourceDescriptor
        from xauditor.integrations.neo4j_repository import (
            Neo4jGraphRepository,
        )

        repository = self._repository
        if not isinstance(repository, Neo4jGraphRepository):
            raise NotImplementedError(
                "Neo4jAuditGraphSource.pool_descriptor: subprocess pool "
                "requires a real Neo4jGraphRepository (with a live "
                "BoltDriver). Got "
                f"repository of type {type(repository).__name__} — "
                "use audit.worker_count: 1 to fall back to the thread "
                "pool, which can share an in-memory test repository."
            )
        driver = repository.driver
        host = getattr(driver, "host", None)
        if host is None:
            host = getattr(driver, "_host", None)
        if host is None:
            raise NotImplementedError(
                "Neo4jAuditGraphSource.pool_descriptor: driver "
                f"{type(driver).__name__} exposes neither a public "
                "`host` attribute nor a private `_host` — extend the "
                "driver to surface this so the subprocess pool can "
                "reconstruct it."
            )
        return Neo4jSourceDescriptor(
            neo4j_config=repository.config,
            host=host,
            build_fingerprint=self.build_fingerprint,
            repo_root=self.repo_root,
            page_size=self._page_size,
        )

    def close(self) -> None:
        # Forward to the underlying driver so subprocess workers
        # don't leak Bolt connections at exit. Best-effort: some
        # repository stubs don't expose a closable driver, in which
        # case we silently skip.
        repository = self._repository
        driver = getattr(repository, "driver", None)
        if driver is None:
            return
        close_fn = getattr(driver, "close", None)
        if close_fn is None:
            return
        try:
            close_fn()
        except Exception:  # noqa: BLE001 - shutdown best-effort
            pass

    # --- 0.9.0 catalog-oriented Protocol methods (foundation phase) ---
    # These currently call repository ``_load_*`` methods which load
    # everything in one Bolt round-trip. The Protocol API supports
    # streaming, but the implementation is materialize-once-then-yield.
    # A future change can swap the underlying Cypher for paginated
    # streams without changing this Protocol surface.

    def iter_functions(self, *, page_size: int = 1000) -> Iterator[FunctionRecord]:
        del page_size
        if self._functions_cache is None:
            self._functions_cache = self._repository._load_functions(
                self.build_fingerprint
            )
        yield from self._functions_cache

    def iter_classes(self, *, page_size: int = 1000) -> Iterator[ClassRecord]:
        del page_size
        if self._classes_cache is None:
            self._classes_cache = self._repository._load_classes(
                self.build_fingerprint
            )
        yield from self._classes_cache

    def iter_edges(self, *, page_size: int = 1000) -> Iterator[EdgeRecord]:
        del page_size
        if self._edges_cache is None:
            self._edges_cache = self._repository._load_edges(
                self.build_fingerprint
            )
        yield from self._edges_cache

    def iter_module_symbols(
        self, *, page_size: int = 1000
    ) -> Iterator[ModuleSymbolRecord]:
        del page_size
        if self._module_symbols_cache is None:
            self._module_symbols_cache = self._repository._load_module_symbols(
                self.build_fingerprint
            )
        yield from self._module_symbols_cache

    def lookup_function_by_id(
        self, function_id: str
    ) -> FunctionRecord | None:
        if self._function_by_id_cache is None:
            self._function_by_id_cache = {
                function.function_id: function
                for function in self.iter_functions()
            }
        return self._function_by_id_cache.get(function_id)

    def lookup_class_by_id(self, class_id: str) -> ClassRecord | None:
        if self._class_by_id_cache is None:
            self._class_by_id_cache = {
                class_record.class_id: class_record
                for class_record in self.iter_classes()
            }
        return self._class_by_id_cache.get(class_id)

    def lookup_path_by_fingerprint(
        self, path_fingerprint: str
    ) -> PathRecord | None:
        if self._path_by_fingerprint_cache is None:
            self._path_by_fingerprint_cache = {
                path.path_fingerprint: path for path in self.iter_paths()
            }
        return self._path_by_fingerprint_cache.get(path_fingerprint)

    def lookup_function_by_name(self, name: str) -> FunctionRecord | None:
        if self._function_by_name_cache is None:
            cache: dict[str, FunctionRecord] = {}
            for function in self.iter_functions():
                cache.setdefault(function.name, function)
                cache[function.qualified_name] = function
            self._function_by_name_cache = cache
        return self._function_by_name_cache.get(name)

    def coverage_inventory(self) -> CoverageInventory:
        # Reads the coverage payload that ``canonical_finalize``
        # streamed onto the ``(b:Build)`` node. Cached for the
        # source's lifetime so repeated audit-time calls don't
        # re-query Neo4j.
        if self._coverage_cache is None:
            load_fn = getattr(self._repository, "load_coverage_payload", None)
            if load_fn is not None:
                try:
                    self._coverage_cache = load_fn(self.build_fingerprint)
                except Exception:  # noqa: BLE001 - read-only path
                    self._coverage_cache = CoverageInventory()
            else:
                self._coverage_cache = CoverageInventory()
        return self._coverage_cache


__all__ = ["Neo4jAuditGraphSource"]
