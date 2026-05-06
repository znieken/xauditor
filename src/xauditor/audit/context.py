"""Per-audit-unit context builder.

`restructure-audit-modes-and-coverage` Phase 2A renamed
`build_path_context` to `build_graph_slice` and broadened the
output from a `dict[str, object]` to a structured `GraphSlice`
dataclass with five new fields beyond the historical
`(call_chain, function_definitions, referenced_symbols)` triple:

- `decorator_chain` — every decorator on functions in the call
  chain (currently empty; needs a graph-builder enhancement that
  captures `DECORATES` edges)
- `registration_context` — framework-registration sites
  (currently empty; needs a graph-builder enhancement that
  captures `REGISTERS` edges)
- `entry_classification` — `EntryKind` derived heuristically from
  the entry function's name + module path (best-effort without
  decorator/registration data)
- `type_context` — type annotations / value reprs for the
  referenced module symbols (read from existing
  `ModuleSymbolRecord` fields)
- `cross_path_definitions` — for symbols read on the path but
  defined in functions outside it, the definition sites
  (read from existing `USES_SYMBOL` + `DECLARES_SYMBOL` data)

`build_path_context` is kept as a backward-compatible alias
returning the dict shape — internal call sites are migrated
to `build_graph_slice`, but downstream tooling / tests that
import the legacy name keep working.

The new fields are gated behind
`audit.experimental.graph_slice` (added by Phase 2 to
`AuditModeConfig`). When the flag is off (default for the
first release), the new fields are emitted as empty tuples
so the analyzer / validator payloads stay shape-stable but
do not change semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal, Mapping, Sequence

from xauditor.models import (
    DecoratorRecord,
    FunctionRecord,
    FunctionSymbolUseEdge,
    ModuleSymbolRecord,
    RegistrationSiteRecord,
)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


EntryKind = Literal[
    "public_http",
    "admin_http",
    "internal_rpc",
    "cron",
    "cli",
    "test_only",
    "unknown",
]


@dataclass(frozen=True)
class DecoratorRef:
    """A single decorator applied to a function in the call chain.

    Phase 2A ships the dataclass; population requires the
    graph-builder to capture decorators (deferred to
    `capture-decorators-and-registrations` follow-up).
    """

    function_id: str
    expression: str
    """Source-text rendering of the decorator (`@router.post('/users',
    dependencies=[require_admin])`). Used by the analyzer prompt as
    free-form context, not parsed downstream."""

    framework: str = ""
    """Best-effort framework label (`fastapi.router`,
    `flask.app`, `celery.task`, `django.url`, etc.). Empty when
    the decorator is unrecognised."""

    intent: str = ""
    """Best-effort semantic label (`route`, `auth_required`,
    `csrf_exempt`, `task_handler`). Empty when unrecognised."""


@dataclass(frozen=True)
class RegistrationRef:
    """A registration site that binds a function into a framework's
    request / task / event pipeline.

    Phase 2A ships the dataclass; population requires the
    graph-builder to capture registration sites (deferred to
    the same follow-up as `decorator_chain`).
    """

    function_id: str
    site_file: str
    site_line: int
    framework: str = ""
    expression: str = ""


@dataclass(frozen=True)
class TypeRef:
    """A type annotation observed for a referenced symbol or function
    parameter.

    Populated from the existing `ModuleSymbolRecord.type_annotation`
    and `value_repr` fields, plus heuristic ORM-column / pydantic
    detection on the rendered annotation string. No graph-builder
    change required.
    """

    symbol_id: str
    name: str
    type_annotation: str
    value_repr: str = ""
    is_orm_column: bool = False
    """True when the annotation looks like an ORM column type
    (`Mapped[str]`, `Column(String)`, SQLAlchemy `__table__.c.*`,
    Django `models.CharField`, Tortoise / Peewee field types).
    Heuristic-only; false negatives are expected."""


@dataclass(frozen=True)
class DefSite:
    """Definition site of a symbol read on this audit unit's path
    but defined in a function NOT on the path.

    Helps the analyzer reason about whether a "tainted" symbol was
    sanitized by sibling code (the classic
    "sanitization happened on a different path" false-negative
    class). Populated from existing `USES_SYMBOL` /
    `DECLARES_SYMBOL` data.
    """

    symbol_id: str
    name: str
    defining_file: str
    defining_line: int
    defining_function_id: str
    """The function that declares the symbol — outside the audit
    unit's path. Empty string for module-level / top-level
    definitions."""


# ---------------------------------------------------------------------------
# GraphSlice — replaces the historical `dict[str, object]` path-context
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphSlice:
    """Per-audit-unit context handed to every stage runner.

    Three legacy fields preserve today's analyzer / validator /
    exploiter payload shape:

    - `call_chain` — function refs in topological order
    - `function_definitions` — full source dicts for each function
    - `referenced_symbols` — module-level symbols referenced by the
      call chain (with their per-symbol `used_by` lists)

    Five new fields broaden the slice with surrounding context.
    Phase 2A populates two of them
    (`type_context`, `cross_path_definitions`) plus the
    heuristic `entry_classification`. The other two
    (`decorator_chain`, `registration_context`) are empty
    until a graph-builder change captures `DECORATES` and
    `REGISTERS` edges.

    The dataclass is intentionally JSON-friendly:
    `to_payload_dict` projects it to the dict shape every
    existing stage payload consumer (`analyzer_agent`,
    `validator_agent`, `exploitation_agent`,
    `_dispatch_coder`) already expects, so call sites keep
    working without migration.
    """

    # Legacy three (unchanged).
    call_chain: tuple[dict[str, object], ...]
    function_definitions: tuple[dict[str, object], ...]
    referenced_symbols: tuple[dict[str, object], ...]

    # Phase 2A new fields.
    decorator_chain: tuple[DecoratorRef, ...] = ()
    registration_context: tuple[RegistrationRef, ...] = ()
    entry_classification: EntryKind = "unknown"
    type_context: tuple[TypeRef, ...] = ()
    cross_path_definitions: tuple[DefSite, ...] = ()

    def to_payload_dict(self) -> dict[str, object]:
        """Project to the dict shape every existing consumer expects.

        Preserves backwards compatibility with the historical
        `path_context.get("call_chain", ())` style access in
        `audit/agents.py`. The new fields are surfaced under
        `audit_v2_*` keys so callers can opt in without colliding
        with the legacy three.
        """

        payload: dict[str, object] = {
            "call_chain": list(self.call_chain),
            "function_definitions": list(self.function_definitions),
            "referenced_symbols": list(self.referenced_symbols),
        }
        # New fields go behind a `graph_slice` namespace so existing
        # stage prompts that lookup `payload["call_chain"]` etc. keep
        # working unchanged. Stage prompts that want to consume the
        # new fields read from `payload["graph_slice"]`.
        payload["graph_slice"] = {
            "decorator_chain": [
                {
                    "function_id": d.function_id,
                    "expression": d.expression,
                    "framework": d.framework,
                    "intent": d.intent,
                }
                for d in self.decorator_chain
            ],
            "registration_context": [
                {
                    "function_id": r.function_id,
                    "site_file": r.site_file,
                    "site_line": r.site_line,
                    "framework": r.framework,
                    "expression": r.expression,
                }
                for r in self.registration_context
            ],
            "entry_classification": self.entry_classification,
            "type_context": [
                {
                    "symbol_id": t.symbol_id,
                    "name": t.name,
                    "type_annotation": t.type_annotation,
                    "value_repr": t.value_repr,
                    "is_orm_column": t.is_orm_column,
                }
                for t in self.type_context
            ],
            "cross_path_definitions": [
                {
                    "symbol_id": d.symbol_id,
                    "name": d.name,
                    "defining_file": d.defining_file,
                    "defining_line": d.defining_line,
                    "defining_function_id": d.defining_function_id,
                }
                for d in self.cross_path_definitions
            ],
        }
        return payload


# ---------------------------------------------------------------------------
# Heuristic helpers (no graph-builder dependency)
# ---------------------------------------------------------------------------


# Conservative name-pattern table for `entry_classification`. Heuristics
# fire on the entry function's qualified name + its file path. The
# `unknown` default kicks in when no pattern matches; downstream code
# treats `unknown` exactly the same as "we have no signal here".
_ENTRY_NAME_PATTERNS: tuple[tuple[str, EntryKind], ...] = (
    # Test-only paths come first because `tests/` modules can also
    # contain `cron_*` / `_admin_*` helpers that would otherwise
    # match a more interesting kind.
    ("/tests/", "test_only"),
    ("/test_", "test_only"),
    ("_test.py", "test_only"),
    # Admin-flavoured handlers.
    ("/admin/", "admin_http"),
    ("admin_", "admin_http"),
    # Cron / scheduled jobs.
    ("cron_", "cron"),
    ("scheduled_", "cron"),
    ("/jobs/", "cron"),
    # CLI entrypoints.
    ("/cli/", "cli"),
    ("/cli.py", "cli"),
    ("__main__", "cli"),
    # Internal RPC / queue / event handlers.
    ("/rpc/", "internal_rpc"),
    ("_handler", "internal_rpc"),
    ("on_message", "internal_rpc"),
    ("consume_", "internal_rpc"),
    # HTTP-shaped handlers (last so the more-specific kinds above win).
    ("/api/", "public_http"),
    ("/routes/", "public_http"),
    ("/views/", "public_http"),
    ("handle_", "public_http"),
    ("get_", "public_http"),
    ("post_", "public_http"),
)


def _classify_entry(
    entry_function_id: str,
    function_definitions: Sequence[dict[str, object]],
    *,
    decorator_chain: Sequence["DecoratorRef"] = (),
) -> EntryKind:
    """Best-effort entry classification.

    `capture-decorators-and-registrations` Phase 2.1 — when the
    `decorator_chain` argument is non-empty, classification
    becomes deterministic: a decorator with `intent="route"` AND
    a path containing `/admin/` → `admin_http`; a decorator with
    `intent="route"` (no admin marker) → `public_http`; a
    decorator with `intent="task_handler"` → `internal_rpc`; a
    decorator with `intent="cli_entry"` → `cli`. Otherwise falls
    back to today's name-pattern heuristic.

    Returns `"unknown"` when no decorator OR name-pattern matches.
    """

    # Deterministic check via decorator chain (when present).
    for deco in decorator_chain:
        if getattr(deco, "function_id", "") and deco.function_id != entry_function_id:
            # Only consider decorators on the entry function itself
            # for classification.
            continue
        intent = getattr(deco, "intent", "")
        if intent == "route":
            expr = (getattr(deco, "expression", "") or "").lower()
            if "/admin/" in expr or "admin_required" in expr:
                return "admin_http"
            return "public_http"
        if intent == "task_handler":
            return "internal_rpc"
        if intent == "cli_entry":
            return "cli"

    # Fallback: name-pattern heuristic on the entry's file_path +
    # qualified_name (preserves today's behaviour for routes that
    # don't carry framework decorators we recognise).
    if not function_definitions:
        return "unknown"
    entry: dict[str, object] | None = next(
        (
            f
            for f in function_definitions
            if f.get("function_id") == entry_function_id
        ),
        None,
    )
    if entry is None:
        entry = function_definitions[0]
    file_path = str(entry.get("file_path", "")).lower()
    qualified = str(entry.get("qualified_name", "")).lower()
    haystack = f"{file_path}::{qualified}"
    for pattern, kind in _ENTRY_NAME_PATTERNS:
        if pattern in haystack:
            return kind
    return "unknown"


_ORM_TYPE_HINTS: tuple[str, ...] = (
    "Mapped[",
    "Column(",
    "BigInteger",
    "Integer(",
    "String(",
    "Text(",
    "JSONB",
    "models.",
    "sqlmodel.",
    "fields.",
    "tortoise.",
    "peewee.",
)


def _looks_like_orm_column(annotation: str, value_repr: str) -> bool:
    haystack = f"{annotation}\n{value_repr}"
    return any(hint in haystack for hint in _ORM_TYPE_HINTS)


# ---------------------------------------------------------------------------
# Slice builder
# ---------------------------------------------------------------------------


def build_graph_slice(
    *,
    repo_root: Path,
    path_functions: list[FunctionRecord],
    module_symbols: Sequence[ModuleSymbolRecord] = (),
    function_symbol_uses: Sequence[FunctionSymbolUseEdge] = (),
    enable_v2_fields: bool = False,
    decorators_by_function_id: "Mapping[str, Sequence[DecoratorRecord]] | None" = None,
    registrations_by_function_id: "Mapping[str, Sequence[RegistrationSiteRecord]] | None" = None,
) -> GraphSlice:
    """Build a `GraphSlice` for one audit unit.

    The legacy three fields (`call_chain`, `function_definitions`,
    `referenced_symbols`) are populated unconditionally. The five
    new fields are populated only when `enable_v2_fields=True`
    (resolved from `audit.experimental.graph_slice`). When the
    flag is off, the slice's new fields are empty tuples — the
    payload stays shape-stable and downstream prompt rendering
    can skip the `graph_slice` block via standard "non-empty"
    checks.

    Three new fields are populated from existing graph data:
    `cross_path_definitions` (USES_SYMBOL + DECLARES_SYMBOL),
    `type_context` (ModuleSymbolRecord.type_annotation /
    value_repr), and `entry_classification` (heuristic from
    function name + file path).

    Two new fields are reserved for a future graph-builder
    enhancement: `decorator_chain` and `registration_context`
    are emitted as empty tuples until `DECORATES` / `REGISTERS`
    edges are written by the graph builder.
    """

    source_by_function_id = {
        function.function_id: _function_source(repo_root, function)
        for function in path_functions
    }

    path_function_ids = {function.function_id for function in path_functions}
    symbols_by_id = {symbol.symbol_id: symbol for symbol in module_symbols}
    referenced_symbol_ids: list[str] = []
    seen_symbols: set[str] = set()
    uses_by_symbol: dict[str, list[dict[str, object]]] = {}
    cross_path_uses: list[FunctionSymbolUseEdge] = []
    for use in function_symbol_uses:
        if use.symbol_id not in symbols_by_id:
            continue
        if use.function_id not in path_function_ids:
            # Symbol read by a function OUTSIDE the path — candidate
            # for `cross_path_definitions`. Tracked separately and
            # reconciled below.
            cross_path_uses.append(use)
            continue
        if use.symbol_id not in seen_symbols:
            seen_symbols.add(use.symbol_id)
            referenced_symbol_ids.append(use.symbol_id)
        uses_by_symbol.setdefault(use.symbol_id, []).append(
            {
                "function_id": use.function_id,
                "line_number": use.line_number,
                "evidence": use.evidence,
            }
        )

    referenced_symbols = tuple(
        {
            "symbol_id": symbols_by_id[sid].symbol_id,
            "name": symbols_by_id[sid].name,
            "kind": symbols_by_id[sid].kind,
            "module_name": symbols_by_id[sid].module_name,
            "file_path": symbols_by_id[sid].file_path,
            "start_line": symbols_by_id[sid].start_line,
            "end_line": symbols_by_id[sid].end_line,
            "type_annotation": symbols_by_id[sid].type_annotation,
            "value_repr": symbols_by_id[sid].value_repr,
            "is_placeholder": symbols_by_id[sid].is_placeholder,
            "used_by": uses_by_symbol[sid],
        }
        for sid in referenced_symbol_ids
    )

    call_chain = tuple(
        {
            "function_id": function.function_id,
            "qualified_name": function.qualified_name,
            "file_path": function.file_path,
            "start_line": function.start_line,
            "end_line": function.end_line,
        }
        for function in path_functions
    )

    function_definitions = tuple(
        {
            "function_id": function.function_id,
            "qualified_name": function.qualified_name,
            "file_path": function.file_path,
            "start_line": function.start_line,
            "end_line": function.end_line,
            "source": source_by_function_id[function.function_id],
        }
        for function in path_functions
    )

    if not enable_v2_fields:
        return GraphSlice(
            call_chain=call_chain,
            function_definitions=function_definitions,
            referenced_symbols=referenced_symbols,
        )

    # ----- v2 fields below; only populated when flag is on -----

    # `type_context` — every referenced symbol that has a non-empty
    # type annotation OR value_repr surfaces as a TypeRef. Conservative
    # filter so the analyzer payload doesn't bloat with no-info entries.
    type_context = tuple(
        TypeRef(
            symbol_id=symbols_by_id[sid].symbol_id,
            name=symbols_by_id[sid].name,
            type_annotation=symbols_by_id[sid].type_annotation,
            value_repr=symbols_by_id[sid].value_repr,
            is_orm_column=_looks_like_orm_column(
                symbols_by_id[sid].type_annotation,
                symbols_by_id[sid].value_repr,
            ),
        )
        for sid in referenced_symbol_ids
        if symbols_by_id[sid].type_annotation or symbols_by_id[sid].value_repr
    )

    # `cross_path_definitions` — symbols referenced by the path
    # whose defining function is outside the path. We re-walk
    # `cross_path_uses` to surface ONLY symbols also referenced
    # by an in-path function (otherwise the slice has no use for
    # them).
    in_path_symbol_ids = set(referenced_symbol_ids)
    cross_path_definitions: list[DefSite] = []
    seen_def_keys: set[tuple[str, str]] = set()
    for use in cross_path_uses:
        if use.symbol_id not in in_path_symbol_ids:
            continue
        symbol = symbols_by_id[use.symbol_id]
        key = (use.symbol_id, use.function_id)
        if key in seen_def_keys:
            continue
        seen_def_keys.add(key)
        cross_path_definitions.append(
            DefSite(
                symbol_id=use.symbol_id,
                name=symbol.name,
                defining_file=symbol.file_path,
                defining_line=use.line_number,
                defining_function_id=use.function_id,
            )
        )

    # `decorator_chain` — `capture-decorators-and-registrations`
    # Phase 2.1. Populated from the supplied
    # `decorators_by_function_id` map (filled by the workflow via
    # `source.fetch_decorators_for(...)`). Empty when the map is
    # absent OR the v2 flag is off (handled by the early return
    # above).
    decorator_chain_list: list[DecoratorRef] = []
    if decorators_by_function_id:
        for fn in path_functions:
            for deco in decorators_by_function_id.get(fn.function_id, ()):
                decorator_chain_list.append(
                    DecoratorRef(
                        function_id=fn.function_id,
                        expression=deco.expression,
                        framework=deco.framework,
                        intent=deco.intent,
                    )
                )
    decorator_chain = tuple(decorator_chain_list)

    # `entry_classification` — deterministic via decorator chain
    # when populated; otherwise heuristic from the entry's name +
    # path. The first function in `path_functions` is the entry by
    # convention (the planner emits paths in entry → ... → leaf
    # order).
    entry_function_id = (
        path_functions[0].function_id if path_functions else ""
    )
    entry_classification = _classify_entry(
        entry_function_id,
        list(function_definitions),
        decorator_chain=decorator_chain,
    )

    # `registration_context` — `capture-decorators-and-registrations`
    # Commit C. Populated from the supplied
    # `registrations_by_function_id` map (filled by the workflow
    # via `source.fetch_registrations_for(...)`).
    registration_list: list[RegistrationRef] = []
    if registrations_by_function_id:
        for fn in path_functions:
            for site in registrations_by_function_id.get(fn.function_id, ()):
                registration_list.append(
                    RegistrationRef(
                        function_id=fn.function_id,
                        site_file=site.file_path,
                        site_line=site.line_number,
                        framework=site.framework,
                        expression=site.expression,
                    )
                )
    registration_context = tuple(registration_list)

    return GraphSlice(
        call_chain=call_chain,
        function_definitions=function_definitions,
        referenced_symbols=referenced_symbols,
        decorator_chain=decorator_chain,
        registration_context=registration_context,
        entry_classification=entry_classification,
        type_context=type_context,
        cross_path_definitions=tuple(cross_path_definitions),
    )


def build_path_context(
    *,
    repo_root: Path,
    path_functions: list[FunctionRecord],
    module_symbols: Sequence[ModuleSymbolRecord] = (),
    function_symbol_uses: Sequence[FunctionSymbolUseEdge] = (),
) -> dict[str, object]:
    """Backward-compat alias returning the legacy dict shape.

    Internal call sites have been migrated to `build_graph_slice`
    (Phase 2A); this alias stays for one minor release for
    out-of-tree consumers (downstream test suites that import
    `xauditor.audit.context.build_path_context` directly). It
    builds a `GraphSlice` with the v2 flag OFF and projects to
    the historical dict shape WITHOUT the new `graph_slice`
    sub-key.
    """

    slice_obj = build_graph_slice(
        repo_root=repo_root,
        path_functions=path_functions,
        module_symbols=module_symbols,
        function_symbol_uses=function_symbol_uses,
        enable_v2_fields=False,
    )
    return {
        "call_chain": list(slice_obj.call_chain),
        "function_definitions": list(slice_obj.function_definitions),
        "referenced_symbols": list(slice_obj.referenced_symbols),
    }


def _function_source(repo_root: Path, function: FunctionRecord) -> str:
    if function.source:
        return function.source
    text = _read_text(repo_root / function.file_path)
    if not text:
        return ""
    lines = text.splitlines()
    start = max(function.start_line - 1, 0)
    end = max(function.end_line, start)
    return "\n".join(lines[start:end])


@lru_cache(maxsize=256)
def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return ""
