from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from xauditor.config import XAuditorConfig
from xauditor.llm import LLMClient
from xauditor.models import (
    ClassMemberRecord,
    ClassRecord,
    ConfidenceLevel,
    CoverageInventory,
    CoverageRecord,
    CoverageState,
    EdgeRecord,
    EnrichmentRecord,
    FileRecord,
    FunctionRecord,
    FunctionSymbolUseEdge,
    GraphProvenance,
    ModuleRecord,
    ModuleSymbolRecord,
    PathRecord,
    RepositoryScope,
)


@dataclass(frozen=True)
class DiscoveredFile:
    path: str
    module_name: str
    language: str


@dataclass(frozen=True)
class DiscoveredClass:
    class_id: str
    name: str
    file_path: str
    module_name: str
    start_line: int
    end_line: int
    summary: str = ""
    business_context: str = ""


@dataclass(frozen=True)
class DiscoveredClassMember:
    member_id: str
    name: str
    class_id: str
    file_path: str
    module_name: str
    line_number: int
    summary: str = ""


@dataclass(frozen=True)
class DiscoveredFunction:
    function_id: str
    name: str
    qualified_name: str
    file_path: str
    module_name: str
    start_line: int
    end_line: int
    summary: str = ""
    business_context: str = ""
    trust_boundary: str = ""
    class_id: str | None = None


@dataclass(frozen=True)
class DiscoveredCall:
    caller_function_id: str
    callee_name: str
    file_path: str
    line_number: int
    evidence: str
    edge_type: str = "calls"
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH
    callee_qualified_name: str | None = None
    caller_class_id: str | None = None


@dataclass(frozen=True)
class DiscoveredModuleSymbol:
    symbol_id: str
    name: str
    kind: str
    module_name: str
    file_path: str
    start_line: int
    end_line: int
    type_annotation: str = ""
    value_repr: str = ""
    is_placeholder: bool = False


@dataclass(frozen=True)
class DiscoveredSymbolUse:
    function_id: str
    symbol_id: str
    line_number: int
    evidence: str


@dataclass(frozen=True)
class DiscoveredGraph:
    files: tuple[DiscoveredFile, ...]
    classes: tuple[DiscoveredClass, ...]
    class_members: tuple[DiscoveredClassMember, ...]
    functions: tuple[DiscoveredFunction, ...]
    calls: tuple[DiscoveredCall, ...]
    provenance: tuple[GraphProvenance, ...]
    lsp_messages: tuple[str, ...] = ()
    agent_state: dict[str, dict[str, object]] = field(default_factory=dict)
    module_symbols: tuple[DiscoveredModuleSymbol, ...] = ()
    symbol_uses: tuple[DiscoveredSymbolUse, ...] = ()


@dataclass(frozen=True)
class CanonicalFinalizeResult:
    """Lightweight summary returned by the streaming canonical_finalize.

    Replaces the pre-0.9.0 ``GraphBuildResult`` return value. Records
    themselves live in Neo4j (or the in-memory test stub); this
    envelope just carries the build fingerprint and the few fields
    callers need without re-querying (lsp messages, truncation flags,
    coverage payload).
    """

    build_fingerprint: str
    paths_truncated: bool = False
    paths_truncation_reason: str | None = None
    paths_truncation_cap: int | None = None
    lsp_messages: tuple[str, ...] = ()


class CanonicalGraphTransformer:
    """Streams DiscoveredGraph records into Neo4j in bounded chunks.

    0.9.0 ``consolidate-on-neo4j-source``: this class no longer
    assembles a ``GraphBuildResult`` in master's heap. Instead it
    transforms each record kind in turn and writes chunks of
    ``chunk_size`` records (default 5,000) directly into the supplied
    repository via its ``write_*_chunk`` methods.

    The repository can be either ``Neo4jGraphRepository`` (production)
    or ``InMemoryNeo4jGraphRepository`` (tests); both honour the same
    chunk-write Protocol.
    """

    def __init__(
        self,
        *,
        config: XAuditorConfig,
        repository,
        llm_client: LLMClient | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.llm_client = llm_client
        self.chunk_size = config.graph.build.neo4j_chunk_size

    def transform_and_persist(
        self,
        *,
        repo_root: Path,
        scope: RepositoryScope,
        build_fingerprint: str,
        discovered: DiscoveredGraph,
    ) -> CanonicalFinalizeResult:
        enrichment_enabled = self.config.graph.build.enable_llm_enrichment
        chunk_size = self.chunk_size

        self.repository.begin_build(
            repo_root=repo_root,
            build_fingerprint=build_fingerprint,
        )

        # Files + modules. Modules are derived from the union of file /
        # class / function module_names; we accumulate them then write.
        file_records = tuple(
            FileRecord(
                path=item.path,
                module_name=item.module_name,
                language=item.language,
                content="",
            )
            for item in discovered.files
        )
        modules_paths: dict[str, set[str]] = defaultdict(set)
        for fr in file_records:
            modules_paths[fr.module_name].add(fr.path)

        class_records = tuple(
            ClassRecord(
                class_id=item.class_id,
                name=item.name,
                file_path=item.file_path,
                module_name=item.module_name,
                start_line=item.start_line,
                end_line=item.end_line,
                summary=item.summary if enrichment_enabled else "",
                business_context=item.business_context if enrichment_enabled else "",
            )
            for item in discovered.classes
        )
        for cr in class_records:
            modules_paths[cr.module_name].add(cr.file_path)

        class_member_records = tuple(
            ClassMemberRecord(
                member_id=item.member_id,
                name=item.name,
                class_id=item.class_id,
                file_path=item.file_path,
                module_name=item.module_name,
                line_number=item.line_number,
                source="",
                summary=item.summary,
            )
            for item in discovered.class_members
        )

        function_records = tuple(
            FunctionRecord(
                function_id=function.function_id,
                name=function.name,
                qualified_name=function.qualified_name,
                file_path=function.file_path,
                module_name=function.module_name,
                start_line=function.start_line,
                end_line=function.end_line,
                source="",
                summary=function.summary if enrichment_enabled else "",
                business_context=function.business_context if enrichment_enabled else "",
                trust_boundary=function.trust_boundary if enrichment_enabled else "",
                class_id=function.class_id,
            )
            for function in discovered.functions
        )
        for fn in function_records:
            modules_paths[fn.module_name].add(fn.file_path)

        module_records = tuple(
            ModuleRecord(name=name, file_paths=tuple(sorted(modules_paths[name])))
            for name in sorted(modules_paths)
        )

        # Stream the bulk record kinds in dependency order: files (so
        # File nodes exist before Class/Function/Symbol edges merge),
        # then classes, members, functions, symbols, etc.
        self._chunked_write(
            self.repository.write_file_chunk, build_fingerprint, file_records, chunk_size
        )
        # Modules — Neo4j adapter currently treats this as a no-op
        # (modules are reconstructed from File nodes at audit time);
        # in-memory adapter accumulates the records.
        self._chunked_write(
            getattr(self.repository, "write_module_chunk", _noop_write),
            build_fingerprint,
            module_records,
            chunk_size,
        )
        self._chunked_write(
            self.repository.write_class_chunk, build_fingerprint, class_records, chunk_size
        )
        self._chunked_write(
            self.repository.write_class_member_chunk,
            build_fingerprint,
            class_member_records,
            chunk_size,
        )
        self._chunked_write(
            self.repository.write_function_chunk, build_fingerprint, function_records, chunk_size
        )

        # Build function lookup dicts now that all functions are
        # materialised — needed for callee resolution + path enumeration.
        function_by_id = {function.function_id: function for function in function_records}
        function_by_qualified_name = {
            function.qualified_name: function for function in function_records
        }
        functions_by_name: dict[str, list[FunctionRecord]] = defaultdict(list)
        for function in function_records:
            functions_by_name[function.name].append(function)

        # Module symbols + symbol uses. Symbol uses are filtered to
        # known functions/symbols, mirroring the legacy contract.
        module_symbol_records = tuple(
            ModuleSymbolRecord(
                symbol_id=item.symbol_id,
                name=item.name,
                kind=item.kind,
                module_name=item.module_name,
                file_path=item.file_path,
                start_line=item.start_line,
                end_line=item.end_line,
                type_annotation=item.type_annotation,
                value_repr=item.value_repr,
                is_placeholder=item.is_placeholder,
            )
            for item in discovered.module_symbols
        )
        self._chunked_write(
            self.repository.write_module_symbol_chunk,
            build_fingerprint,
            module_symbol_records,
            chunk_size,
        )
        symbol_ids = {item.symbol_id for item in module_symbol_records}
        function_symbol_uses = tuple(
            FunctionSymbolUseEdge(
                function_id=use.function_id,
                symbol_id=use.symbol_id,
                line_number=use.line_number,
                evidence=use.evidence,
            )
            for use in discovered.symbol_uses
            if use.symbol_id in symbol_ids and use.function_id in function_by_id
        )
        self._chunked_write(
            self.repository.write_symbol_use_chunk,
            build_fingerprint,
            function_symbol_uses,
            chunk_size,
        )

        # Resolve calls → edges, write in chunks.
        edges: list[EdgeRecord] = []
        provenance: list[GraphProvenance] = list(discovered.provenance)
        edge_buffer: list[EdgeRecord] = []
        for call in discovered.calls:
            caller = function_by_id.get(call.caller_function_id)
            callee = self._resolve_callee(
                call=call,
                caller=caller,
                function_by_qualified_name=function_by_qualified_name,
                functions_by_name=functions_by_name,
            )
            if caller is None or callee is None:
                continue
            call_provenance = GraphProvenance(
                file_path=call.file_path,
                line_number=call.line_number,
                evidence=call.evidence,
            )
            edge = EdgeRecord(
                source_function=caller.function_id,
                target_function=callee.function_id,
                edge_type=call.edge_type,
                provenance=call_provenance,
                confidence=call.confidence,
            )
            edges.append(edge)
            provenance.append(call_provenance)
            edge_buffer.append(edge)
            if len(edge_buffer) >= chunk_size:
                self.repository.write_edge_chunk(build_fingerprint, edge_buffer)
                edge_buffer = []
        if edge_buffer:
            self.repository.write_edge_chunk(build_fingerprint, edge_buffer)

        # Path enumeration uses all edges + functions in heap (bounded
        # by paths_max_count). Stream the resulting paths in chunks.
        paths_max_depth = self.config.graph.build.paths_max_depth
        paths_max_count = self.config.graph.build.paths_max_count
        path_chunk: list[PathRecord] = []
        path_records: list[PathRecord] = []
        truncated = False
        truncation_reason: str | None = None
        truncation_cap: int | None = None
        for path in self._enumerate_paths(
            function_records,
            tuple(edges),
            max_depth=paths_max_depth,
            max_count=paths_max_count,
            on_truncate=lambda reason, cap: None,
        ):
            if isinstance(path, _TruncationMarker):
                truncated = True
                truncation_reason = path.reason
                truncation_cap = path.cap
                continue
            if enrichment_enabled:
                business_context, trust_boundary = self._summarize_path(path)
                path = PathRecord(
                    entry_function=path.entry_function,
                    function_names=path.function_names,
                    file_paths=path.file_paths,
                    path_fingerprint=path.path_fingerprint,
                    function_ids=path.function_ids,
                    business_context=business_context,
                    trust_boundary=trust_boundary,
                )
            path_chunk.append(path)
            path_records.append(path)
            if len(path_chunk) >= chunk_size:
                self.repository.write_path_chunk(build_fingerprint, path_chunk)
                path_chunk = []
        if path_chunk:
            self.repository.write_path_chunk(build_fingerprint, path_chunk)

        # Coverage baseline — derived from the records we just wrote.
        coverage = self._build_coverage(
            scope,
            module_records,
            file_records,
            class_records,
            class_member_records,
            function_records,
            tuple(path_records),
        )
        self.repository.finalize_build(
            build_fingerprint=build_fingerprint,
            paths_truncated=truncated,
            paths_truncation_reason=truncation_reason,
            paths_truncation_cap=truncation_cap,
            coverage=coverage,
        )

        return CanonicalFinalizeResult(
            build_fingerprint=build_fingerprint,
            paths_truncated=truncated,
            paths_truncation_reason=truncation_reason,
            paths_truncation_cap=truncation_cap,
            lsp_messages=discovered.lsp_messages,
        )

    @staticmethod
    def _chunked_write(
        write_fn,
        build_fingerprint: str,
        records: Sequence,
        chunk_size: int,
    ) -> None:
        if not records:
            return
        for start in range(0, len(records), chunk_size):
            write_fn(build_fingerprint, records[start : start + chunk_size])

    def _resolve_callee(
        self,
        *,
        call: DiscoveredCall,
        caller: FunctionRecord | None,
        function_by_qualified_name: dict[str, FunctionRecord],
        functions_by_name: dict[str, list[FunctionRecord]],
    ) -> FunctionRecord | None:
        if call.callee_qualified_name:
            callee = function_by_qualified_name.get(call.callee_qualified_name)
            if callee is not None:
                return callee
        candidates = functions_by_name.get(call.callee_name, [])
        if len(candidates) == 1:
            return candidates[0]
        if caller is not None and caller.class_id is not None:
            for candidate in candidates:
                if candidate.class_id == caller.class_id:
                    return candidate
        return None

    def _enumerate_paths(
        self,
        functions: tuple[FunctionRecord, ...],
        edges: tuple[EdgeRecord, ...],
        *,
        max_depth: int,
        max_count: int,
        on_truncate,
    ) -> Iterator:
        """Yield ``PathRecord`` objects (and at most one
        ``_TruncationMarker`` to signal truncation) lazily so the
        caller can stream them into chunked writes.
        """

        if not functions:
            return
        function_by_id = {function.function_id: function for function in functions}
        outgoing_sorted: dict[str, tuple[str, ...]] = {}
        outgoing_raw: dict[str, list[str]] = defaultdict(list)
        inbound: set[str] = set()
        for edge in edges:
            outgoing_raw[edge.source_function].append(edge.target_function)
            inbound.add(edge.target_function)
        for source, targets in outgoing_raw.items():
            outgoing_sorted[source] = tuple(sorted(set(targets)))
        entry_ids = [
            function.function_id
            for function in sorted(functions, key=lambda item: item.qualified_name)
            if function.function_id not in inbound
        ]
        if not entry_ids:
            entry_ids = [
                function.function_id
                for function in sorted(functions, key=lambda item: item.qualified_name)
            ]

        seen: set[str] = set()
        emitted = 0
        truncation_emitted = False

        def _emit(entry_id: str, stack: list[str]):
            nonlocal emitted
            entry_function = function_by_id[entry_id].qualified_name
            function_ids = tuple(stack)
            path_fingerprint = self._path_fingerprint(entry_function, function_ids)
            if path_fingerprint in seen:
                return None
            seen.add(path_fingerprint)
            function_names = tuple(
                function_by_id[item].qualified_name for item in function_ids
            )
            file_paths = tuple(function_by_id[item].file_path for item in function_ids)
            emitted += 1
            return PathRecord(
                entry_function=entry_function,
                function_names=function_names,
                file_paths=file_paths,
                path_fingerprint=path_fingerprint,
                function_ids=function_ids,
            )

        count_cap_hit = False
        depth_cap_hit = False
        for entry_id in entry_ids:
            if count_cap_hit:
                break
            stack: list[str] = [entry_id]
            visited: set[str] = {entry_id}
            iter_stack: list = [iter(outgoing_sorted.get(entry_id, ()))]
            while stack:
                if emitted >= max_count:
                    if not truncation_emitted:
                        yield _TruncationMarker(reason="max_count_reached", cap=max_count)
                        truncation_emitted = True
                    count_cap_hit = True
                    break
                next_id = next(iter_stack[-1], None)
                if next_id is None:
                    if len(outgoing_sorted.get(stack[-1], ())) == 0:
                        result = _emit(entry_id, stack)
                        if result is not None:
                            yield result
                    popped = stack.pop()
                    visited.discard(popped)
                    iter_stack.pop()
                    continue
                if next_id in visited:
                    continue
                if len(stack) >= max_depth:
                    result = _emit(entry_id, stack)
                    if result is not None:
                        yield result
                    if not depth_cap_hit and not truncation_emitted:
                        yield _TruncationMarker(reason="max_depth_reached", cap=max_depth)
                        truncation_emitted = True
                        depth_cap_hit = True
                    continue
                stack.append(next_id)
                visited.add(next_id)
                iter_stack.append(iter(outgoing_sorted.get(next_id, ())))

    def _summarize_path(self, path: PathRecord) -> tuple[str, str]:
        if self.llm_client is not None:
            enrichment = self.llm_client.summarize_path(path.entry_function, path.function_names)
            return enrichment["business_context"], enrichment["trust_boundary"]
        joined = " -> ".join(path.function_names)
        trust_boundary = (
            "crosses external input"
            if any("handler" in name.lower() or "request" in name.lower() for name in path.function_names)
            else "internal flow"
        )
        return f"Path from {path.entry_function} through {joined}.", trust_boundary

    def _build_coverage(
        self,
        scope: RepositoryScope,
        modules: tuple[ModuleRecord, ...],
        files: tuple[FileRecord, ...],
        classes: tuple[ClassRecord, ...],
        class_members: tuple[ClassMemberRecord, ...],
        functions: tuple[FunctionRecord, ...],
        paths: tuple[PathRecord, ...],
    ) -> CoverageInventory:
        coverage = CoverageInventory()
        for module in modules:
            coverage.add(CoverageRecord(category="module", identifier=module.name, state=CoverageState.NOT_AUDITED))
        for file_record in files:
            coverage.add(CoverageRecord(category="file", identifier=file_record.path, state=CoverageState.NOT_AUDITED))
        for class_record in classes:
            coverage.add(CoverageRecord(category="class", identifier=class_record.class_id, state=CoverageState.NOT_AUDITED))
        for class_member in class_members:
            coverage.add(
                CoverageRecord(
                    category="class_member",
                    identifier=class_member.member_id,
                    state=CoverageState.NOT_AUDITED,
                )
            )
        for function in functions:
            coverage.add(
                CoverageRecord(
                    category="function",
                    identifier=function.qualified_name,
                    state=CoverageState.NOT_AUDITED,
                )
            )
        for path in paths:
            coverage.add(CoverageRecord(category="path", identifier=path.path_fingerprint, state=CoverageState.NOT_AUDITED))
        for excluded in scope.excluded_files:
            coverage.add(
                CoverageRecord(
                    category="file",
                    identifier=excluded.relative_to(scope.repo_root).as_posix(),
                    state=CoverageState.EXCLUDED,
                )
            )
        return coverage

    def _path_fingerprint(self, entry_function: str, function_ids: tuple[str, ...]) -> str:
        digest = hashlib.sha256()
        digest.update(entry_function.encode("utf-8"))
        digest.update("|".join(function_ids).encode("utf-8"))
        return digest.hexdigest()

    def _stable_hash(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _TruncationMarker:
    reason: str
    cap: int


def _noop_write(build_fingerprint, records) -> None:
    """Fallback for repositories that don't expose a particular
    ``write_*_chunk`` method (e.g. real Neo4j has no separate Module
    label so ``write_module_chunk`` is a no-op there)."""

    del build_fingerprint, records
