# Audit modes (`fast` / `deep`)

xauditor's audit pipeline is parameterised by a single mode preset
that resolves to per-stage replication, validator-debate, and
per-unit finding-cap defaults. Operators normally set just
`audit.mode`; advanced overrides are available for the underlying
fields.

## When to pick each mode

| | `fast` (default) | `deep` (opt-in) |
|---|---|---|
| **Positioning** | CI gate / large-batch scan | PSIRT pre-release / post-incident review |
| **Replicas per stage** | 1 / 1 / 1 (analyzer / validator / exploiter) | 3 / 3 / 1 |
| **Validator debate** | off | on (max 2 rounds, halt on consensus) |
| **Multi-finding mechanism** | iterative analyzer with `excluded_findings` (cap `audit.max_findings_per_unit`, default 3) | replication × persona × dedup (cap = `replication.analyzer`) |
| **Approx tokens / finding** | 5K – 10K | 50K – 200K |
| **Wall-clock per audit** | seconds–minutes | minutes–hours |

`fast` is the default. Operators who never set `audit.mode` (or
who used the legacy `teaming.enabled: false` shape) are running
in fast mode.

## Configuration

```yaml
audit:
  mode: fast              # fast | deep — preset for the fields below
  # max_findings_per_unit: 3   # bounded [1, 20]; cap for fast mode's
                                #   iterative analyzer loop. Deep mode
                                #   is implicitly capped at
                                #   replication.analyzer.

  # Advanced overrides — only set these when the preset isn't right.
  # The mode preset already resolves all of them.
  # replication:
  #   analyzer: 3
  #   validator: 3
  #   exploiter: 1
  # validator:
  #   debate:
  #     enabled: true
  #     max_rounds: 2
  #     halt_on_consensus: true
```

The mode preset resolves to:

| Field | `fast` resolves to | `deep` resolves to |
|---|---|---|
| `audit.replication.analyzer` | `1` | `3` |
| `audit.replication.validator` | `1` | `3` |
| `audit.replication.exploiter` | `1` | `1` |
| `audit.validator.debate.enabled` | `false` | `true` |
| `audit.validator.debate.max_rounds` | `5` (no-op when disabled) | `2` |
| `audit.validator.debate.halt_on_consensus` | `true` | `true` |
| `audit.max_findings_per_unit` | `3` | `3` |

Operators MAY override any field directly. Workflow code reads
the resolved per-axis values and SHALL NOT branch on
`audit.mode` itself, so e.g. `audit.mode: fast` plus
`audit.replication.analyzer: 3` is meaningful — three
prompt-driven analyzer replicas, no debate.

`audit.mode` is validated against the closed set
`{"fast", "deep"}`; unknown values raise `ConfigError` at
config-parse time naming the offending dot-path.

## Environment overrides

| Env var | Maps to |
|---|---|
| `XAUDITOR_AUDIT_MODE` | `audit.mode` |
| `XAUDITOR_AUDIT_REPLICATION_ANALYZER` | `audit.replication.analyzer` |
| `XAUDITOR_AUDIT_REPLICATION_VALIDATOR` | `audit.replication.validator` |
| `XAUDITOR_AUDIT_REPLICATION_EXPLOITER` | `audit.replication.exploiter` |
| `XAUDITOR_AUDIT_VALIDATOR_DEBATE_ENABLED` | `audit.validator.debate.enabled` |
| `XAUDITOR_AUDIT_VALIDATOR_DEBATE_MAX_ROUNDS` | `audit.validator.debate.max_rounds` |
| `XAUDITOR_AUDIT_VALIDATOR_DEBATE_HALT_ON_CONSENSUS` | `audit.validator.debate.halt_on_consensus` |
| `XAUDITOR_AUDIT_MAX_FINDINGS_PER_UNIT` | `audit.max_findings_per_unit` |
| `XAUDITOR_AUDIT_PERSIST_FALSE_POSITIVES` | `audit.persist_false_positives` (boolean, default `false`) |

## Stage order

Both modes execute per-unit stages as **`Analyzer → Validator →
Exploiter`**. The validator's payload SHALL NOT include
exploitation status or steps in any mode (the exploiter has not yet
run when the validator is invoked). The exploiter MAY emit
`status: "not_exploitable"`, in which case xauditor downgrades a
`Valid` verdict to `Partial Valid` and prepends an
`[exploiter downgrade] ...` marker to the validation analysis;
the validator is NOT re-invoked on the downgrade.

`False Positive` validator verdicts short-circuit the rest of the
per-finding pipeline. The exploiter is skipped (the finding is
recorded with a placeholder `exploitation.status="skipped"`) AND the
coder verification stage is skipped regardless of `coder.enabled`
(the finding is emitted with `coder_status: "Skipped"`, matching the
`coder.enabled = false` shape). The skip is a structural pipeline
property — there is no opt-out knob.

The `audit.persist_false_positives` knob (boolean, default `false`)
extends the bypass to the persistence and export boundary. When
`false`, FP findings stay in the in-memory snapshot (so coverage
and per-stage counts remain accurate) but SHALL NOT be upserted
into the reportdb sink bus, included in the Neo4j
`persist_audit_run` payload, or rendered into `false-positives.md`
(the artifact is emitted with only its header and an explanatory
note pointing at the knob). Operators who want validator-quality
triage from reportdb / Neo4j set
`audit.persist_false_positives: true` in `xauditor.yml` (or
`XAUDITOR_AUDIT_PERSIST_FALSE_POSITIVES=true` in the environment).

## Multi-finding per audit unit

Both modes support producing multiple distinct findings from a
single audit unit:

- **`fast`** invokes the analyzer iteratively. Each round, the
  analyzer's user payload includes an `excluded_findings` list
  (the `(finding_name, suspect_function_id, suspect_line)` keys
  of every accepted candidate so far); the analyzer is asked to
  return ONE additional distinct candidate or
  `status: "no_issue"`. The loop terminates on
  two-consecutive-`no_issue` OR
  `len(accepted) >= audit.max_findings_per_unit`. A
  `"max_findings_per_unit cap reached"` warning fires when the
  cap caps the loop.
- **`deep`** uses the existing N-replica + dedup pipeline (one
  candidate per replica, deduplicated across replicas). The
  number of distinct findings per unit is bounded by
  `audit.replication.analyzer`. A `"replication-cap reached"`
  warning fires when every replica produces a distinct
  surviving candidate (signalling more candidates may exist —
  consider raising the replication count).

## Cost model

Cost scales roughly linearly in the number of LLM calls per unit:

- **`fast`** worst case: `cap × stages` LLM calls per unit.
  Default cap `3` and three stages (analyzer + validator + exploiter)
  → up to 9 calls per unit, ~5K tokens each → ~50K tokens / unit.
- **`deep`** worst case (per finding): `replication.analyzer ×
  analyzer + replication.validator × validator × debate_rounds +
  exploiter`. Default 3+3+1 with 2 debate rounds → ~10 calls /
  finding × ~10K tokens / call → ~100K tokens / finding.

Multiply by the unit count for a rough whole-audit estimate.

## Migrating from `teaming.*` (legacy)

Yaml files using the pre-rename `teaming.*` shape continue to
work for one minor release. The translation table:

| Legacy (`teaming.*`) | New (`audit.*`) |
|---|---|
| `teaming.enabled: true` | `audit.mode: deep` |
| `teaming.enabled: false` (or absent) | `audit.mode: fast` |
| `teaming.analyzer.subagent_count` | `audit.replication.analyzer` |
| `teaming.validator.subagent_count` | `audit.replication.validator` |
| `teaming.exploiter.subagent_count` | `audit.replication.exploiter` |
| `teaming.validator.debate_rounds` | `audit.validator.debate.max_rounds` (also implies `debate.enabled: true`) |
| `teaming.analyzer.provider_list` | `audit.analyzer.provider_list` |
| `teaming.validator.provider_list` | `audit.validator.provider_list` |
| `teaming.exploiter.provider_list` | `audit.exploiter.provider_list` |

`xauditor` emits one `DeprecationWarning` per populated legacy
field, naming the new dot-path so operators can mechanically
rewrite their yaml. Both shapes can coexist during the
migration; the new shape wins on collision.

**Provider list home**: per-stage provider rotation lives at
`audit.analyzer.provider_list` / `audit.validator.provider_list` /
`audit.exploiter.provider_list`. The legacy
`teaming.<stage>.provider_list` keys still parse for one minor
release with a `DeprecationWarning`; after that they are removed
along with the rest of the `teaming.*` block.

## Output layout

Every audit run directory contains the consolidated stage
Markdown files. When `replication.<stage> > 1` (typically deep
mode), per-replica subagent records and validator-debate
transcripts also land in the run directory under
`analyzer-subagents/`, `validator-subagents/`, `exploiter-subagents/`,
and `validator-debates/`. The portal renders the same structure
inside each finding card.

## GraphSlice — per-unit context broadening

Phase 2A renamed the per-audit-unit `path_context` dict to a
structured `GraphSlice` dataclass and added five new context
fields. The new fields are gated behind `audit.graph_slice`
(default **`true`** since 1.4.x — promoted from
`audit.experimental.graph_slice` once the data shipped reliably
populated). When on, analyzer / validator payloads gain a
`graph_slice` block:

```yaml
audit:
  graph_slice: true       # default true; set false to roll back
                          #   to the legacy three-field payload
```

The legacy `audit.experimental.graph_slice` shape is accepted
for one minor release with a `DeprecationWarning`; remove it
from `xauditor.yml` and use the top-level field instead.

| Field | What | Status |
|---|---|---|
| `call_chain` | function refs in topological order | always present |
| `function_definitions` | full source dicts for each function on the path | always present |
| `referenced_symbols` | module-level symbols referenced by the call chain | always present |
| `entry_classification` | `EntryKind` heuristic (`public_http` / `admin_http` / `internal_rpc` / `cron` / `cli` / `test_only` / `unknown`) derived from entry function name + file path | populated when flag on |
| `type_context` | type annotations + ORM-column detection on referenced symbols | populated when flag on |
| `cross_path_definitions` | for symbols read on the path but defined in functions outside it, the definition sites — solves the "sanitization happened on a sibling path" false-negative class | populated when flag on |
| `decorator_chain` | every decorator applied to functions on the path, with `position` (0 = closest to the function) + `framework` + `intent` | populated when flag on |
| `registration_context` | framework-registration sites that bind functions into request / task pipelines (`app.add_url_rule(...)`, `urlpatterns = [path(...)]`, `app.add_event_handler(...)`) | populated when flag on |

All five new fields ship usable data:

- The first three read from existing `ModuleSymbolRecord` /
  `USES_SYMBOL` / `DECLARES_SYMBOL` data.
- `decorator_chain` and `registration_context` read from the
  `Decorator` / `RegistrationSite` graph data captured by
  `capture-decorators-and-registrations` — the parser walks
  `decorator_list` and registration call-site patterns at
  graph-build time, the repository writes `Decorator` /
  `RegistrationSite` nodes, and `build_graph_slice` joins
  them onto each path's functions when the flag is on. See
  [Framework heuristics](framework-heuristics.md) for the
  decorator → framework / intent table the analyzer sees in
  `decorator_chain`.

When the flag is off the new fields are emitted as empty tuples
/ `unknown`; the legacy three keys are still present at the top
level of the payload, so existing stage prompts that read
`payload["call_chain"]` keep working unchanged.

### Env-var binding

| Env var | Maps to |
|---|---|
| `XAUDITOR_AUDIT_GRAPH_SLICE` | `audit.graph_slice` (default `true`) |
| `XAUDITOR_AUDIT_EXPERIMENTAL_GRAPH_SLICE` | legacy alias — `DeprecationWarning`, removed in next minor release |

## Stage form: prompt vs agentic (Phase 4+)

`audit.stages.form` selects how each per-stage agent
(analyzer / validator / exploiter) executes:

```yaml
audit:
  stages:
    form: prompt   # prompt | agentic
```

| Value | Runner | Status |
|---|---|---|
| `prompt` (default) | `PromptStageRunner` — single JSON-mode chat completion per stage | shipped (Phase 4A) |
| `agentic` | `AgenticStageRunner` — Claude Code agent with constrained tool grants | **shipped** runner + transport (Phase 1+2 of `agentic-stage-runner-real`); workflow re-platform pending — see [`agentic-stage-runner.md`](./agentic-stage-runner.md) for the full story |

The value is **run-scoped today**: the same form applies to every
stage in the run. A future change may extend
`audit.stages.form` to a per-stage map (`{analyzer: prompt,
validator: agentic, exploiter: prompt}`) — see `design.md`
open question 5.

The deep mode preset does NOT yet flip `stages.form` to
`agentic` automatically. Two reasons: (1) the workflow's
`_process_unit_*` paths still call the prompt agents
directly rather than dispatching through `StageRunner` (the
deferred Phase 2.2 re-platform of `agentic-stage-runner-real`),
and (2) flipping the preset would obligate every operator to
have a `claude-code` binary on PATH. Operators who want the
agentic surface today must opt in explicitly.

## Pre-flight cost estimate

Before any LLM call, xauditor emits a one-line cost estimate
to stderr (and to the run logger when present) so operators
see the order of magnitude up front:

```
[estimate] mode=fast, ~100 units → ~900 LLM calls,
~1350K-2700K input tokens. Cost depends on your provider's
per-token pricing.
```

The math is conservative — the worst-case assumes every audit
unit accepts the maximum number of findings (fast: cap; deep:
`replication.analyzer`). Actual runs typically land at
~30–60% of the worst case.

## Personas (deep mode replica seeding)

Deep mode runs `audit.replication.<stage>` parallel replicas of
each stage. Each replica is seeded with a **persona** that
nudges it toward a different aspect of the same audit unit, so
the dedup pipeline gets non-trivially-different candidates to
consolidate.

```yaml
audit:
  personas:
    - name: data_flow
      focus_summary: Linear taint propagation through the slice
      extra_system_prefix: |
        Focus on data flow from sources to sinks. Pay close
        attention to sanitization, encoding, and type
        transformations.
      tool_emphasis: [read_file, query_graph]
    - name: auth_boundaries
      focus_summary: Authentication / authorization gaps
      extra_system_prefix: |
        Focus on trust-boundary transitions. Inspect
        decorator chains, middleware registration, and
        entry classification before evaluating any sink.
      tool_emphasis: [read_file, grep, query_graph]
    - name: config_assumptions
      focus_summary: Deployment / config-driven assumptions
      extra_system_prefix: |
        Focus on assumptions the code makes about its
        deployment context: internal-network only,
        feature-flagged, debug-only routes, etc.
      tool_emphasis: [read_config, grep, read_file]
```

When `audit.personas` is empty, deep mode uses the built-in
`DEFAULT_DEEP_PERSONAS` (the three above). When
`audit.replication.<stage>` exceeds the persona-list length,
the resolver repeats from the start of the list (replica `i`
gets `personas[i % len(personas)]`).

`tool_emphasis` is consumed by `AgenticStageRunner` only.
`PromptStageRunner` ignores the field.

Persona is dropped entirely when `audit.replication.<stage>
== 1` (fast-mode default) — single-replica runs have no
diversity dimension to exploit.

### Stage-runner status

| Runner | Implementation | Workflow integration |
|---|---|---|
| `PromptStageRunner` | shipped + real | wired end-to-end (`wire-agentic-into-workflow` Phase 1) — `_process_unit_single` dispatches through `self.stage_runner.run_*(...)`, which `PromptStageRunner` forwards to the prompt agents. Behaviour is bit-for-bit identical to pre-1.3.0. |
| `AgenticStageRunner` | shipped + real — `CoderServiceAgentTransport` (HTTP POST to coder-service's `/agent_invocations` endpoint), structured response parsing, fallback semantics, persona injection, transcript capture | **Fast mode**: wired end-to-end via `_process_unit_single` (since 1.3.0). **Deep mode**: `AnalyzerTeam` and `ExploiterTeam` also dispatch through the agentic runner (per-replica via `stage_runner.run_*(persona=p[i], subagent_id=i)`). **`ValidatorTeam` is intentionally NOT agentic** — its multi-round debate machinery is turn-by-turn and doesn't fit one-shot `/agent_invocations` dispatch; re-platforming it is a separate change. So in any deep run, validator is always prompt-form, while analyzer + exploiter follow `audit.stages.form`. |

For the full agentic stage-runner story — transport
contract, tool grants, fallback budget, env-var bindings,
operational guidance — see
[`agentic-stage-runner.md`](./agentic-stage-runner.md).

### Multi-model / multi-provider rotation per stage × form

Per-replica model rotation (replica 0 → provider A, replica 1 →
provider B, …) is wired to `audit.<stage>.provider_list`. The
rotation rule is `provider_list[index % len(provider_list)]`.
Whether each replica actually USES its rotated provider depends on
which runner the team class dispatches through:

| Stage | Form | Per-replica multi-model? | Diversity comes from |
|---|---|---|---|
| **Analyzer** | `prompt` | ✅ yes — `provider_list` rotates per replica | provider/model/sampling × persona |
| **Analyzer** | `agentic` | ❌ no — every replica targets one shared coder-service container, which is bound at `docker run` time to one `(ANTHROPIC_BASE_URL, ANTHROPIC_MODEL, ANTHROPIC_API_KEY)` triple. `provider_list` is silently ignored. | persona (`extra_system_prefix`) only |
| **Validator** | (any) | ✅ yes — `ValidatorTeam` is always prompt-form regardless of `audit.stages.form`, so `validator.provider_list` rotates per replica even when `stages.form: agentic`. Debate rounds re-use the per-replica model assignments. | provider/model/sampling × debate |
| **Exploiter** | `prompt` | ✅ yes — same rotation as analyzer | provider/model/sampling × persona |
| **Exploiter** | `agentic` | ❌ no — same single-container constraint as analyzer | persona only |

To get model-level diversity in agentic mode for analyzer / exploiter,
the deployment needs N coder-service containers (each baked with a
different `ANTHROPIC_MODEL`) plus an agentic transport that fans
replicas across them. That is **not implemented today** — the
transport carries one `coder_service.endpoint` value. File a change
proposal if you need it; the wiring point is
`AgenticStageRunner.run_*` dispatching to a multi-endpoint transport
keyed by `subagent_id`.

### Env-var binding (Phase 4+)

| Env var | Maps to |
|---|---|
| `XAUDITOR_AUDIT_STAGES_FORM` | `audit.stages.form` |
| `XAUDITOR_AUDIT_AGENTIC_TIMEOUT_SECONDS` | `audit.agentic.timeout_seconds` |
| `XAUDITOR_AUDIT_AGENTIC_TRANSPORT_KIND` | `audit.agentic.transport.kind` (only `coder_service` accepted today) |
| `XAUDITOR_AUDIT_AGENTIC_TRANSPORT_CODER_SERVICE_ENDPOINT` | `audit.agentic.transport.coder_service.endpoint` |
| `XAUDITOR_AUDIT_AGENTIC_TRANSPORT_CODER_SERVICE_BEARER_TOKEN_ENV` | `audit.agentic.transport.coder_service.bearer_token_env` |
| `XAUDITOR_AUDIT_AGENTIC_TRANSPORT_CODER_SERVICE_REQUEST_TIMEOUT_SAFETY_SECONDS` | `audit.agentic.transport.coder_service.request_timeout_safety_seconds` |
| `XAUDITOR_AUDIT_AGENTIC_TRANSPORT_CODER_SERVICE_PROJECT` | `audit.agentic.transport.coder_service.project` |

## AuditUnit kinds (Phase 3+)

The audit workflow iterates over **audit units**. Phase 3A
introduces an `AuditUnit` Protocol with six concrete kinds —
`PathAuditUnit` (the historical default) plus shells for `sink`,
`entry`, `state`, `boundary`, and `config`. Today the planner
emits only `path` units regardless of `audit.mode`. Once the
graph-builder follow-up adds sink labels + decorator capture, the
`audit.experimental.units` flag will gate sink / entry enumeration
in **both** modes (`fast` and `deep`); state / boundary / config
remain Phase 5 deep-mode-only.

→ See [Audit units](audit-units.md) for the per-kind detail.

## Coverage Gaps (Phase 5+)

Every audit run emits a `Coverage Gaps` payload naming the
vulnerability classes the run covered, the classes a different
mode would have covered, and the classes outside xauditor's
scope altogether. The payload answers the operator's reasonable
post-audit question: *"the tool found nothing — what didn't it
look at?"* It surfaces in three places:

- the Markdown report's `Coverage Gaps` section,
- the JSON envelope's top-level `coverage_gaps` field, and
- the **portal Coverage panel** rendered above the findings list
  on each run-detail page (the primary user-facing surface;
  see [`portal.md`](./portal.md#coverage-panel-findings-sub-tab)
  for the panel's group-by-group breakdown + the fast-mode CTA
  banner behaviour).

```yaml
audit:
  coverage_gaps:
    report: true   # default true; set false to suppress the section
```

Per-mode taxonomy (Phase 5A):

| Class | Audited by |
|---|---|
| `sql_injection` / `command_injection` / `ssrf` / `path_traversal` / `deserialization` / `xss` / `xxe` | Path unit (both modes) |
| `sink_convergence` | Sink unit (both modes — populates when SinkAuditUnit enumeration ships) |
| `entry_exposure` | Entry unit (both modes — same caveat) |
| `state_machine_violation` / `toctou_race` | State unit (deep only) |
| `cross_process_taint` | Boundary unit (deep only) |
| `configuration_misuse` | Config unit (deep only) |
| `supply_chain` | NEVER — out of scope; use OSV / SCA |
| `business_logic_idor` | NEVER — needs human review |
| `cryptographic_primitives` | NEVER — out of scope |
| `race_condition` | NEVER — out of scope (use targeted dynamic-analysis tooling) |
| `unknown_unknowns` | NEVER — by definition |

In fast mode the section's `advice_to_user` is an
operator-actionable CTA: "Run with `audit.mode: deep` to also
cover `state_machine_violation`, `toctou_race`,
`cross_process_taint`, `configuration_misuse`."

In deep mode the CTA is empty when no classes are skipped —
operators see only the "audited" + "out of scope" lists.

### Env-var binding

| Env var | Maps to |
|---|---|
| `XAUDITOR_AUDIT_COVERAGE_GAPS_REPORT` | `audit.coverage_gaps.report` |

## Cross-unit reconciler (Phase 5+)

When a finding surfaces from MULTIPLE audit-unit kinds — e.g.
the same SQL-injection finding emerging from `PathAuditUnit` +
`SinkAuditUnit` + `EntryAuditUnit` — the deep-mode reconciler
stage consolidates the per-unit verdicts into one
`consolidated_verdict` plus a `consolidation_reasoning` prose
block. The portal renders both the per-unit verdicts panel
(side-by-side) and the consolidated verdict on the finding card.

Phase 5A ships:

- The `reconciler.py` module + Protocol seam.
- A `PassthroughReconciler` that groups by finding fingerprint
  and returns each group's first finding unchanged. For
  Path-only audits (today's planner output) every group has
  size 1, so the reconciler is structurally a no-op.
- The `findings.reconciliation` JSONB column (alembic 0012)
  for the per-finding `{per_unit_verdicts,
  consolidated_verdict, consolidation_reasoning}` payload.
- The `reconciler` v1 stage prompt reserved in the registry.

Real LLM-driven `AgenticReconciler` ships alongside the
`agentic-stage-runner-real` follow-up because it shares the
same SDK choice as `AgenticStageRunner`. Until then,
`reconciliation` is `NULL` on every persisted finding.

## See also

- [Coder verification](coder.md) — pairs naturally with deep
  mode for a 4th, repo-global verdict per finding
- [Providers](providers.md) — provider declaration syntax
- [Configuration reference](configuration.md) — full
  `xauditor.yml` schema
- [Portal Settings](portal-settings.md) — UI-editable subset of
  the audit-mode fields
- [Audit units](audit-units.md) — per-kind detail for the six
  AuditUnit kinds the reconciler consolidates across
