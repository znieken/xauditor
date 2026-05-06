# Audit units (Phase 3+)

xauditor's audit pipeline iterates over **audit units** — each unit
is a self-contained slice of the code graph that the
analyzer / validator / exploiter chain reasons about as one. Phase 3
introduces an `AuditUnit` Protocol so the workflow can grow new
unit kinds without churning stage-runner code.

## The six unit kinds

| Unit | Anchored on | Catches | Status |
|---|---|---|---|
| `PathAuditUnit` | one call chain (entry → … → leaf) | linear data-flow vulns: SQLi, RCE, SSRF, path traversal, deserialization, XSS, XXE | shipped (Phase 1+) |
| `SinkAuditUnit` | one dangerous sink + every inbound path | multi-path sanitization gaps ("one inbound path forgets to sanitize") | shipped — emitted under `audit.experimental.units: true` |
| `EntryAuditUnit` | one entry function + its full exposed surface | authz / authentication boundaries, entry exposure | shipped — emitted under `audit.experimental.units: true` |
| `StateAuditUnit` | one stateful class + lifecycle | state-machine violations, session fixation, TOCTOU | shipped — emitted under `audit.experimental.units: true` |
| `BoundaryAuditUnit` | one cross-process / async / RPC boundary | producer-consumer taint propagation across processes | shipped — emitted under `audit.experimental.units: true` |
| `ConfigAuditUnit` | one config file / env var / deploy manifest | misconfiguration, deployment-driven exposure | deferred — see [`capture-decorators-and-registrations` 2.2.6](../openspec/changes/capture-decorators-and-registrations/tasks.md) |

## Status today

The Protocol seam ships in `src/xauditor/audit/units.py`. The
planner enumerates **five** of the six unit kinds when
`audit.experimental.units: true` is set; `ConfigAuditUnit` is
deferred until real audit gaps justify the extra walker layer
(yaml/Helm/`BaseSettings`).

What each unit kind anchors on, in code:

- **`PathAuditUnit`** — one entry-rooted call chain. Always
  emitted (one per planned path); the historical workflow
  baseline.
- **`SinkAuditUnit`** — one Function with `is_well_known_sink:
  true` (set at graph-build time by `sink_labelling.py`) plus
  every planned path that reaches it. See
  [Sink labels](sink-labels.md) for the default sink list and
  the `audit.sinks.custom` extension point.
- **`EntryAuditUnit`** — one function whose decorator chain
  carries `intent ∈ {route, task_handler, cli_entry}`, OR with
  an inbound REGISTERS edge from a registration call site
  (`app.add_url_rule(url, view)`,
  `urlpatterns = [path(url, view)]`, etc.). See
  [Framework heuristics](framework-heuristics.md) for the
  decorator → intent table.
- **`StateAuditUnit`** — one Class whose methods include `>= 2`
  with `mutates_self: true` (set at parse time by
  `PythonParser._detect_self_mutation` — Python only today;
  default False on cross-language records).
- **`BoundaryAuditUnit`** — one (producer, consumer) pair where
  the consumer is a sink with `sink_kind ∈ {http_client,
  subprocess}` reached on a planned path. Producer = the
  function in the path immediately preceding the sink (falls
  back to the path entry when the sink is the first hop).
  De-duplicated across paths sharing the same call.

The `audit.experimental.units` flag (default `false`) gates
sink / entry / state / boundary emission. With the flag off,
the planner returns only `PathAuditUnit` instances — the
historical baseline.

### Stage-prompt families

`analyzer_sink`, `validator_sink`, `exploiter_sink`,
`analyzer_entry`, `validator_entry`, `exploiter_entry` are
registered in the prompt registry at v1. The stage runner
keeps using the path-shaped prompt family for state / boundary
units today; kind-specific state / boundary prompts can be
slotted in via the same registry seam without touching the
runner.

## The Protocol seam

The workflow loop consumes the `AuditUnit` Protocol surface
(`unit_id`, `unit_kind`, `graph_slice`, `to_*_payload()`,
`fingerprint()`) rather than the historical `models.AuditUnit`
dataclass. Each unit kind is a separate dataclass implementing
the Protocol, so adding a new kind needs no stage-runner
changes — the runner dispatches on `unit_kind` to pick a prompt
family.

`as_audit_unit(legacy_unit, *, graph_slice=...)` is the adapter
that wraps a historical `models.AuditUnit` in a `PathAuditUnit`
view of the Protocol — it's the bridge between today's workflow
loop and the new contract.

## Configuration

```yaml
audit:
  experimental:
    units: false   # Default. With `false`, the planner emits
                   # one `PathAuditUnit` per planned path —
                   # historical baseline.
                   # Set to `true` to also emit
                   # `SinkAuditUnit` / `EntryAuditUnit` /
                   # `StateAuditUnit` / `BoundaryAuditUnit`
                   # alongside path units. (`ConfigAuditUnit`
                   # is deferred — see 2.2.6 in tasks.md.)
```

## Env-var binding

| Env var | Maps to |
|---|---|
| `XAUDITOR_AUDIT_EXPERIMENTAL_UNITS` | `audit.experimental.units` |

## See also

- [Audit modes](audit-modes.md) — `fast` / `deep` mode preset
  resolves the `audit.units` set in a future change
- [Sink labels](sink-labels.md) — default sink list,
  `sink_kind` taxonomy, and `audit.sinks.custom` extension
  syntax
- [Framework heuristics](framework-heuristics.md) — the
  built-in decorator → framework / intent table and the
  resolution algorithm
- [Configuration reference](configuration.md) — full
  `xauditor.yml` schema
