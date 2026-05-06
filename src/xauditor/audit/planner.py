from __future__ import annotations

from xauditor.audit.source import AuditGraphSource
from xauditor.audit.units import AuditUnit as ProtocolAuditUnit
from xauditor.audit.units import (
    BoundaryAuditUnit,
    EntryAuditUnit,
    SinkAuditUnit,
    StateAuditUnit,
    as_audit_unit,
)
from xauditor.config import AuditModeConfig
from xauditor.llm import LLMClient
from xauditor.models import AuditPlan, AuditUnit, FunctionRecord, PathRecord


_ENTRY_INTENTS: frozenset[str] = frozenset({"route", "task_handler", "cli_entry"})

_BOUNDARY_SINK_KINDS: frozenset[str] = frozenset({"http_client", "subprocess"})
_SINK_KIND_TO_BOUNDARY_KIND: dict[str, str] = {
    "http_client": "rpc",
    "subprocess": "subprocess",
}

_STATE_MIN_MUTATING_METHODS: int = 2


def enumerate_units(
    source: AuditGraphSource,
    audit_mode: AuditModeConfig | None = None,
    *,
    llm_client: LLMClient | None = None,
) -> list[ProtocolAuditUnit]:
    """Return the list of audit units the workflow will iterate.

    Always emits one `PathAuditUnit` per planned path (today's
    behaviour). When `audit.experimental.units` is on AND the
    graph carries `is_well_known_sink: true` Function nodes
    (shipped in `capture-decorators-and-registrations` Commit 2),
    additionally emits one `SinkAuditUnit` per matched sink with
    its inbound paths attached. Operators flip the flag in
    `xauditor.yml` or via `XAUDITOR_AUDIT_EXPERIMENTAL_UNITS` to
    opt into the broader unit-kind taxonomy.

    Future kinds (`entry` / `state` / `boundary` / `config`)
    follow the same gating pattern in subsequent changes.
    """

    plan = plan_audit_paths(source, llm_client)
    units: list[ProtocolAuditUnit] = [as_audit_unit(unit) for unit in plan.audit_units]

    units_flag_on = bool(
        audit_mode is not None
        and getattr(audit_mode, "experimental", None) is not None
        and getattr(audit_mode.experimental, "units", False)
    )
    if units_flag_on:
        units.extend(_enumerate_sink_units(source, plan))
        units.extend(_enumerate_entry_units(source))
        units.extend(_enumerate_boundary_units(source, plan))
        units.extend(_enumerate_state_units(source))

    return units


def _enumerate_state_units(source: AuditGraphSource) -> list[ProtocolAuditUnit]:
    """Walk the function catalog for shared-state classes.

    `capture-decorators-and-registrations` Commit F. A class is
    "stateful" when it has `>= 2` methods whose bodies assign to
    `self.<attr>`. Such classes are interesting audit anchors
    because a single instance shared across requests / threads /
    coroutines is a concurrency / correctness risk: one method
    sets a field, another reads it, and the analyzer should
    inspect the invariants holding between calls.

    Method-body inspection happens at parse time
    (`PythonParser._detect_self_mutation`). We just group
    Function records by `class_id` and filter to classes meeting
    the threshold. Stub external Function records
    (`is_external=True`) and module-level functions
    (`class_id is None`) are skipped.
    """

    methods_by_class: dict[str, list[FunctionRecord]] = {}
    for fn in source.iter_functions():
        if getattr(fn, "is_external", False):
            continue
        class_id = getattr(fn, "class_id", None)
        if not class_id:
            continue
        if not getattr(fn, "mutates_self", False):
            continue
        methods_by_class.setdefault(class_id, []).append(fn)

    units: list[ProtocolAuditUnit] = []
    for class_id, mutators in methods_by_class.items():
        if len(mutators) < _STATE_MIN_MUTATING_METHODS:
            continue
        class_record = source.lookup_class_by_id(class_id)
        if class_record is None:
            class_qualified_name = mutators[0].qualified_name.rsplit(".", 1)[0]
        else:
            class_qualified_name = (
                f"{class_record.module_name}.{class_record.name}"
                if class_record.module_name
                else class_record.name
            )
        method_ids = tuple(sorted(fn.function_id for fn in mutators))
        units.append(
            StateAuditUnit(
                class_id=class_id,
                class_qualified_name=class_qualified_name,
                method_ids=method_ids,
            )
        )
    return units


def _enumerate_boundary_units(
    source: AuditGraphSource, plan: AuditPlan
) -> list[ProtocolAuditUnit]:
    """Walk planned paths for cross-process / network-call boundaries.

    `capture-decorators-and-registrations` Commit E. A boundary is
    a CALL into a Function whose record has
    `sink_kind ∈ {http_client, subprocess}` — i.e. data crossing
    out of the in-process trust zone (HTTP request, subprocess
    invocation). Each such call deserves a distinct audit lens
    from in-process taint flow: input encoding, resource bounds,
    serialization invariants.

    Producer = the function in the path immediately *preceding*
    the sink in `path.function_ids` (the in-process caller that
    wires data into the boundary). Consumer = the sink itself
    (represented today by the `external::*` stub Function record
    from sink-labelling). When the sink is the path's first
    function, producer falls back to the path entry.

    Reachability: spec 2.2.5 calls for "reachable from a
    registered entry". Today every planned path is entry-rooted
    by construction, so iterating planned paths gives the same
    coverage in practice; the strict registered-entry filter is
    deferred to a follow-up.

    De-duplication: a boundary is keyed by
    `(producer_function_id, consumer_function_id)` — two paths
    that share a producer→sink call collapse to one unit.
    """

    boundary_sinks = {
        fn.function_id: fn
        for fn in source.iter_functions()
        if getattr(fn, "is_well_known_sink", False)
        and getattr(fn, "sink_kind", "") in _BOUNDARY_SINK_KINDS
    }
    if not boundary_sinks:
        return []

    units: list[ProtocolAuditUnit] = []
    seen_pairs: set[tuple[str, str]] = set()
    for plan_unit in plan.audit_units:
        function_ids = tuple(plan_unit.function_ids)
        for index, fn_id in enumerate(function_ids):
            sink_fn = boundary_sinks.get(fn_id)
            if sink_fn is None:
                continue
            producer_id = function_ids[index - 1] if index > 0 else function_ids[0]
            pair = (producer_id, fn_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            sink_kind = getattr(sink_fn, "sink_kind", "") or ""
            boundary_kind = _SINK_KIND_TO_BOUNDARY_KIND.get(sink_kind, sink_kind)
            units.append(
                BoundaryAuditUnit(
                    boundary_kind=boundary_kind,
                    producer_function_id=producer_id,
                    consumer_function_ids=(fn_id,),
                )
            )
    return units


def _enumerate_entry_units(source: AuditGraphSource) -> list[ProtocolAuditUnit]:
    """Walk the function catalog for decorator / registration entries.

    `capture-decorators-and-registrations` Commit D. A function is
    an "entry" when:

    - it has a decorator with `intent ∈ {route, task_handler,
      cli_entry}` (e.g. `@app.route(...)`, `@celery.task`,
      `@click.command`), OR
    - it has an inbound REGISTERS edge (e.g. registered via
      `app.add_url_rule(url, view)` or
      `urlpatterns = [path(url, view)]`).

    Returns one `EntryAuditUnit` per matched entry function.
    Skips stub Function records (`is_external=True`) — sinks
    aren't entries.
    """

    functions = [fn for fn in source.iter_functions() if not getattr(fn, "is_external", False)]
    if not functions:
        return []

    function_ids = [fn.function_id for fn in functions]
    decorators_by_fn = source.fetch_decorators_for(function_ids)
    registrations_by_fn = source.fetch_registrations_for(function_ids)
    if not decorators_by_fn and not registrations_by_fn:
        return []

    units: list[ProtocolAuditUnit] = []
    for fn in functions:
        decorators = decorators_by_fn.get(fn.function_id, [])
        registrations = registrations_by_fn.get(fn.function_id, [])
        is_entry = (
            any(getattr(deco, "intent", "") in _ENTRY_INTENTS for deco in decorators)
            or bool(registrations)
        )
        if not is_entry:
            continue
        units.append(
            EntryAuditUnit(
                entry_function_id=fn.function_id,
                entry_qualified_name=fn.qualified_name,
            )
        )
    return units


def _enumerate_sink_units(
    source: AuditGraphSource, plan: AuditPlan
) -> list[ProtocolAuditUnit]:
    """Walk the source's Function catalog for sink-labelled nodes;
    build one `SinkAuditUnit` per matched sink with the subset of
    planned paths that reach it.
    """

    sink_functions = [
        fn
        for fn in source.iter_functions()
        if getattr(fn, "is_well_known_sink", False)
    ]
    if not sink_functions:
        return []

    units: list[ProtocolAuditUnit] = []
    for sink_fn in sink_functions:
        inbound_paths = tuple(
            unit.path
            for unit in plan.audit_units
            if sink_fn.function_id in unit.function_ids
        )
        if not inbound_paths:
            # No planned path reaches this sink; skip — nothing
            # for the analyzer to audit on this unit.
            continue
        units.append(
            SinkAuditUnit(
                sink_qualified_name=sink_fn.qualified_name,
                inbound_paths=inbound_paths,
            )
        )
    return units


def plan_audit_paths(
    source: AuditGraphSource,
    llm_client: LLMClient | None = None,
) -> AuditPlan:
    audit_units: list[AuditUnit] = []
    seen_fingerprints: set[str] = set()
    for path in source.iter_paths():
        if path.path_fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(path.path_fingerprint)
        function_ids = path.function_ids
        business_context = path.business_context
        trust_boundary = path.trust_boundary
        if llm_client is not None and (not business_context or not trust_boundary):
            enrichment = llm_client.summarize_path(path.entry_function, path.function_names)
            business_context = business_context or enrichment["business_context"]
            trust_boundary = trust_boundary or enrichment["trust_boundary"]
        audit_units.append(
            AuditUnit(
                path=PathRecord(
                    entry_function=path.entry_function,
                    function_names=path.function_names,
                    file_paths=path.file_paths,
                    path_fingerprint=path.path_fingerprint,
                    function_ids=function_ids,
                    business_context=business_context,
                    trust_boundary=trust_boundary,
                ),
                function_ids=function_ids,
            )
        )
    return AuditPlan(audit_units=tuple(audit_units))
