from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any, Iterator, Sequence


@dataclass(frozen=True)
class GraphBuildSummary:
    build_fingerprint: str
    created_at: int
    status: str


@dataclass
class _StoredBuild:
    """Internal storage for ``InMemoryNeo4jAdapter`` chunk-write accumulator.

    Holds the per-record-kind tuples accumulated by the streaming
    ``write_*_chunk`` methods. Replaces the pre-0.9.0 monolithic
    ``GraphBuildResult`` payload that used to be stashed in
    ``InMemoryNeo4jAdapter.graphs``.
    """

    repo_root: Path
    build_fingerprint: str
    scope: "RepositoryScope | None" = None
    modules: tuple["ModuleRecord", ...] = ()
    files: tuple["FileRecord", ...] = ()
    classes: tuple["ClassRecord", ...] = ()
    class_members: tuple["ClassMemberRecord", ...] = ()
    functions: tuple["FunctionRecord", ...] = ()
    paths: tuple["PathRecord", ...] = ()
    edges: tuple["EdgeRecord", ...] = ()
    provenance: tuple["GraphProvenance", ...] = ()
    module_symbols: tuple["ModuleSymbolRecord", ...] = ()
    function_symbol_uses: tuple["FunctionSymbolUseEdge", ...] = ()
    coverage: "CoverageInventory | None" = None
    paths_truncated: bool = False
    paths_truncation_reason: str | None = None
    paths_truncation_cap: int | None = None


@dataclass(frozen=True)
class FunctionRef:
    function_id: str
    name: str
    qualified_name: str
    file_path: str
    start_line: int
    end_line: int


@dataclass(frozen=True)
class PathSummary:
    path_fingerprint: str
    entry_function: str
    function_chain: tuple[str, ...]


@dataclass(frozen=True)
class CallStackNode:
    function: FunctionRef
    children: tuple["CallStackNode", ...]
    cycle: bool = False
    truncated: bool = False


def _row_to_function(row: dict[str, Any]) -> FunctionRef:
    return FunctionRef(
        function_id=row["function_id"],
        name=row["name"],
        qualified_name=row["qualified_name"],
        file_path=row["file_path"],
        start_line=int(row["start_line"]),
        end_line=int(row["end_line"]),
    )

from xauditor.config import Neo4jConfig
from xauditor.integrations.neo4j import InMemoryNeo4jAdapter
from xauditor.integrations.neo4j_driver import Neo4jDriverProtocol
from xauditor.models import (
    ClassMemberRecord,
    ClassRecord,
    ConfidenceLevel,
    CoverageInventory,
    CoverageRecord,
    CoverageState,
    EdgeRecord,
    FileRecord,
    FunctionRecord,
    FunctionSymbolUseEdge,
    GraphProvenance,
    ModuleRecord,
    ModuleSymbolRecord,
    PathRecord,
    RepositoryScope,
)


class Neo4jGraphRepository:
    query_timeout = 30

    def __init__(self, *, config: Neo4jConfig, driver: Neo4jDriverProtocol) -> None:
        self.config = config
        self.driver = driver

    def get_latest_build(self, repo_root: Path) -> str:
        rows = self._rows(
            "MATCH (:Repository {name: $name})-[:HAS_BUILD]->(build:GraphBuild {status: 'ready'}) "
            "RETURN build.build_fingerprint AS build_fingerprint "
            "ORDER BY build.created_at DESC LIMIT 1",
            {"name": repo_root.name or str(repo_root)},
        )
        if not rows:
            raise FileNotFoundError("No graph build is available in Neo4j yet. Run `xauditor graph build` first.")
        return rows[0]["build_fingerprint"]

    def list_builds(self, repo_root: Path) -> list[GraphBuildSummary]:
        rows = self._rows(
            "MATCH (:Repository {name: $name})-[:HAS_BUILD]->(build:GraphBuild) "
            "RETURN build.build_fingerprint AS build_fingerprint, "
            "build.created_at AS created_at, build.status AS status "
            "ORDER BY build.created_at DESC",
            {"name": repo_root.name or str(repo_root)},
        )
        return [
            GraphBuildSummary(
                build_fingerprint=row["build_fingerprint"],
                created_at=int(row.get("created_at") or 0),
                status=row.get("status") or "",
            )
            for row in rows
        ]

    def require_build(self, build_fingerprint: str) -> None:
        rows = self._rows(
            "MATCH (build:GraphBuild {build_fingerprint: $bf}) "
            "RETURN build.status AS status LIMIT 1",
            {"bf": build_fingerprint},
        )
        if not rows:
            raise FileNotFoundError(f"Graph build `{build_fingerprint}` was not found in Neo4j.")
        if (rows[0].get("status") or "") != "ready":
            raise FileNotFoundError(f"Graph build `{build_fingerprint}` is not in a ready state.")

    def find_functions(self, build_fingerprint: str, query: str) -> list[FunctionRef]:
        rows = self._rows(
            "MATCH (fn:Function {build_fingerprint: $bf}) "
            "WHERE fn.name = $q OR fn.qualified_name = $q "
            "RETURN fn.function_id AS function_id, fn.name AS name, "
            "fn.qualified_name AS qualified_name, fn.file_path AS file_path, "
            "fn.start_line AS start_line, fn.end_line AS end_line "
            "ORDER BY fn.qualified_name",
            {"bf": build_fingerprint, "q": query},
        )
        return [
            FunctionRef(
                function_id=row["function_id"],
                name=row["name"],
                qualified_name=row["qualified_name"],
                file_path=row["file_path"],
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
            )
            for row in rows
        ]

    def get_call_stack(
        self,
        build_fingerprint: str,
        function_id: str,
        *,
        max_depth: int = 20,
    ) -> CallStackNode:
        rows = self._rows(
            "MATCH (fn:Function {build_fingerprint: $bf, function_id: $fid}) "
            "RETURN fn.function_id AS function_id, fn.name AS name, "
            "fn.qualified_name AS qualified_name, fn.file_path AS file_path, "
            "fn.start_line AS start_line, fn.end_line AS end_line LIMIT 1",
            {"bf": build_fingerprint, "fid": function_id},
        )
        if not rows:
            raise FileNotFoundError(f"Function `{function_id}` not found in build `{build_fingerprint}`.")
        root = _row_to_function(rows[0])
        return self._expand_node(build_fingerprint, root, visited=frozenset({root.function_id}), depth=0, max_depth=max_depth)

    def _expand_node(
        self,
        build_fingerprint: str,
        function: FunctionRef,
        *,
        visited: frozenset[str],
        depth: int,
        max_depth: int,
    ) -> CallStackNode:
        if depth >= max_depth:
            return CallStackNode(function=function, children=(), truncated=True)
        rows = self._rows(
            "MATCH (:Function {build_fingerprint: $bf, function_id: $fid})"
            "-[:CALLS]->(target:Function {build_fingerprint: $bf}) "
            "RETURN DISTINCT target.function_id AS function_id, target.name AS name, "
            "target.qualified_name AS qualified_name, target.file_path AS file_path, "
            "target.start_line AS start_line, target.end_line AS end_line "
            "ORDER BY target.qualified_name",
            {"bf": build_fingerprint, "fid": function.function_id},
        )
        children: list[CallStackNode] = []
        for row in rows:
            target = _row_to_function(row)
            if target.function_id in visited:
                children.append(CallStackNode(function=target, children=(), cycle=True))
                continue
            children.append(
                self._expand_node(
                    build_fingerprint,
                    target,
                    visited=visited | {target.function_id},
                    depth=depth + 1,
                    max_depth=max_depth,
                )
            )
        return CallStackNode(function=function, children=tuple(children))

    def list_paths(self, build_fingerprint: str) -> list[PathSummary]:
        path_rows = self._rows(
            "MATCH (p:Path {build_fingerprint: $bf}) "
            "RETURN p.path_fingerprint AS path_fingerprint, p.entry_function AS entry_function "
            "ORDER BY p.path_fingerprint",
            {"bf": build_fingerprint},
        )
        include_rows = self._rows(
            "MATCH (p:Path {build_fingerprint: $bf})-[r:INCLUDES]->(fn:Function {build_fingerprint: $bf}) "
            "RETURN p.path_fingerprint AS path_fingerprint, r.position AS position, "
            "fn.qualified_name AS qualified_name "
            "ORDER BY p.path_fingerprint, r.position",
            {"bf": build_fingerprint},
        )
        membership: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for row in include_rows:
            membership[row["path_fingerprint"]].append((int(row["position"]), row["qualified_name"]))
        return [
            PathSummary(
                path_fingerprint=row["path_fingerprint"],
                entry_function=row.get("entry_function") or "",
                function_chain=tuple(
                    name for _pos, name in sorted(membership.get(row["path_fingerprint"], ()))
                ),
            )
            for row in path_rows
        ]

    def _load_repo_root(self, build_fingerprint: str) -> Path:
        rows = self._rows(
            "MATCH (build:GraphBuild {build_fingerprint: $bf}) RETURN build.repo_path AS repo_path LIMIT 1",
            {"bf": build_fingerprint},
        )
        if not rows:
            raise FileNotFoundError(f"Graph build `{build_fingerprint}` was not found in Neo4j.")
        return Path(rows[0]["repo_path"])

    def _load_modules(self, build_fingerprint: str, files: tuple[FileRecord, ...]) -> tuple[ModuleRecord, ...]:
        del build_fingerprint
        grouped_paths: dict[str, list[str]] = defaultdict(list)
        for item in files:
            grouped_paths[item.module_name].append(item.path)
        return tuple(
            ModuleRecord(name=name, file_paths=tuple(sorted(grouped_paths[name])))
            for name in sorted(grouped_paths)
        )

    def _load_files(self, build_fingerprint: str) -> tuple[FileRecord, ...]:
        rows = self._rows(
            "MATCH (f:File {build_fingerprint: $bf}) "
            "RETURN f.path AS path, f.module_name AS module_name, f.language AS language "
            "ORDER BY f.path",
            {"bf": build_fingerprint},
        )
        return tuple(
            FileRecord(
                path=row["path"],
                module_name=row["module_name"],
                language=row["language"],
                content="",
            )
            for row in rows
        )

    def _load_classes(self, build_fingerprint: str) -> tuple[ClassRecord, ...]:
        rows = self._rows(
            "MATCH (c:Class {build_fingerprint: $bf}) "
            "RETURN c.class_id AS class_id, c.name AS name, c.file_path AS file_path, "
            "c.module_name AS module_name, c.start_line AS start_line, c.end_line AS end_line, "
            "c.summary AS summary, c.business_context AS business_context "
            "ORDER BY c.class_id",
            {"bf": build_fingerprint},
        )
        return tuple(
            ClassRecord(
                class_id=row["class_id"],
                name=row["name"],
                file_path=row["file_path"],
                module_name=row["module_name"],
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
                summary=row.get("summary") or "",
                business_context=row.get("business_context") or "",
            )
            for row in rows
        )

    def _load_class_members(self, build_fingerprint: str) -> tuple[ClassMemberRecord, ...]:
        rows = self._rows(
            "MATCH (m:ClassMember {build_fingerprint: $bf}) "
            "RETURN m.member_id AS member_id, m.name AS name, m.class_id AS class_id, "
            "m.file_path AS file_path, m.module_name AS module_name, "
            "m.line_number AS line_number, m.summary AS summary "
            "ORDER BY m.member_id",
            {"bf": build_fingerprint},
        )
        return tuple(
            ClassMemberRecord(
                member_id=row["member_id"],
                name=row["name"],
                class_id=row["class_id"],
                file_path=row["file_path"],
                module_name=row["module_name"],
                line_number=int(row["line_number"]),
                source="",
                summary=row.get("summary") or "",
            )
            for row in rows
        )

    def _load_functions(self, build_fingerprint: str) -> tuple[FunctionRecord, ...]:
        rows = self._rows(
            "MATCH (fn:Function {build_fingerprint: $bf}) "
            "RETURN fn.function_id AS function_id, fn.name AS name, fn.qualified_name AS qualified_name, "
            "fn.file_path AS file_path, fn.module_name AS module_name, fn.start_line AS start_line, "
            "fn.end_line AS end_line, fn.summary AS summary, fn.business_context AS business_context, "
            "fn.trust_boundary AS trust_boundary, fn.class_id AS class_id "
            "ORDER BY fn.function_id",
            {"bf": build_fingerprint},
        )
        return tuple(
            FunctionRecord(
                function_id=row["function_id"],
                name=row["name"],
                qualified_name=row["qualified_name"],
                file_path=row["file_path"],
                module_name=row["module_name"],
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
                source="",
                summary=row.get("summary") or "",
                business_context=row.get("business_context") or "",
                trust_boundary=row.get("trust_boundary") or "",
                class_id=row.get("class_id") or None,
            )
            for row in rows
        )

    def _load_edges(self, build_fingerprint: str) -> tuple[EdgeRecord, ...]:
        rows = self._rows(
            "MATCH (source:Function {build_fingerprint: $bf})-[r:CALLS]->(target:Function {build_fingerprint: $bf}) "
            "RETURN source.function_id AS source_function, target.function_id AS target_function, "
            "r.edge_type AS edge_type, r.file_path AS file_path, r.line_number AS line_number, "
            "r.evidence AS evidence, r.confidence AS confidence "
            "ORDER BY source.function_id, target.function_id",
            {"bf": build_fingerprint},
        )
        edges: list[EdgeRecord] = []
        for row in rows:
            try:
                confidence_level = ConfidenceLevel(row.get("confidence") or "")
            except ValueError:
                confidence_level = ConfidenceLevel.MEDIUM
            edges.append(
                EdgeRecord(
                    source_function=row["source_function"],
                    target_function=row["target_function"],
                    edge_type=row.get("edge_type") or "calls",
                    provenance=GraphProvenance(
                        file_path=row.get("file_path") or "",
                        line_number=int(row.get("line_number") or 0),
                        evidence=row.get("evidence") or "",
                    ),
                    confidence=confidence_level,
                )
            )
        return tuple(edges)

    def _load_paths(
        self,
        build_fingerprint: str,
        functions: tuple[FunctionRecord, ...],
    ) -> tuple[PathRecord, ...]:
        function_by_id = {item.function_id: item for item in functions}
        path_rows = self._rows(
            "MATCH (p:Path {build_fingerprint: $bf}) "
            "RETURN p.path_fingerprint AS path_fingerprint, p.entry_function AS entry_function, "
            "p.business_context AS business_context, p.trust_boundary AS trust_boundary "
            "ORDER BY p.path_fingerprint",
            {"bf": build_fingerprint},
        )
        include_rows = self._rows(
            "MATCH (p:Path {build_fingerprint: $bf})-[r:INCLUDES]->(fn:Function {build_fingerprint: $bf}) "
            "RETURN p.path_fingerprint AS path_fingerprint, r.position AS position, fn.function_id AS function_id "
            "ORDER BY p.path_fingerprint, r.position",
            {"bf": build_fingerprint},
        )
        membership: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for row in include_rows:
            membership[row["path_fingerprint"]].append((int(row["position"]), row["function_id"]))
        paths: list[PathRecord] = []
        for row in path_rows:
            path_fingerprint = row["path_fingerprint"]
            ordered_function_ids = tuple(
                function_id for _position, function_id in sorted(membership.get(path_fingerprint, ()))
            )
            function_names = tuple(
                function_by_id[function_id].qualified_name
                for function_id in ordered_function_ids
                if function_id in function_by_id
            )
            file_paths = tuple(
                function_by_id[function_id].file_path
                for function_id in ordered_function_ids
                if function_id in function_by_id
            )
            paths.append(
                PathRecord(
                    entry_function=row["entry_function"],
                    function_names=function_names,
                    file_paths=file_paths,
                    path_fingerprint=path_fingerprint,
                    function_ids=ordered_function_ids,
                    business_context=row.get("business_context") or "",
                    trust_boundary=row.get("trust_boundary") or "",
                )
            )
        return tuple(paths)

    def _load_module_symbols(self, build_fingerprint: str) -> tuple[ModuleSymbolRecord, ...]:
        rows = self._rows(
            "MATCH (s:ModuleSymbol {build_fingerprint: $bf}) "
            "RETURN s.symbol_id AS symbol_id, s.name AS name, s.kind AS kind, "
            "s.module_name AS module_name, s.file_path AS file_path, "
            "s.start_line AS start_line, s.end_line AS end_line, "
            "s.type_annotation AS type_annotation, s.value_repr AS value_repr, "
            "s.is_placeholder AS is_placeholder "
            "ORDER BY s.symbol_id",
            {"bf": build_fingerprint},
        )
        return tuple(
            ModuleSymbolRecord(
                symbol_id=row["symbol_id"],
                name=row["name"],
                kind=row.get("kind") or "",
                module_name=row.get("module_name") or "",
                file_path=row.get("file_path") or "",
                start_line=int(row.get("start_line") or 0),
                end_line=int(row.get("end_line") or 0),
                type_annotation=row.get("type_annotation") or "",
                value_repr=row.get("value_repr") or "",
                is_placeholder=bool(row.get("is_placeholder")),
            )
            for row in rows
        )

    def _load_function_symbol_uses(self, build_fingerprint: str) -> tuple[FunctionSymbolUseEdge, ...]:
        rows = self._rows(
            "MATCH (fn:Function {build_fingerprint: $bf})-[r:USES_SYMBOL]->"
            "(s:ModuleSymbol {build_fingerprint: $bf}) "
            "RETURN fn.function_id AS function_id, s.symbol_id AS symbol_id, "
            "r.line_number AS line_number, r.evidence AS evidence "
            "ORDER BY fn.function_id, s.symbol_id",
            {"bf": build_fingerprint},
        )
        return tuple(
            FunctionSymbolUseEdge(
                function_id=row["function_id"],
                symbol_id=row["symbol_id"],
                line_number=int(row.get("line_number") or 0),
                evidence=row.get("evidence") or "",
            )
            for row in rows
        )

    def iter_path_records(
        self,
        build_fingerprint: str,
        *,
        page_size: int = 100,
    ) -> "Iterator[PathRecord]":
        from xauditor.models import PathRecord

        offset = 0
        while True:
            path_rows = self._rows(
                "MATCH (p:Path {build_fingerprint: $bf}) "
                "RETURN p.path_fingerprint AS path_fingerprint, p.entry_function AS entry_function, "
                "p.business_context AS business_context, p.trust_boundary AS trust_boundary "
                "ORDER BY p.path_fingerprint SKIP $skip LIMIT $limit",
                {"bf": build_fingerprint, "skip": offset, "limit": page_size},
            )
            if not path_rows:
                return
            fingerprints = [row["path_fingerprint"] for row in path_rows]
            include_rows = self._rows(
                "MATCH (p:Path {build_fingerprint: $bf})-[r:INCLUDES]->(fn:Function {build_fingerprint: $bf}) "
                "WHERE p.path_fingerprint IN $fps "
                "RETURN p.path_fingerprint AS path_fingerprint, r.position AS position, "
                "fn.function_id AS function_id, fn.qualified_name AS qualified_name, "
                "fn.file_path AS file_path "
                "ORDER BY p.path_fingerprint, r.position",
                {"bf": build_fingerprint, "fps": fingerprints},
            )
            membership: dict[str, list[tuple[int, str, str, str]]] = defaultdict(list)
            for row in include_rows:
                membership[row["path_fingerprint"]].append(
                    (
                        int(row["position"]),
                        row["function_id"],
                        row.get("qualified_name") or "",
                        row.get("file_path") or "",
                    )
                )
            for row in path_rows:
                fingerprint = row["path_fingerprint"]
                ordered = sorted(membership.get(fingerprint, ()))
                yield PathRecord(
                    entry_function=row.get("entry_function") or "",
                    function_names=tuple(name for _pos, _fid, name, _fp in ordered),
                    file_paths=tuple(fp for _pos, _fid, _name, fp in ordered),
                    path_fingerprint=fingerprint,
                    function_ids=tuple(fid for _pos, fid, _name, _fp in ordered),
                    business_context=row.get("business_context") or "",
                    trust_boundary=row.get("trust_boundary") or "",
                )
            offset += len(path_rows)

    def count_paths(self, build_fingerprint: str) -> int:
        rows = self._rows(
            "MATCH (p:Path {build_fingerprint: $bf}) RETURN count(p) AS n",
            {"bf": build_fingerprint},
        )
        return int(rows[0]["n"]) if rows else 0

    def load_functions_by_ids(
        self, build_fingerprint: str, function_ids: Sequence[str]
    ) -> list[FunctionRecord]:
        if not function_ids:
            return []
        rows = self._rows(
            "MATCH (fn:Function {build_fingerprint: $bf}) "
            "WHERE fn.function_id IN $ids "
            "RETURN fn.function_id AS function_id, fn.name AS name, fn.qualified_name AS qualified_name, "
            "fn.file_path AS file_path, fn.module_name AS module_name, fn.start_line AS start_line, "
            "fn.end_line AS end_line, fn.summary AS summary, fn.business_context AS business_context, "
            "fn.trust_boundary AS trust_boundary, fn.class_id AS class_id",
            {"bf": build_fingerprint, "ids": list(function_ids)},
        )
        by_id = {
            row["function_id"]: FunctionRecord(
                function_id=row["function_id"],
                name=row["name"],
                qualified_name=row["qualified_name"],
                file_path=row["file_path"],
                module_name=row["module_name"],
                start_line=int(row["start_line"]),
                end_line=int(row["end_line"]),
                source="",
                summary=row.get("summary") or "",
                business_context=row.get("business_context") or "",
                trust_boundary=row.get("trust_boundary") or "",
                class_id=row.get("class_id") or None,
            )
            for row in rows
        }
        return [by_id[function_id] for function_id in function_ids if function_id in by_id]

    def load_symbols_for_functions(
        self, build_fingerprint: str, function_ids: Sequence[str]
    ) -> tuple[tuple["ModuleSymbolRecord", ...], tuple["FunctionSymbolUseEdge", ...]]:
        from xauditor.models import FunctionSymbolUseEdge, ModuleSymbolRecord

        if not function_ids:
            return (), ()
        use_rows = self._rows(
            "MATCH (fn:Function {build_fingerprint: $bf})-[r:USES_SYMBOL]->"
            "(s:ModuleSymbol {build_fingerprint: $bf}) "
            "WHERE fn.function_id IN $ids "
            "RETURN fn.function_id AS function_id, s.symbol_id AS symbol_id, "
            "r.line_number AS line_number, r.evidence AS evidence, "
            "s.name AS name, s.kind AS kind, s.module_name AS module_name, "
            "s.file_path AS file_path, s.start_line AS start_line, s.end_line AS end_line, "
            "s.type_annotation AS type_annotation, s.value_repr AS value_repr, "
            "s.is_placeholder AS is_placeholder "
            "ORDER BY fn.function_id, s.symbol_id",
            {"bf": build_fingerprint, "ids": list(function_ids)},
        )
        uses: list[FunctionSymbolUseEdge] = []
        symbols: list[ModuleSymbolRecord] = []
        seen_symbols: set[str] = set()
        for row in use_rows:
            uses.append(
                FunctionSymbolUseEdge(
                    function_id=row["function_id"],
                    symbol_id=row["symbol_id"],
                    line_number=int(row.get("line_number") or 0),
                    evidence=row.get("evidence") or "",
                )
            )
            if row["symbol_id"] in seen_symbols:
                continue
            seen_symbols.add(row["symbol_id"])
            symbols.append(
                ModuleSymbolRecord(
                    symbol_id=row["symbol_id"],
                    name=row["name"],
                    kind=row.get("kind") or "",
                    module_name=row.get("module_name") or "",
                    file_path=row.get("file_path") or "",
                    start_line=int(row.get("start_line") or 0),
                    end_line=int(row.get("end_line") or 0),
                    type_annotation=row.get("type_annotation") or "",
                    value_repr=row.get("value_repr") or "",
                    is_placeholder=bool(row.get("is_placeholder")),
                )
            )
        return tuple(symbols), tuple(uses)

    def coverage_baseline(
        self, build_fingerprint: str
    ) -> tuple[
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
        dict[str, str],
        dict[str, str],
        dict[str, str],
        tuple[str, ...],
    ]:
        file_rows = self._rows(
            "MATCH (f:File {build_fingerprint: $bf}) "
            "RETURN f.path AS path, f.module_name AS module_name "
            "ORDER BY f.path",
            {"bf": build_fingerprint},
        )
        function_rows = self._rows(
            "MATCH (fn:Function {build_fingerprint: $bf}) "
            "RETURN fn.function_id AS function_id, fn.qualified_name AS qualified_name, "
            "fn.file_path AS file_path, fn.module_name AS module_name "
            "ORDER BY fn.function_id",
            {"bf": build_fingerprint},
        )
        path_rows = self._rows(
            "MATCH (p:Path {build_fingerprint: $bf}) RETURN p.path_fingerprint AS fp ORDER BY p.path_fingerprint",
            {"bf": build_fingerprint},
        )
        modules = tuple(sorted({row.get("module_name") or "" for row in file_rows if row.get("module_name")}))
        files = tuple(row["path"] for row in file_rows)
        function_qualified_names = tuple(row["qualified_name"] for row in function_rows)
        function_to_module = {row["function_id"]: row.get("module_name") or "" for row in function_rows}
        function_to_file = {row["function_id"]: row.get("file_path") or "" for row in function_rows}
        function_to_name = {row["function_id"]: row["qualified_name"] for row in function_rows}
        paths = tuple(row["fp"] for row in path_rows)
        return modules, files, function_qualified_names, function_to_module, function_to_file, function_to_name, paths

    def _rows(self, query: str, parameters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return self.driver.read_rows(query, parameters, timeout=self.query_timeout)

    write_timeout = 300

    def _write(self, query: str, parameters: dict[str, Any] | None = None) -> None:
        self.driver.run_write(query, parameters, timeout=self.write_timeout)

    # --- Streaming chunk writers (consolidate-on-neo4j-source 0.9.0) ---
    # Each method takes a build fingerprint and a list of records and
    # issues one parameterised UNWIND-MERGE write per call. Idempotent
    # by primary key so re-running canonical_finalize after a partial
    # write is safe (build resume).

    def begin_build(
        self,
        *,
        repo_root: Path,
        build_fingerprint: str,
    ) -> None:
        repository_name = repo_root.name or str(repo_root)
        repository_full_path = str(repo_root)
        self._write(
            "MERGE (r:Repository {name: $name}) SET r.path = $path "
            "MERGE (build:GraphBuild {build_fingerprint: $bf}) "
            "ON CREATE SET build.created_at = timestamp() "
            "SET build.repo_path = $path, build.status = 'building' "
            "MERGE (r)-[:HAS_BUILD]->(build)",
            {
                "name": repository_name,
                "path": repository_full_path,
                "bf": build_fingerprint,
            },
        )

    def write_file_chunk(
        self, build_fingerprint: str, records: Sequence[FileRecord]
    ) -> None:
        if not records:
            return
        rows = [
            {
                "file_key": _scoped_key(build_fingerprint, item.path),
                "path": item.path,
                "module_name": item.module_name,
                "language": item.language,
            }
            for item in records
        ]
        self._write(
            "UNWIND $rows AS row "
            "MERGE (f:File {file_key: row.file_key}) "
            "SET f.build_fingerprint = $bf, f.path = row.path, "
            "f.module_name = row.module_name, f.language = row.language "
            "WITH f "
            "MATCH (build:GraphBuild {build_fingerprint: $bf}) "
            "MERGE (build)-[:CONTAINS_FILE]->(f)",
            {"bf": build_fingerprint, "rows": rows},
        )

    def write_class_chunk(
        self, build_fingerprint: str, records: Sequence[ClassRecord]
    ) -> None:
        if not records:
            return
        rows = [
            {
                "class_key": _scoped_key(build_fingerprint, item.class_id),
                "class_id": item.class_id,
                "name": item.name,
                "file_path": item.file_path,
                "module_name": item.module_name,
                "start_line": item.start_line,
                "end_line": item.end_line,
                "summary": item.summary,
                "business_context": item.business_context,
                "file_key": _scoped_key(build_fingerprint, item.file_path),
            }
            for item in records
        ]
        self._write(
            "UNWIND $rows AS row "
            "MERGE (c:Class {class_key: row.class_key}) "
            "SET c.build_fingerprint = $bf, c.class_id = row.class_id, "
            "c.name = row.name, c.file_path = row.file_path, "
            "c.module_name = row.module_name, c.start_line = row.start_line, "
            "c.end_line = row.end_line, c.summary = row.summary, "
            "c.business_context = row.business_context "
            "WITH c, row "
            "MATCH (f:File {file_key: row.file_key}) "
            "MERGE (f)-[:DECLARES_CLASS]->(c)",
            {"bf": build_fingerprint, "rows": rows},
        )

    def write_class_member_chunk(
        self, build_fingerprint: str, records: Sequence[ClassMemberRecord]
    ) -> None:
        if not records:
            return
        rows = [
            {
                "member_key": _scoped_key(build_fingerprint, item.member_id),
                "member_id": item.member_id,
                "name": item.name,
                "class_id": item.class_id,
                "file_path": item.file_path,
                "module_name": item.module_name,
                "line_number": item.line_number,
                "summary": item.summary,
                "class_key": _scoped_key(build_fingerprint, item.class_id),
            }
            for item in records
        ]
        self._write(
            "UNWIND $rows AS row "
            "MERGE (m:ClassMember {class_member_key: row.member_key}) "
            "SET m.build_fingerprint = $bf, m.member_id = row.member_id, "
            "m.name = row.name, m.class_id = row.class_id, "
            "m.file_path = row.file_path, m.module_name = row.module_name, "
            "m.line_number = row.line_number, m.summary = row.summary "
            "WITH m, row "
            "MATCH (c:Class {class_key: row.class_key}) "
            "MERGE (c)-[:DECLARES_MEMBER]->(m)",
            {"bf": build_fingerprint, "rows": rows},
        )

    def write_function_chunk(
        self, build_fingerprint: str, records: Sequence[FunctionRecord]
    ) -> None:
        if not records:
            return
        rows = [
            {
                "function_key": _scoped_key(build_fingerprint, item.function_id),
                "function_id": item.function_id,
                "name": item.name,
                "qualified_name": item.qualified_name,
                "file_path": item.file_path,
                "module_name": item.module_name,
                "start_line": item.start_line,
                "end_line": item.end_line,
                "summary": item.summary,
                "business_context": item.business_context,
                "trust_boundary": item.trust_boundary,
                "class_id": item.class_id or "",
                "class_key": _scoped_key(build_fingerprint, item.class_id) if item.class_id else "",
                "file_key": _scoped_key(build_fingerprint, item.file_path),
                "has_class": bool(item.class_id),
            }
            for item in records
        ]
        # Property writes for the function node (idempotent).
        self._write(
            "UNWIND $rows AS row "
            "MERGE (fn:Function {function_key: row.function_key}) "
            "SET fn.build_fingerprint = $bf, fn.function_id = row.function_id, "
            "fn.name = row.name, fn.qualified_name = row.qualified_name, "
            "fn.file_path = row.file_path, fn.module_name = row.module_name, "
            "fn.start_line = row.start_line, fn.end_line = row.end_line, "
            "fn.summary = row.summary, fn.business_context = row.business_context, "
            "fn.trust_boundary = row.trust_boundary, fn.class_id = row.class_id",
            {"bf": build_fingerprint, "rows": rows},
        )
        # Class-declares-method edges for class-scoped functions.
        class_rows = [row for row in rows if row["has_class"]]
        if class_rows:
            self._write(
                "UNWIND $rows AS row "
                "MATCH (c:Class {class_key: row.class_key}), "
                "(fn:Function {function_key: row.function_key}) "
                "MERGE (c)-[:DECLARES_METHOD]->(fn)",
                {"rows": class_rows},
            )
        # File-declares-function edges for module-scoped functions.
        file_rows = [row for row in rows if not row["has_class"]]
        if file_rows:
            self._write(
                "UNWIND $rows AS row "
                "MATCH (f:File {file_key: row.file_key}), "
                "(fn:Function {function_key: row.function_key}) "
                "MERGE (f)-[:DECLARES_FUNCTION]->(fn)",
                {"rows": file_rows},
            )

    def write_module_symbol_chunk(
        self, build_fingerprint: str, records: Sequence[ModuleSymbolRecord]
    ) -> None:
        if not records:
            return
        rows = [
            {
                "symbol_key": _scoped_key(build_fingerprint, item.symbol_id),
                "symbol_id": item.symbol_id,
                "name": item.name,
                "kind": item.kind,
                "module_name": item.module_name,
                "file_path": item.file_path,
                "start_line": item.start_line,
                "end_line": item.end_line,
                "type_annotation": item.type_annotation,
                "value_repr": item.value_repr,
                "is_placeholder": item.is_placeholder,
                "file_key": _scoped_key(build_fingerprint, item.file_path),
            }
            for item in records
        ]
        self._write(
            "UNWIND $rows AS row "
            "MERGE (s:ModuleSymbol {symbol_key: row.symbol_key}) "
            "SET s.build_fingerprint = $bf, s.symbol_id = row.symbol_id, "
            "s.name = row.name, s.kind = row.kind, "
            "s.module_name = row.module_name, s.file_path = row.file_path, "
            "s.start_line = row.start_line, s.end_line = row.end_line, "
            "s.type_annotation = row.type_annotation, s.value_repr = row.value_repr, "
            "s.is_placeholder = row.is_placeholder "
            "WITH s, row "
            "MATCH (f:File {file_key: row.file_key}) "
            "MERGE (f)-[:DECLARES_SYMBOL]->(s)",
            {"bf": build_fingerprint, "rows": rows},
        )

    def write_symbol_use_chunk(
        self, build_fingerprint: str, records: Sequence[FunctionSymbolUseEdge]
    ) -> None:
        if not records:
            return
        rows = [
            {
                "function_key": _scoped_key(build_fingerprint, item.function_id),
                "symbol_key": _scoped_key(build_fingerprint, item.symbol_id),
                "line_number": item.line_number,
                "evidence": item.evidence,
            }
            for item in records
        ]
        self._write(
            "UNWIND $rows AS row "
            "MATCH (fn:Function {function_key: row.function_key}), "
            "(s:ModuleSymbol {symbol_key: row.symbol_key}) "
            "MERGE (fn)-[r:USES_SYMBOL]->(s) "
            "SET r.line_number = row.line_number, r.evidence = row.evidence",
            {"rows": rows},
        )

    def write_edge_chunk(
        self, build_fingerprint: str, records: Sequence[EdgeRecord]
    ) -> None:
        if not records:
            return
        rows = [
            {
                "source_key": _scoped_key(build_fingerprint, item.source_function),
                "target_key": _scoped_key(build_fingerprint, item.target_function),
                "edge_type": item.edge_type,
                "file_path": item.provenance.file_path,
                "evidence": item.provenance.evidence,
                "line_number": item.provenance.line_number,
                "confidence": item.confidence.value,
            }
            for item in records
        ]
        self._write(
            "UNWIND $rows AS row "
            "MATCH (source:Function {function_key: row.source_key}), "
            "(target:Function {function_key: row.target_key}) "
            "MERGE (source)-[r:CALLS]->(target) "
            "SET r.file_path = row.file_path, r.evidence = row.evidence, "
            "r.line_number = row.line_number, r.edge_type = row.edge_type, "
            "r.confidence = row.confidence",
            {"rows": rows},
        )

    def write_module_chunk(
        self, build_fingerprint: str, records: Sequence[ModuleRecord]
    ) -> None:
        # Modules are derived from File records' module_name groupings;
        # they exist in the graph as a logical aggregation, not as a
        # distinct Neo4j label today. The module aggregation is
        # reconstructed at audit time via ``coverage_baseline`` from
        # File rows, so we don't need a separate :Module node. This
        # method exists for API parity with the other writers — call
        # sites can stream module chunks alongside files; the
        # implementation here is intentionally a no-op for the Neo4j
        # backend.
        del build_fingerprint, records

    def write_path_chunk(
        self, build_fingerprint: str, records: Sequence[PathRecord]
    ) -> None:
        if not records:
            return
        # First the path nodes + build linkage.
        path_rows = [
            {
                "path_key": _scoped_key(build_fingerprint, item.path_fingerprint),
                "path_fingerprint": item.path_fingerprint,
                "entry_function": item.entry_function,
                "business_context": item.business_context,
                "trust_boundary": item.trust_boundary,
            }
            for item in records
        ]
        self._write(
            "UNWIND $rows AS row "
            "MERGE (p:Path {path_key: row.path_key}) "
            "SET p.build_fingerprint = $bf, "
            "p.path_fingerprint = row.path_fingerprint, "
            "p.entry_function = row.entry_function, "
            "p.business_context = row.business_context, "
            "p.trust_boundary = row.trust_boundary "
            "WITH p "
            "MATCH (build:GraphBuild {build_fingerprint: $bf}) "
            "MERGE (build)-[:CONTAINS_PATH]->(p)",
            {"bf": build_fingerprint, "rows": path_rows},
        )
        # Then the INCLUDES edges (one per (path, function, position)).
        include_rows: list[dict[str, Any]] = []
        for item in records:
            path_key = _scoped_key(build_fingerprint, item.path_fingerprint)
            for position, function_id in enumerate(item.function_ids):
                include_rows.append(
                    {
                        "path_key": path_key,
                        "function_key": _scoped_key(build_fingerprint, function_id),
                        "position": position,
                    }
                )
        if include_rows:
            self._write(
                "UNWIND $rows AS row "
                "MATCH (p:Path {path_key: row.path_key}), "
                "(fn:Function {function_key: row.function_key}) "
                "MERGE (p)-[r:INCLUDES {position: row.position}]->(fn)",
                {"rows": include_rows},
            )

    def finalize_build(
        self,
        *,
        build_fingerprint: str,
        paths_truncated: bool = False,
        paths_truncation_reason: str | None = None,
        paths_truncation_cap: int | None = None,
        coverage: CoverageInventory | None = None,
    ) -> None:
        """Mark the build ready and stamp aggregate metadata onto (b:Build).

        Called once at the end of ``canonical_finalize`` to flip the
        build's ``status`` to ``ready`` and persist truncation flags +
        coverage payload (a JSON-serialised summary) for the
        audit-time ``coverage_inventory()`` query.
        """

        # Coverage payload — a flat JSON envelope of the per-category
        # records (category, identifier, state). Audit-time
        # ``Neo4jAuditGraphSource.coverage_inventory()`` reads this
        # back into a ``CoverageInventory``.
        import json as _json

        coverage_payload = ""
        if coverage is not None and coverage.records:
            coverage_payload = _json.dumps(
                {
                    category: [
                        {
                            "category": record.category,
                            "identifier": record.identifier,
                            "state": record.state.value,
                        }
                        for record in records
                    ]
                    for category, records in coverage.records.items()
                }
            )
        self._write(
            "MATCH (build:GraphBuild {build_fingerprint: $bf}) "
            "SET build.status = 'ready', "
            "build.paths_truncated = $truncated, "
            "build.paths_truncation_reason = $reason, "
            "build.paths_truncation_cap = $cap, "
            "build.coverage_payload = $coverage",
            {
                "bf": build_fingerprint,
                "truncated": paths_truncated,
                "reason": paths_truncation_reason or "",
                "cap": paths_truncation_cap if paths_truncation_cap is not None else 0,
                "coverage": coverage_payload,
            },
        )

    def load_coverage_payload(self, build_fingerprint: str) -> CoverageInventory:
        """Read the (b:Build) coverage payload back as a CoverageInventory.

        Returns an empty ``CoverageInventory`` when no payload was
        written (legacy builds, or builds where canonical_finalize ran
        with enrichment disabled).
        """

        rows = self._rows(
            "MATCH (build:GraphBuild {build_fingerprint: $bf}) "
            "RETURN build.coverage_payload AS payload LIMIT 1",
            {"bf": build_fingerprint},
        )
        if not rows:
            return CoverageInventory()
        raw = rows[0].get("payload") or ""
        if not raw:
            return CoverageInventory()
        import json as _json

        try:
            data = _json.loads(raw)
        except (ValueError, TypeError):
            return CoverageInventory()
        coverage = CoverageInventory()
        for category, items in data.items():
            for item in items:
                try:
                    state = CoverageState(item.get("state", "not_audited"))
                except ValueError:
                    state = CoverageState.NOT_AUDITED
                coverage.add(
                    CoverageRecord(
                        category=str(item.get("category", category)),
                        identifier=str(item.get("identifier", "")),
                        state=state,
                    )
                )
        return coverage


def _scoped_key(build_fingerprint: str, value: str) -> str:
    return f"{build_fingerprint}:{value}"


class InMemoryNeo4jGraphRepository:
    def __init__(self, adapter: InMemoryNeo4jAdapter) -> None:
        self.adapter = adapter

    def get_latest_build(self, repo_root: Path) -> str:
        builds = self.adapter.repo_builds.get(str(repo_root), [])
        if not builds:
            raise FileNotFoundError("No graph build is available in Neo4j yet. Run `xauditor graph build` first.")
        return builds[-1]

    def list_builds(self, repo_root: Path) -> list[GraphBuildSummary]:
        builds = self.adapter.repo_builds.get(str(repo_root), [])
        return [
            GraphBuildSummary(build_fingerprint=fp, created_at=index, status="ready")
            for index, fp in enumerate(reversed(builds))
        ]

    def require_build(self, build_fingerprint: str) -> None:
        if build_fingerprint not in self.adapter.builds_by_fingerprint:
            raise FileNotFoundError(f"Graph build `{build_fingerprint}` was not found in Neo4j.")

    def list_paths(self, build_fingerprint: str) -> list[PathSummary]:
        build = self._require_build(build_fingerprint)
        return [
            PathSummary(
                path_fingerprint=path.path_fingerprint,
                entry_function=path.entry_function,
                function_chain=tuple(path.function_names),
            )
            for path in build.paths
        ]

    def _require_build(self, build_fingerprint: str) -> _StoredBuild:
        build = self.adapter.builds_by_fingerprint.get(build_fingerprint)
        if build is None:
            raise FileNotFoundError(f"Graph build `{build_fingerprint}` was not found in Neo4j.")
        return build

    def iter_path_records(
        self,
        build_fingerprint: str,
        *,
        page_size: int = 100,
    ) -> Iterator[PathRecord]:
        del page_size
        yield from self._require_build(build_fingerprint).paths

    def count_paths(self, build_fingerprint: str) -> int:
        return len(self._require_build(build_fingerprint).paths)

    def load_functions_by_ids(
        self, build_fingerprint: str, function_ids: Sequence[str]
    ) -> list[FunctionRecord]:
        build = self._require_build(build_fingerprint)
        by_id = {item.function_id: item for item in build.functions}
        return [by_id[function_id] for function_id in function_ids if function_id in by_id]

    def load_symbols_for_functions(
        self, build_fingerprint: str, function_ids: Sequence[str]
    ) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
        build = self._require_build(build_fingerprint)
        function_id_set = set(function_ids)
        symbols_by_id = {symbol.symbol_id: symbol for symbol in build.module_symbols}
        uses: list[FunctionSymbolUseEdge] = []
        symbol_ids: list[str] = []
        seen_symbols: set[str] = set()
        for use in build.function_symbol_uses:
            if use.function_id not in function_id_set:
                continue
            if use.symbol_id not in symbols_by_id:
                continue
            uses.append(use)
            if use.symbol_id not in seen_symbols:
                seen_symbols.add(use.symbol_id)
                symbol_ids.append(use.symbol_id)
        symbols = tuple(symbols_by_id[symbol_id] for symbol_id in symbol_ids)
        return symbols, tuple(uses)

    def coverage_baseline(
        self, build_fingerprint: str
    ) -> tuple[
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
        dict[str, str],
        dict[str, str],
        dict[str, str],
        tuple[str, ...],
    ]:
        build = self._require_build(build_fingerprint)
        modules = tuple(item.name for item in build.modules)
        files = tuple(item.path for item in build.files)
        function_qualified_names = tuple(item.qualified_name for item in build.functions)
        function_to_module = {item.function_id: item.module_name for item in build.functions}
        function_to_file = {item.function_id: item.file_path for item in build.functions}
        function_to_name = {item.function_id: item.qualified_name for item in build.functions}
        paths = tuple(item.path_fingerprint for item in build.paths)
        return modules, files, function_qualified_names, function_to_module, function_to_file, function_to_name, paths

    def get_coverage(self, build_fingerprint: str) -> CoverageInventory | None:
        build = self._require_build(build_fingerprint)
        if build.coverage is None or not build.coverage.records:
            return None
        return build.coverage

    # --- Streaming chunk writers (parity with Neo4jGraphRepository) ---

    def begin_build(
        self,
        *,
        repo_root: Path,
        build_fingerprint: str,
    ) -> None:
        self.adapter._upsert_build(
            build_fingerprint=build_fingerprint, repo_root=repo_root
        )

    def write_file_chunk(
        self, build_fingerprint: str, records: Sequence[FileRecord]
    ) -> None:
        self.adapter._extend_build(build_fingerprint, "files", records)

    def write_class_chunk(
        self, build_fingerprint: str, records: Sequence[ClassRecord]
    ) -> None:
        self.adapter._extend_build(build_fingerprint, "classes", records)

    def write_class_member_chunk(
        self, build_fingerprint: str, records: Sequence[ClassMemberRecord]
    ) -> None:
        self.adapter._extend_build(build_fingerprint, "class_members", records)

    def write_function_chunk(
        self, build_fingerprint: str, records: Sequence[FunctionRecord]
    ) -> None:
        self.adapter._extend_build(build_fingerprint, "functions", records)

    def write_module_chunk(
        self, build_fingerprint: str, records: Sequence[ModuleRecord]
    ) -> None:
        self.adapter._extend_build(build_fingerprint, "modules", records)

    def write_module_symbol_chunk(
        self, build_fingerprint: str, records: Sequence[ModuleSymbolRecord]
    ) -> None:
        self.adapter._extend_build(build_fingerprint, "module_symbols", records)

    def write_symbol_use_chunk(
        self, build_fingerprint: str, records: Sequence[FunctionSymbolUseEdge]
    ) -> None:
        self.adapter._extend_build(build_fingerprint, "function_symbol_uses", records)

    def write_edge_chunk(
        self, build_fingerprint: str, records: Sequence[EdgeRecord]
    ) -> None:
        self.adapter._extend_build(build_fingerprint, "edges", records)
        self.adapter._extend_build(
            build_fingerprint,
            "provenance",
            tuple(item.provenance for item in records),
        )

    def write_path_chunk(
        self, build_fingerprint: str, records: Sequence[PathRecord]
    ) -> None:
        self.adapter._extend_build(build_fingerprint, "paths", records)

    def finalize_build(
        self,
        *,
        build_fingerprint: str,
        paths_truncated: bool = False,
        paths_truncation_reason: str | None = None,
        paths_truncation_cap: int | None = None,
        coverage: CoverageInventory | None = None,
    ) -> None:
        self.adapter._finalize_build(
            build_fingerprint=build_fingerprint,
            paths_truncated=paths_truncated,
            paths_truncation_reason=paths_truncation_reason,
            paths_truncation_cap=paths_truncation_cap,
            coverage=coverage,
        )

    def load_coverage_payload(self, build_fingerprint: str) -> CoverageInventory:
        build = self._require_build(build_fingerprint)
        return build.coverage or CoverageInventory()
