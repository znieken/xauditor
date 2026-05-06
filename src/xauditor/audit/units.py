"""AuditUnit Protocol and concrete implementations.

`restructure-audit-modes-and-coverage` Phase 3A introduces the
`AuditUnit` Protocol seam. Phase 3A ships only the Protocol +
`PathAuditUnit` (a thin wrapper over the historical
`models.AuditUnit` dataclass) + dataclass shells for the five
other unit kinds the spec foresees:

| Unit | Phase 3A | Status |
|---|---|---|
| `PathAuditUnit` | ✅ shipped | Wraps `models.AuditUnit`. Workflow uses it interchangeably with the legacy dataclass. |
| `SinkAuditUnit` | ⚠️ shell only | Dataclass exists; planner emits none until graph builder learns sink labels. |
| `EntryAuditUnit` | ⚠️ shell only | Dataclass exists; planner emits none until graph builder captures decorator / registration data. |
| `StateAuditUnit` | ⚠️ shell only | Phase 5 deep-only. |
| `BoundaryAuditUnit` | ⚠️ shell only | Phase 5 deep-only. |
| `ConfigAuditUnit` | ⚠️ shell only | Phase 5 deep-only. |

The Protocol gives Phase 4/5 a stable contract to consume:
agentic stage runners (Phase 4) and the cross-unit reconciler
(Phase 5) reference `AuditUnit` rather than a specific
implementation, so adding new unit kinds doesn't churn the
workflow code.

Workflow integration in Phase 3A is **opt-in**: the existing
`for unit in plan.audit_units` loop passes the legacy
`models.AuditUnit` dataclass through unchanged. Stage-runner
code that wants the Protocol surface uses
`as_audit_unit(legacy_unit)` to get a `PathAuditUnit` view.
This avoids a workflow-wide rewrite while still landing the
seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from xauditor.audit.context import GraphSlice
from xauditor.models import AuditUnit as LegacyAuditUnit
from xauditor.models import PathRecord


UnitKind = Literal[
    "path",
    "sink",
    "entry",
    "state",
    "boundary",
    "config",
]


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AuditUnit(Protocol):
    """Per-audit-unit contract consumed by stage runners + reconciler.

    The four dict-shaped `to_*_payload` methods project the unit
    onto the dict shape every existing stage agent
    (`AnalyzerAgent`, `ValidatorAgent`, `ExploitationAgent`)
    consumes. Each implementation is responsible for
    unit-kind-specific payload framing — Path units include
    `call_chain` / `function_definitions`, Sink units include
    the per-path sanitization summary, Entry units include the
    full exposed surface, etc.

    `fingerprint()` returns a stable string used by:
    - cross-unit reconciliation (Phase 5) to group findings
      that surfaced from multiple unit kinds
    - resume / dedup logic to identify "this is the same audit
      unit" across runs
    - the workflow's per-unit shared-state key
    """

    unit_id: str
    """Stable identifier for this unit within an audit run.

    Path units use the path fingerprint; Sink units use the
    sink's qualified name; Entry units use the entry function
    id; State / Boundary / Config units use their anchor's
    qualified name + kind suffix.
    """

    unit_kind: UnitKind
    """One of the six closed values. Stage runners use this for
    telemetry and prompt-family selection only — they SHALL NOT
    branch on `unit_kind` for behavior."""

    graph_slice: GraphSlice
    """The per-unit `GraphSlice` (Phase 2A) carrying the call
    chain, referenced symbols, and the v2 fields when the
    `audit.experimental.graph_slice` flag is on."""

    def to_analyzer_payload(self) -> dict[str, object]:
        ...

    def to_validator_payload(self) -> dict[str, object]:
        ...

    def to_exploiter_payload(self) -> dict[str, object]:
        ...

    def fingerprint(self) -> str:
        ...


# ---------------------------------------------------------------------------
# PathAuditUnit — shipped, populated, the only kind the planner emits today
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PathAuditUnit:
    """Thin wrapper over the historical `models.AuditUnit` dataclass.

    Carries the underlying `legacy` reference so the workflow's
    existing code (which reads `unit.path.path_fingerprint`,
    `unit.function_ids`, etc.) keeps working when a
    `PathAuditUnit` is passed through. New code consuming the
    Protocol surface uses the `unit_id` / `unit_kind` /
    `graph_slice` / `to_*_payload` methods.
    """

    legacy: LegacyAuditUnit
    graph_slice: GraphSlice = field(
        default_factory=lambda: GraphSlice(
            call_chain=(), function_definitions=(), referenced_symbols=()
        )
    )
    unit_kind: UnitKind = "path"

    @property
    def unit_id(self) -> str:
        return self.legacy.path.path_fingerprint

    @property
    def path(self) -> PathRecord:
        """Backward-compat passthrough so existing
        `unit.path.<...>` accesses keep working."""
        return self.legacy.path

    @property
    def function_ids(self) -> tuple[str, ...]:
        return self.legacy.function_ids

    def fingerprint(self) -> str:
        return self.legacy.path.path_fingerprint

    def to_analyzer_payload(self) -> dict[str, object]:
        payload = self.graph_slice.to_payload_dict()
        payload["entry_function"] = self.legacy.path.entry_function
        payload["function_names"] = list(self.legacy.path.function_names)
        return payload

    def to_validator_payload(self) -> dict[str, object]:
        # Validator runs BEFORE exploiter (Phase 1B reorder), so
        # validator's payload mirrors the analyzer's surface — no
        # exploitation context is included here.
        payload = self.graph_slice.to_payload_dict()
        payload["entry_function"] = self.legacy.path.entry_function
        return payload

    def to_exploiter_payload(self) -> dict[str, object]:
        payload = self.graph_slice.to_payload_dict()
        payload["entry_function"] = self.legacy.path.entry_function
        return payload


# ---------------------------------------------------------------------------
# Sink / Entry / State / Boundary / Config — dataclass shells only
# ---------------------------------------------------------------------------
# Phase 3A ships the dataclass shapes so the Protocol surface is
# complete and Phase 4/5 can reference the kinds. Real population
# is deferred:
# - SinkAuditUnit / EntryAuditUnit need graph-builder enhancements
#   (sink labels, decorator / registration capture).
# - StateAuditUnit / BoundaryAuditUnit / ConfigAuditUnit are
#   deep-only and ship in Phase 5.
#
# The planner does not currently emit any of these kinds. When a
# follow-up change adds the upstream data, the planner gains
# enumeration logic and these shells get their `to_*_payload`
# implementations.


@dataclass(frozen=True)
class SinkAuditUnit:
    """Anchored on a callable that's a known dangerous sink.

    Holds every inbound path reaching the sink along with the
    per-path sanitization-evidence summary. Catches the
    "one inbound path forgets to sanitize" multi-path
    sanitization-gap class — a finding `PathAuditUnit` alone
    misses because it audits each inbound path independently.

    Phase 3A: shell only. Real enumeration requires the graph
    builder to label sinks (`audit.sinks.well_known` config
    list + an upstream sink-labelling pass).
    """

    sink_qualified_name: str
    inbound_paths: tuple[PathRecord, ...] = ()
    graph_slice: GraphSlice = field(
        default_factory=lambda: GraphSlice(
            call_chain=(), function_definitions=(), referenced_symbols=()
        )
    )
    unit_kind: UnitKind = "sink"

    @property
    def unit_id(self) -> str:
        return f"sink::{self.sink_qualified_name}"

    def fingerprint(self) -> str:
        return self.unit_id

    def to_analyzer_payload(self) -> dict[str, object]:
        payload = self.graph_slice.to_payload_dict()
        payload["sink_qualified_name"] = self.sink_qualified_name
        payload["inbound_path_count"] = len(self.inbound_paths)
        payload["inbound_paths"] = [
            {
                "path_fingerprint": p.path_fingerprint,
                "entry_function": p.entry_function,
                "function_names": list(p.function_names),
            }
            for p in self.inbound_paths
        ]
        return payload

    def to_validator_payload(self) -> dict[str, object]:
        return self.to_analyzer_payload()

    def to_exploiter_payload(self) -> dict[str, object]:
        return self.to_analyzer_payload()


@dataclass(frozen=True)
class EntryAuditUnit:
    """Anchored on an entry function.

    Holds the entry's full exposed surface — its parameters,
    downstream sinks, cross-path interactions visible from this
    entry. Catches authz / authentication-boundary findings and
    entry-exposure issues that path-only auditing misses.

    Phase 3A: shell only. Real enumeration awaits the
    decorator / registration capture follow-up so
    `entry_classification` becomes deterministic rather than
    heuristic.
    """

    entry_function_id: str
    entry_qualified_name: str
    downstream_function_ids: tuple[str, ...] = ()
    graph_slice: GraphSlice = field(
        default_factory=lambda: GraphSlice(
            call_chain=(), function_definitions=(), referenced_symbols=()
        )
    )
    unit_kind: UnitKind = "entry"

    @property
    def unit_id(self) -> str:
        return f"entry::{self.entry_function_id}"

    def fingerprint(self) -> str:
        return self.unit_id

    def to_analyzer_payload(self) -> dict[str, object]:
        payload = self.graph_slice.to_payload_dict()
        payload["entry_function_id"] = self.entry_function_id
        payload["entry_qualified_name"] = self.entry_qualified_name
        payload["downstream_function_ids"] = list(self.downstream_function_ids)
        return payload

    def to_validator_payload(self) -> dict[str, object]:
        return self.to_analyzer_payload()

    def to_exploiter_payload(self) -> dict[str, object]:
        return self.to_analyzer_payload()


@dataclass(frozen=True)
class StateAuditUnit:
    """Anchored on a class with mutable instance state.

    Holds the lifecycle (init / mutating methods / cleanup)
    so the analyzer can reason about state-machine violations,
    session fixation, TOCTOU. Phase 5 (deep-only) shipping
    target. Phase 3A: shell only.
    """

    class_id: str
    class_qualified_name: str
    method_ids: tuple[str, ...] = ()
    graph_slice: GraphSlice = field(
        default_factory=lambda: GraphSlice(
            call_chain=(), function_definitions=(), referenced_symbols=()
        )
    )
    unit_kind: UnitKind = "state"

    @property
    def unit_id(self) -> str:
        return f"state::{self.class_id}"

    def fingerprint(self) -> str:
        return self.unit_id

    def to_analyzer_payload(self) -> dict[str, object]:
        payload = self.graph_slice.to_payload_dict()
        payload["class_id"] = self.class_id
        payload["class_qualified_name"] = self.class_qualified_name
        payload["method_ids"] = list(self.method_ids)
        return payload

    def to_validator_payload(self) -> dict[str, object]:
        return self.to_analyzer_payload()

    def to_exploiter_payload(self) -> dict[str, object]:
        return self.to_analyzer_payload()


@dataclass(frozen=True)
class BoundaryAuditUnit:
    """Anchored on a cross-process / async / RPC boundary.

    Holds the producer + consumer sites + message schema.
    Phase 5 shell.
    """

    boundary_kind: str  # "queue" | "rpc" | "async" | "subprocess" | ...
    producer_function_id: str
    consumer_function_ids: tuple[str, ...] = ()
    graph_slice: GraphSlice = field(
        default_factory=lambda: GraphSlice(
            call_chain=(), function_definitions=(), referenced_symbols=()
        )
    )
    unit_kind: UnitKind = "boundary"

    @property
    def unit_id(self) -> str:
        return f"boundary::{self.boundary_kind}::{self.producer_function_id}"

    def fingerprint(self) -> str:
        return self.unit_id

    def to_analyzer_payload(self) -> dict[str, object]:
        payload = self.graph_slice.to_payload_dict()
        payload["boundary_kind"] = self.boundary_kind
        payload["producer_function_id"] = self.producer_function_id
        payload["consumer_function_ids"] = list(self.consumer_function_ids)
        return payload

    def to_validator_payload(self) -> dict[str, object]:
        return self.to_analyzer_payload()

    def to_exploiter_payload(self) -> dict[str, object]:
        return self.to_analyzer_payload()


@dataclass(frozen=True)
class ConfigAuditUnit:
    """Anchored on a config file / env var / deploy manifest.

    Holds the consumed config keys + their default values + the
    deployment context. Phase 5 shell.
    """

    config_source: str  # path-or-uri identifying the config locus
    consumed_keys: tuple[str, ...] = ()
    graph_slice: GraphSlice = field(
        default_factory=lambda: GraphSlice(
            call_chain=(), function_definitions=(), referenced_symbols=()
        )
    )
    unit_kind: UnitKind = "config"

    @property
    def unit_id(self) -> str:
        return f"config::{self.config_source}"

    def fingerprint(self) -> str:
        return self.unit_id

    def to_analyzer_payload(self) -> dict[str, object]:
        payload = self.graph_slice.to_payload_dict()
        payload["config_source"] = self.config_source
        payload["consumed_keys"] = list(self.consumed_keys)
        return payload

    def to_validator_payload(self) -> dict[str, object]:
        return self.to_analyzer_payload()

    def to_exploiter_payload(self) -> dict[str, object]:
        return self.to_analyzer_payload()


# ---------------------------------------------------------------------------
# Adapter — turn a legacy `models.AuditUnit` dataclass into a `PathAuditUnit`
# ---------------------------------------------------------------------------


def as_audit_unit(
    legacy: LegacyAuditUnit, *, graph_slice: GraphSlice | None = None
) -> PathAuditUnit:
    """Wrap a legacy `models.AuditUnit` in a `PathAuditUnit`.

    Used by stage-runner / reconciler code that consumes the
    Protocol surface. The `graph_slice` argument is optional;
    when omitted, the wrapper carries an empty slice (the
    workflow already passes a real slice through to stage agents
    via `path_context`, so the wrapper's slice is mostly used by
    Phase 4+ code that wants to consume the Protocol's
    `to_*_payload` methods directly).
    """

    if graph_slice is None:
        graph_slice = GraphSlice(
            call_chain=(), function_definitions=(), referenced_symbols=()
        )
    return PathAuditUnit(legacy=legacy, graph_slice=graph_slice)


__all__ = [
    "AuditUnit",
    "PathAuditUnit",
    "SinkAuditUnit",
    "EntryAuditUnit",
    "StateAuditUnit",
    "BoundaryAuditUnit",
    "ConfigAuditUnit",
    "UnitKind",
    "as_audit_unit",
]
