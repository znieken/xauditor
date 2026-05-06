# Agentic stage runner

`AgenticStageRunner` is the second `StageRunner` implementation
that ships alongside `PromptStageRunner`. Where the prompt
runner sends a single JSON-mode chat completion per stage, the
agentic runner POSTs to xauditor-coder-service's
`/agent_invocations` endpoint, which spawns a tool-using
`claude` subprocess against the same audit slice with a
hard wall-clock budget.

## Mode selection

```yaml
audit:
  stages:
    form: prompt   # prompt | agentic — default `prompt`
```

| Value | Runner | Status |
|---|---|---|
| `prompt` (default) | `PromptStageRunner` — single JSON-mode chat completion per stage | shipped + wired end-to-end |
| `agentic` | `AgenticStageRunner` — `claude` via coder-service `/agent_invocations` | shipped + wired end-to-end **for fast mode**; deep-mode (teaming) wiring is a follow-up |

The value is **run-scoped**: one form for every stage in the
run. A future change may extend `audit.stages.form` to a
per-stage map (e.g. `{analyzer: prompt, validator: agentic,
exploiter: prompt}`).

## Transport

```yaml
audit:
  agentic:
    transport:
      kind: coder_service
      coder_service:
        endpoint: http://127.0.0.1:8090
        bearer_token_env: ""           # name of an env var holding the bearer token
        request_timeout_safety_seconds: 60
        project: ""                     # leave empty -- auto-derived from
                                        #   coder.workspace_root + repo_root.
                                        #   Set explicitly only to override.
```

### Project name auto-derivation

`audit.agentic.transport.coder_service.project` selects which
subdirectory inside the coder-service container's `/workspace`
mount the per-stage agentic call runs `claude` in. **You
usually don't need to set it.**

When it's empty (the default), xauditor derives the project
name at workflow construction using the SAME logic the
verification coder uses (`preflight.derive_project_from_workspace`):

1. **Operator override wins.** A non-empty
   `audit.agentic.transport.coder_service.project` value is
   used as-is — for multi-tenant deployments where the
   workspace mount layout diverges from the host directory
   layout.
2. **`repo_root` relative to `coder.workspace_root`.** When
   the audit target lies inside the coder workspace mount,
   the project is the `/`-joined relative path (e.g.
   `team/repo` for `coder.workspace_root=/ws` and
   `repo_root=/ws/team/repo`). This matches what
   `GET /projects` emits.
3. **`basename(repo_root)` legacy fallback.** Used when
   `coder.workspace_root` is empty (single-repo container)
   or when `repo_root` isn't relative to `workspace_root`.

In other words: the agentic transport and the verification
coder share the same project namespace because they typically
talk to the same coder-service container. Configure
`coder.workspace_root` once (under the `coder.*` block) and
the agentic transport picks up the same derivation
automatically.

Only `coder_service` ships today. The transport layer is a
`Protocol` (`xauditor.audit.agent_transport.AgentTransport`)
so a future variant — or a fully-mocked transport for tests —
can drop in without touching the runner.

`CoderServiceAgentTransport` POSTs to
`<endpoint>/agent_invocations` with the request body the
endpoint accepts (`system_prompt`, `user_payload`,
`response_schema` (JSON Schema), `timeout_seconds`,
`project`). The HTTP request times out at
`timeout_seconds + request_timeout_safety_seconds` (default
`300 + 60 = 360s`); the orchestrator inside coder-service
enforces `timeout_seconds` precisely.

The transport runs `GET /health` once at workflow
construction as a startup probe; `RuntimeError` fires with
the offending config field name + endpoint URL when
coder-service is unreachable so misconfigured operators see
the failure before any per-stage call.

**Why coder-service, not direct subprocess**: the
coder-service container hosts the single `claude` install
for an xauditor deployment. Operators install + auth
`claude` once (in coder-service); both Coder verifications
(per-finding, async via `/verifications`) AND agentic stage
calls (per-stage, synchronous via `/agent_invocations`) reuse
the same install + sandbox + `asyncio.Semaphore(max_concurrent_jobs)`
admission control. See [`coder.md`](./coder.md#agent-invocations-endpoint)
for the endpoint contract.

## Tool grants — container-level, not per-call

The xauditor side does **not** send tool-grant fields per
call. The coder-service container is the security sandbox
— OS / container / network isolation is the boundary.
Operators who want finer per-tool restrictions configure
them via claude's own settings inside the container (e.g.
`~/.claude/settings.json` with `permissions: {allow: [...],
deny: [...]}`). The HTTP request body doesn't re-encode
them per call.

This is a deliberate departure from `agentic-stage-runner-real`
Phase 1's design (which assumed `--tool-grants` JSON flags
on the `claude` CLI that don't exist). The
`wire-agentic-into-workflow` change drops the `tool_grants`
parameter from the `AgentTransport` Protocol entirely along
with the `audit.agentic.tools.*` config block.

## Cost budget — wall-clock only

```yaml
audit:
  agentic:
    timeout_seconds: 300    # default 300, bounded [10, 1800]
```

Every transport call enforces the wall-clock budget:
coder-service SIGTERMs the inner subprocess when the timer
expires. There is no `max_tool_calls` count limit (real
`claude` CLI has no such flag) and no per-call dollar
budget. Operators control total parallelism via
coder-service's `XAUDITOR_CODER_SERVICE_MAX_CONCURRENT`
(also bounds the existing `/verifications` endpoint —
both endpoints share the semaphore).

## Fallback semantics

The runner returns a fallback `AgentResult` (with
`fell_back: True` and a `fallback_reason` string) when:

- coder-service returns 4xx (auth / malformed request /
  unknown project / invalid response_schema):
  `fallback_reason="coder_service_<status>: <body>"`.
- coder-service returns 5xx (infra failure):
  `fallback_reason="coder_service_<status>: <body>"`.
- HTTP connection refused / DNS failure:
  `fallback_reason="coder_service_unavailable: <error>"`.
- HTTP request times out:
  `fallback_reason="http_timeout"`.
- coder-service returns 200 OK with `fell_back: true`
  (claude crashed, hit the wall-clock timeout, produced
  malformed JSON, produced JSON that failed the supplied
  `response_schema`, or produced empty stdout): the
  endpoint's `fallback_reason` propagates through
  unchanged.

In every fallback case the `final_answer` field still holds
a usable response_model instance (with default values) so
downstream code consumes a stable shape. The structured
warning + `fallback_reason` lets operators distinguish
"the network/auth is broken" from "claude ran but didn't
deliver".

## Persona injection

Personas (configured under `audit.personas` — see
[`audit-modes.md`](./audit-modes.md#personas-deep-mode-replica-seeding))
are real for the agentic runner: each persona's
`extra_system_prefix` is appended to the stage's system
prompt verbatim. The dedup pipeline (deep mode, once
Phase 2 wiring lands) gets non-trivially-different
candidates because each replica's agent receives a
distinct system prompt.

Single-replica fast-mode runs drop persona — no diversity
dimension to exploit.

## Stage prompts

The runner picks a stage prompt by resolution order:

1. Unit-kind-specific agentic prompt (e.g.
   `analyzer_sink_agentic`) — reserved for future
   expansion; not yet shipped.
2. Generic agentic prompt (`analyzer_agentic` /
   `validator_agentic` / `exploiter_agentic`).
3. Unit-kind-specific prompt-form prompt (e.g.
   `analyzer_sink`) — fallback when the agentic
   variant doesn't exist.
4. Generic prompt-form prompt (`analyzer` /
   `validator` / `exploitation`) — final fallback.

The agentic variants are tool-using formulations: "use
`Read` to inspect the slice's source, `Grep` to confirm
reachability, then return your verdict via
`final_answer`."

## Cross-unit reconciler

`AgenticReconciler` ships alongside the runner because it
shares the same transport. It groups findings by
fingerprint:

- **Single-unit groups** (today's planner output for path
  units) shortcut to passthrough — no agent call, no cost.
- **Multi-unit groups** (a finding surfacing from a path
  unit AND a sink unit AND an entry unit, etc.) invoke
  the parent change's `reconciler` v1 prompt via the same
  transport. The agent receives every per-unit verdict +
  evidence pack and returns a `consolidated_verdict` +
  `consolidation_reasoning` written to
  `findings.reconciliation` for the portal's Per-Unit
  Verdicts panel.

The reconciler is selected by `build_reconciler(audit_mode,
transport)` — `AgenticReconciler` when
`audit.stages.form: agentic`, else
`PassthroughReconciler` (the `restructure-audit-modes-and-coverage`
Phase 5A baseline).

The workflow calls `reconciler.reconcile(findings)` exactly
once at run end, after every per-unit chain has completed
and findings have streamed to the live sink. Each finding
gains a `reconciliation` JSONB payload (alembic 0012);
`PassthroughReconciler` produces a structurally complete
single-entry payload so the portal Per-Unit Verdicts panel
renders the single-unit case correctly.

## Transcript persistence

`findings.agentic_transcript` (alembic `0013`) is a JSONB
column populated by every agentic-mode finding. Shape: one
list entry per stage call (analyzer / validator / exploiter
/ reconciler), each carrying the `transport.invoke(...)`
transcript:

```json
[
  {
    "stage": "analyzer",
    "tool_call_count": 7,
    "fell_back": false,
    "fallback_reason": null,
    "transcript": [
      {"tool": "Read", "input": {"path": "..."}, "output": "..."},
      {"tool": "Grep", "input": {"pattern": "..."}, "output": [...]},
      {"tool": "final_answer", "input": {...}, "output": null}
    ]
  },
  {"stage": "validator", ...},
  {"stage": "exploiter", ...}
]
```

The workflow drains transcripts per-unit (via the new
`StageRunner.pop_transcripts_for(unit_id)` method) after
each per-unit chain completes. The MVP attaches the same
drained list to every finding produced for that unit
(per-unit attribution); per-finding attribution is a
follow-up.

The transcript column is NULL on every finding produced
by the prompt runner (PromptStageRunner's
`pop_transcripts_for(...)` is a no-op).

The portal's Per-Unit Verdicts panel surfaces the
finding's `reconciliation` JSONB (alembic 0012) but
not the `agentic_transcript` directly — the transcript
is reserved for backend-only analysis (e.g. the future
real-transport FP-comparison test that compares
prompt-vs-agentic verdicts on a fixture).

## Workflow integration status

The runner + transport + reconciler + prompts +
persistence column ALL ship. Status as of the most
recent release:

- **Fast mode + agentic**: SHIPPED end-to-end. The
  workflow's `_process_unit_single` dispatches through
  `self.stage_runner.run_*(...)`. Setting
  `audit.stages.form: agentic` runs the live audit
  through coder-service's claude install. Per-finding
  transcripts persist on `findings.agentic_transcript`.
- **Run-end reconciliation**: SHIPPED. Every audit run
  (regardless of stages.form or mode) calls
  `self.reconciler.reconcile(findings)`. Path-only
  audits get the structurally complete passthrough
  payload (single per_unit_verdict per finding); future
  multi-AuditUnit audits get real consolidation under
  `AgenticReconciler`.
- **Deep mode + agentic — analyzer & exploiter**:
  SHIPPED. `AnalyzerTeam` and `ExploiterTeam` accept
  the `AgenticStageRunner` and dispatch each replica
  through `stage_runner.run_*(persona=p[i],
  subagent_id=i)`. All replicas hit the same
  coder-service endpoint; per-replica diversity is the
  persona's `extra_system_prefix`, not the model
  (the coder-service container is bound to one
  `ANTHROPIC_MODEL` at `docker run` time — see
  [Multi-model rotation](audit-modes.md#multi-model--multi-provider-rotation-per-stage--form)).
- **Deep mode + agentic — validator**: INTENTIONALLY
  NOT WIRED. `ValidatorTeam` always uses the prompt
  path (`_build_subagent_models` + per-replica
  `ValidatorAgent`) regardless of
  `audit.stages.form`, because the multi-round debate
  protocol is turn-by-turn and doesn't fit
  `/agent_invocations`'s one-shot dispatch model.
  Re-platforming the debate onto an agentic transport
  is a separate change. Practical consequence: when
  `audit.mode: deep` + `audit.stages.form: agentic`,
  the validator stage continues to honour
  `audit.validator.provider_list` and runs prompt-form
  multi-replica + debate, even though analyzer and
  exploiter run agentic.

## Configuration reference

```yaml
audit:
  stages:
    form: prompt              # prompt | agentic

  agentic:
    timeout_seconds: 300      # bounded [10, 1800]
    transport:
      kind: coder_service     # only coder_service supported today
      coder_service:
        endpoint: http://127.0.0.1:8090
        bearer_token_env: ""
        request_timeout_safety_seconds: 60
        project: ""
```

| Env var | Maps to |
|---|---|
| `XAUDITOR_AUDIT_STAGES_FORM` | `audit.stages.form` |
| `XAUDITOR_AUDIT_AGENTIC_TIMEOUT_SECONDS` | `audit.agentic.timeout_seconds` |
| `XAUDITOR_AUDIT_AGENTIC_TRANSPORT_KIND` | `audit.agentic.transport.kind` |
| `XAUDITOR_AUDIT_AGENTIC_TRANSPORT_CODER_SERVICE_ENDPOINT` | `audit.agentic.transport.coder_service.endpoint` |
| `XAUDITOR_AUDIT_AGENTIC_TRANSPORT_CODER_SERVICE_BEARER_TOKEN_ENV` | `audit.agentic.transport.coder_service.bearer_token_env` |
| `XAUDITOR_AUDIT_AGENTIC_TRANSPORT_CODER_SERVICE_REQUEST_TIMEOUT_SAFETY_SECONDS` | `audit.agentic.transport.coder_service.request_timeout_safety_seconds` |
| `XAUDITOR_AUDIT_AGENTIC_TRANSPORT_CODER_SERVICE_PROJECT` | `audit.agentic.transport.coder_service.project` |

## Operational guidance

- **Cost.** An agentic stage call typically spends
  10×–50× the tokens of a prompt stage call (multiple
  tool roundtrips + intermediate tool-output context).
  Budget accordingly: a fast-mode audit with
  `audit.stages.form: agentic` on 100 audit units would
  spend roughly the same tokens as a deep-mode audit
  in prompt form.
- **Single claude install.** `claude` lives in the
  coder-service container only. The xauditor process /
  container does NOT need claude installed — it just
  needs network access to coder-service's
  `/agent_invocations` endpoint. Same install hosts
  the existing Coder verification stage.
- **Sandbox.** The subprocess inherits coder-service's
  scrubbed environment + per-project HOME isolation
  (mirrors the `/verifications` worker). The agent's
  read-file tool is bounded by claude's `--add-dir
  <project_dir>` (the workspace mount inside the
  container).
- **Concurrency budget.** Agentic stage calls AND Coder
  verifications share the same
  `asyncio.Semaphore(max_concurrent_jobs)` in
  coder-service. Operators on deep agentic + heavy
  Coder verification load should tune
  `XAUDITOR_CODER_SERVICE_MAX_CONCURRENT` upward.
- **Transcript volume.** Each finding's
  `agentic_transcript` JSONB grows with the tool-call
  count + per-call output size. Fast-mode runs with the
  default `timeout_seconds: 300` typically produce
  10-50 KB per finding; runs that consistently hit the
  timeout should consider lowering it (most audit
  verdicts converge in 5-15 tool calls / 30-90s).
