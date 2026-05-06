from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol, Sequence

from xauditor.models import (
    AuditPlan,
    ClassRecord,
    CoverageInventory,
    DecoratorRecord,
    EdgeRecord,
    FunctionRecord,
    FunctionSymbolUseEdge,
    ModuleSymbolRecord,
    PathRecord,
    RegistrationSiteRecord,
)


# ---------------------------------------------------------------------------
# Source descriptor ADT — pickle-friendly handshake for subprocess workers.
# Only the Neo4j-backed source has a descriptor in 0.9.0; the in-memory
# backend was removed by ``consolidate-on-neo4j-source``.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Neo4jSourceDescriptor:
    """Reconstruction handle for ``Neo4jAuditGraphSource``.

    Carries only the connection parameters and the immutable fields the
    worker needs to rebuild a fresh source on its own end. The
    ``neo4j_config`` is a frozen dataclass of primitives (port,
    username, password, database) so the whole descriptor pickles to
    around 200 bytes regardless of graph size.

    Workers spawned by ``LocalSubprocessPool`` instantiate their own
    ``Neo4jDriver`` from this descriptor — the host-wide Neo4j
    connection count therefore equals ``worker_count + 1`` (one driver
    per worker plus master's own driver).
    """

    neo4j_config: Any  # xauditor.config.Neo4jConfig — avoid circular import
    host: str
    build_fingerprint: str
    repo_root: Path
    page_size: int = 100


# Type alias kept for back-compat with call sites that still type-hint
# ``AuditGraphSourceDescriptor`` (the type is no longer a union since
# the in-memory backend was removed in 0.9.0).
AuditGraphSourceDescriptor = Neo4jSourceDescriptor


class AuditGraphSource(Protocol):
    """Streaming graph-source abstraction the audit workflow consumes.

    0.9.0 ``consolidate-on-neo4j-source``: ``Neo4jAuditGraphSource``
    is the only production implementation. Tests construct
    ``_TestOnlyGraphSource`` from ``tests/_helpers/`` for unit-test
    ergonomics; production code MUST NOT import from ``tests/_helpers/``.

    The Protocol is split into two surfaces:
    - **Path-oriented (audit-time hot path)**: ``iter_paths``,
      ``path_count``, ``load_path_functions``, ``load_path_symbols``.
      Optimized for "process one audit path at a time, fetch its
      functions + symbols on demand".
    - **Catalog-oriented (planning, reporting, lookups)**:
      ``iter_functions``, ``iter_classes``, ``iter_edges``,
      ``iter_module_symbols``, the four ``lookup_*_by_*`` methods,
      and ``coverage_inventory``.

    Pagination semantics: every ``iter_*(page_size=N)`` method
    SHOULD respect the page_size hint as the upper bound on records
    held in memory at one time during iteration. Implementations
    that load everything up front (e.g. ``_TestOnlyGraphSource``)
    just yield from a list and ignore the hint; that's fine —
    page_size is a hint for backends that can stream, not a
    contract.

    Lookup semantics: every ``lookup_*`` method returns ``None``
    for missing identifiers and SHOULD be O(1) amortized (cache
    in the implementation if the underlying store doesn't support
    point lookups).
    """

    build_fingerprint: str
    repo_root: Path

    # --- Path-oriented (audit-time hot path) ---

    def iter_paths(self, *, page_size: int = 100) -> Iterator[PathRecord]: ...

    def path_count(self) -> int: ...

    def load_path_functions(self, function_ids: Sequence[str]) -> list[FunctionRecord]: ...

    def load_path_symbols(
        self, function_ids: Sequence[str]
    ) -> tuple[tuple[ModuleSymbolRecord, ...], tuple[FunctionSymbolUseEdge, ...]]: ...

    def fetch_decorators_for(
        self, function_ids: Sequence[str]
    ) -> dict[str, list["DecoratorRecord"]]:
        """Return `{function_id: [DecoratorRecord ordered by position]}`.

        `capture-decorators-and-registrations` Phase 2.1.
        Implementations that don't carry decorator data (legacy
        graphs, test fixtures that construct the source without
        a builder run) MAY return an empty dict.
        """
        ...

    def fetch_registrations_for(
        self, function_ids: Sequence[str]
    ) -> dict[str, list["RegistrationSiteRecord"]]:
        """Return `{function_id: [RegistrationSiteRecord ...]}`.

        `capture-decorators-and-registrations` Phase 2.1 / Commit C.
        Implementations that don't carry registration data MAY
        return an empty dict.
        """
        ...

    def build_coverage(
        self,
        *,
        plan: AuditPlan,
        skipped_paths: set[str],
        failed_paths: set[str] | frozenset[str] = frozenset(),
    ) -> CoverageInventory: ...

    # --- Catalog-oriented (replaces 0.8.x GraphBuildResult attribute reads) ---

    def iter_functions(self, *, page_size: int = 1000) -> Iterator[FunctionRecord]:
        """Yield every ``FunctionRecord`` for this build."""
        ...

    def iter_classes(self, *, page_size: int = 1000) -> Iterator[ClassRecord]:
        """Yield every ``ClassRecord`` for this build."""
        ...

    def iter_edges(self, *, page_size: int = 1000) -> Iterator[EdgeRecord]:
        """Yield every ``EdgeRecord`` for this build."""
        ...

    def iter_module_symbols(
        self, *, page_size: int = 1000
    ) -> Iterator[ModuleSymbolRecord]:
        """Yield every ``ModuleSymbolRecord`` for this build."""
        ...

    def lookup_function_by_id(
        self, function_id: str
    ) -> FunctionRecord | None:
        """Return the function with the given id, or ``None``."""
        ...

    def lookup_class_by_id(self, class_id: str) -> ClassRecord | None:
        """Return the class with the given id, or ``None``."""
        ...

    def lookup_path_by_fingerprint(
        self, path_fingerprint: str
    ) -> PathRecord | None:
        """Return the path with the given fingerprint, or ``None``."""
        ...

    def lookup_function_by_name(
        self, name: str
    ) -> FunctionRecord | None:
        """Return the first function whose ``name`` or
        ``qualified_name`` matches, or ``None``."""
        ...

    def coverage_inventory(self) -> CoverageInventory:
        """Return the coverage inventory associated with this build.
        Implementations SHOULD cache the result for the lifetime of
        the source instance.
        """
        ...

    # --- Lifecycle (subprocess pool integration) ---

    def pool_descriptor(self) -> AuditGraphSourceDescriptor:
        """Return a pickle-friendly descriptor describing how a
        subprocess worker can reconstruct an equivalent source.
        Mandatory for any source intended to work with
        ``LocalSubprocessPool`` (i.e. ``audit.worker_count >= 2``).
        """
        ...

    def close(self) -> None:
        """Release any resources the source holds."""
        ...


def rebuild_source(descriptor: AuditGraphSourceDescriptor) -> AuditGraphSource:
    """Module-level factory: subprocess workers call this on entry to
    reconstruct an ``AuditGraphSource`` from a pickled descriptor.

    Only ``Neo4jSourceDescriptor`` is recognised in 0.9.0 — the
    pre-0.9.0 in-memory dispatch branch was removed by
    ``consolidate-on-neo4j-source``.
    """

    if isinstance(descriptor, Neo4jSourceDescriptor):
        # Lazy imports — keep neo4j-driver out of the import graph
        # for callers who don't need it (e.g. tests that don't
        # exercise the subprocess pool).
        from xauditor.integrations.neo4j_driver import Neo4jDriver
        from xauditor.integrations.neo4j_repository import (
            Neo4jGraphRepository,
        )
        from xauditor.integrations.audit_source_neo4j import (
            Neo4jAuditGraphSource,
        )

        driver = Neo4jDriver(descriptor.neo4j_config, host=descriptor.host)
        repository = Neo4jGraphRepository(
            config=descriptor.neo4j_config, driver=driver
        )
        return Neo4jAuditGraphSource(
            repository=repository,
            build_fingerprint=descriptor.build_fingerprint,
            repo_root=descriptor.repo_root,
            page_size=descriptor.page_size,
        )
    raise NotImplementedError(
        f"rebuild_source: no factory branch for descriptor type "
        f"{type(descriptor).__name__}"
    )


__all__ = [
    "AuditGraphSource",
    "AuditGraphSourceDescriptor",
    "Neo4jSourceDescriptor",
    "rebuild_source",
]
