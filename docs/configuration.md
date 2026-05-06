# Configuration reference

xauditor reads provider settings, audit parameters, and runtime
options from `~/.xauditor/xauditor.yml`, a project-local
`xauditor.yml` or `.xauditor.yml`, environment variables, or CLI
overrides.

**Configuration precedence:**

```
CLI flags > environment > project file > user default file > built-in defaults
```

## Minimal example

```yaml
graph:
  db:
    password: change-me
  build:
    enable_llm_enrichment: true
llm:
  default_provider: shared
  providers:
    shared:
      base_url: https://your-openai-compatible-endpoint/v1
      api_key: your-api-key
      model_name: your-default-model
      thinking_enabled: false
    graph_specialist:
      base_url: https://your-openai-compatible-endpoint/v1
      api_key: your-graph-api-key
      model_name: your-graph-model
      thinking_enabled: true
agents:
  graph_builder:
    llm:
      provider: graph_specialist
  auditor:
    llm:
      provider: shared
  validator:
    llm:
      provider: shared
runtime:
  root_dir: .xauditor
repository:
  excludes:
    - .venv
    - node_modules
```

For provider declaration syntax (`kind: openai` vs `kind: anthropic`,
extended thinking, sampling fields), see [Providers](providers.md).

## Environment overrides

### LLM + per-agent provider routing

| Env var | Maps to |
|---|---|
| `XAUDITOR_LLM_DEFAULT_PROVIDER` | `llm.default_provider` |
| `XAUDITOR_LLM_BASE_URL` | `llm.providers.<default>.base_url` |
| `XAUDITOR_LLM_API_KEY` | `llm.providers.<default>.api_key` |
| `XAUDITOR_LLM_MODEL_NAME` | `llm.providers.<default>.model_name` |
| `XAUDITOR_LLM_THINKING_ENABLED` | `llm.providers.<default>.thinking_enabled` |
| `XAUDITOR_LLM_REQUEST_TIMEOUT_SECONDS` | `llm.request_timeout_seconds` |
| `XAUDITOR_LLM_PROVIDERS_<PROVIDER>_{TEMPERATURE,TOP_P,TOP_K,REPETITION_PENALTY}` | per-provider sampling field |
| `XAUDITOR_AGENTS_GRAPH_BUILDER_LLM_PROVIDER` | `agents.graph_builder.llm.provider` |
| `XAUDITOR_AGENTS_AUDITOR_LLM_PROVIDER` | `agents.auditor.llm.provider` |
| `XAUDITOR_AGENTS_VALIDATOR_LLM_PROVIDER` | `agents.validator.llm.provider` |
| `XAUDITOR_AGENTS_EXPLOITATION_LLM_PROVIDER` | `agents.exploitation.llm.provider` |
| `XAUDITOR_AGENTS_<AGENT>_LLM_{TEMPERATURE,TOP_P,TOP_K,REPETITION_PENALTY}` | per-agent sampling override |

### Graph build

| Env var | Maps to |
|---|---|
| `XAUDITOR_GRAPH_BUILD_ENABLE_LLM_ENRICHMENT` | `graph.build.enable_llm_enrichment` |
| `XAUDITOR_GRAPH_BUILD_NEO4J_CHUNK_SIZE` | `graph.build.neo4j_chunk_size` (clamped to `[100, 50000]`) |

### Audit pipeline (mode / form / replication / debate / experimental)

See [Audit modes](audit-modes.md) for the prose explanation;
this table is the env-var → dot-path mapping.

| Env var | Maps to |
|---|---|
| `XAUDITOR_AUDIT_MODE` | `audit.mode` (`fast` / `deep`) |
| `XAUDITOR_AUDIT_WORKER_COUNT` | `audit.worker_count` (`[1, 16]`) |
| `XAUDITOR_AUDIT_SHUTDOWN_TIMEOUT_SECONDS` | `audit.shutdown_timeout_seconds` (`[1, 600]`) |
| `XAUDITOR_AUDIT_CODER_SHUTDOWN_TIMEOUT_SECONDS` | `audit.coder.shutdown_timeout_seconds` (must `>= audit.shutdown_timeout_seconds`) |
| `XAUDITOR_AUDIT_MAX_FINDINGS_PER_UNIT` | `audit.max_findings_per_unit` |
| `XAUDITOR_AUDIT_REPLICATION_ANALYZER` | `audit.replication.analyzer` |
| `XAUDITOR_AUDIT_REPLICATION_VALIDATOR` | `audit.replication.validator` |
| `XAUDITOR_AUDIT_REPLICATION_EXPLOITER` | `audit.replication.exploiter` |
| `XAUDITOR_AUDIT_ANALYZER_PROVIDER_LIST` | `audit.analyzer.provider_list` (comma-separated) |
| `XAUDITOR_AUDIT_VALIDATOR_PROVIDER_LIST` | `audit.validator.provider_list` (comma-separated) |
| `XAUDITOR_AUDIT_EXPLOITER_PROVIDER_LIST` | `audit.exploiter.provider_list` (comma-separated) |
| `XAUDITOR_AUDIT_VALIDATOR_DEBATE_ENABLED` | `audit.validator.debate.enabled` |
| `XAUDITOR_AUDIT_VALIDATOR_DEBATE_MAX_ROUNDS` | `audit.validator.debate.max_rounds` |
| `XAUDITOR_AUDIT_VALIDATOR_DEBATE_HALT_ON_CONSENSUS` | `audit.validator.debate.halt_on_consensus` |
| `XAUDITOR_AUDIT_GRAPH_SLICE` | `audit.graph_slice` (default `true`) |
| `XAUDITOR_AUDIT_EXPERIMENTAL_GRAPH_SLICE` | (deprecated) legacy alias for `audit.graph_slice` — emits a `DeprecationWarning`, removed in the next minor release |
| `XAUDITOR_AUDIT_EXPERIMENTAL_UNITS` | `audit.experimental.units` |
| `XAUDITOR_AUDIT_COVERAGE_GAPS_REPORT` | `audit.coverage_gaps.report` |
| `XAUDITOR_AUDIT_SINKS_WELL_KNOWN` | `audit.sinks.well_known` (comma-separated) |
| `XAUDITOR_AUDIT_SINKS_CUSTOM` | `audit.sinks.custom` (comma-separated) |

### Agentic stage form

`audit.stages.form: agentic` routes per-stage agent calls
through `xauditor-coder-service`'s `/agent_invocations`
endpoint instead of direct LLM API calls. See
[Audit modes — Stage form: prompt vs agentic](audit-modes.md#stage-form-prompt-vs-agentic-phase-4)
and [Agentic stage runner](agentic-stage-runner.md) for the
full prose; this table is the env-var → dot-path mapping.

Caveat on per-replica model rotation: under `agentic`, every
replica targets the same coder-service container (one
`ANTHROPIC_MODEL` per container), so `audit.analyzer.provider_list`
and `audit.exploiter.provider_list` are silently ignored — replica
diversity is persona-only. Validator stays prompt-form even
when `stages.form: agentic`, so `audit.validator.provider_list`
is honoured. See
[Multi-model rotation per stage × form](audit-modes.md#multi-model--multi-provider-rotation-per-stage--form).

| Env var | Maps to |
|---|---|
| `XAUDITOR_AUDIT_STAGES_FORM` | `audit.stages.form` (`prompt` / `agentic`) |
| `XAUDITOR_AUDIT_AGENTIC_TIMEOUT_SECONDS` | `audit.agentic.timeout_seconds` |
| `XAUDITOR_AUDIT_AGENTIC_TRANSPORT_KIND` | `audit.agentic.transport.kind` (only `coder_service` accepted today) |
| `XAUDITOR_AUDIT_AGENTIC_TRANSPORT_CODER_SERVICE_ENDPOINT` | `audit.agentic.transport.coder_service.endpoint` |
| `XAUDITOR_AUDIT_AGENTIC_TRANSPORT_CODER_SERVICE_BEARER_TOKEN_ENV` | `audit.agentic.transport.coder_service.bearer_token_env` |
| `XAUDITOR_AUDIT_AGENTIC_TRANSPORT_CODER_SERVICE_REQUEST_TIMEOUT_SAFETY_SECONDS` | `audit.agentic.transport.coder_service.request_timeout_safety_seconds` |
| `XAUDITOR_AUDIT_AGENTIC_TRANSPORT_CODER_SERVICE_PROJECT` | `audit.agentic.transport.coder_service.project` |

### Storage (graphdb / reportdb) + runtime

| Env var | Maps to |
|---|---|
| `XAUDITOR_RUNTIME_ROOT_DIR` | `runtime.root_dir` |
| `XAUDITOR_REPOSITORY_EXCLUDES` | `repository.excludes` (comma-separated) |
| `XAUDITOR_LOGGING_LEVEL` | `logging.level` |
| `XAUDITOR_LOGGING_FILE` | `logging.file` |
| `XAUDITOR_GRAPHDB_IMAGE` | graphdb container image |
| `XAUDITOR_GRAPHDB_PASSWORD` | `graph.db.password` |
| `XAUDITOR_GRAPHDB_NETWORK_NAME` | `graph.db.network_name` |
| `XAUDITOR_GRAPHDB_REMOTE_URL` | `graph.db.remote.url` |
| `XAUDITOR_REPORTDB_IMAGE` | reportdb container image |
| `XAUDITOR_REPORTDB_PASSWORD` | reportdb password |
| `XAUDITOR_REPORTDB_PORT` | reportdb host port |
| `XAUDITOR_REPORTDB_DATABASE` | reportdb database name |
| `XAUDITOR_REPORTDB_NETWORK_NAME` | reportdb network name |
| `XAUDITOR_REPORTDB_REMOTE_URL` | `reportdb.remote.url` |

### Coder microservice

| Env var | Maps to |
|---|---|
| `XAUDITOR_CODER_ENABLED` | `coder.enabled` |
| `XAUDITOR_CODER_TRANSPORT` | `coder.transport` (`subprocess` / `http`) |
| `XAUDITOR_CODER_ENDPOINT` | `coder.endpoint` (HTTP transport) |
| `XAUDITOR_CODER_ENDPOINT_TOKEN` | `coder.endpoint_token` |
| `XAUDITOR_CODER_ENABLE_AUTH` | `coder.enable_auth` |
| `XAUDITOR_CODER_CLI_COMMAND` | `coder.cli_command` |
| `XAUDITOR_CODER_CONCURRENCY` | `coder.concurrency` |
| `XAUDITOR_CODER_THINKING_EFFORT` | `coder.thinking_effort` |
| `XAUDITOR_CODER_MODEL_URL` | `coder.model_url` |
| `XAUDITOR_CODER_MODEL_NAME` | `coder.model_name` |
| `XAUDITOR_CODER_MODEL_API_KEY` | `coder.model_api_key` |
| `XAUDITOR_CODER_REQUEST_TIMEOUT_SECONDS` | `coder.request_timeout_seconds` |
| `XAUDITOR_CODER_POLL_INTERVAL_SECONDS` | `coder.poll_interval_seconds` |
| `XAUDITOR_CODER_PREFLIGHT_TIMEOUT_SECONDS` | `coder.preflight_timeout_seconds` |
| `XAUDITOR_CODER_WORKING_DIRECTORY` | `coder.working_directory` |
| `XAUDITOR_CODER_CONTAINER_IMAGE` | `coder.container_image` |
| `XAUDITOR_CODER_CONTAINER_NAME` | `coder.container_name` |
| `XAUDITOR_CODER_RUNTIME_SOCKET_PATH` | `coder.runtime_socket_path` |
| `XAUDITOR_CODER_REPO_MOUNT_PATH` | `coder.repo_mount_path` |
| `XAUDITOR_CODER_WORKSPACE_ROOT` | `coder.workspace_root` |
| `XAUDITOR_CODER_PROJECT_NAME` | `coder.project_name` |

For prose explanations, see [Coder verification](coder.md),
[Audit modes](audit-modes.md), [Audit parallelism](audit-parallelism.md),
and [Agentic stage runner](agentic-stage-runner.md).

### Deprecated (one minor release of compatibility shim)

The `teaming.*` block is supported for one minor release with a
deprecation warning at parse time. Migrate to the `audit.*`
shape above:

| Legacy env var | Migrated to |
|---|---|
| `XAUDITOR_TEAMING_ENABLED` | `XAUDITOR_AUDIT_MODE=deep` (or `fast`) |
| `XAUDITOR_TEAMING_VALIDATOR_DEBATE_ROUNDS` | `XAUDITOR_AUDIT_VALIDATOR_DEBATE_MAX_ROUNDS` (with `_ENABLED=true`) |
| `XAUDITOR_TEAMING_{ANALYZER,VALIDATOR,EXPLOITER}_SUBAGENT_COUNT` | `XAUDITOR_AUDIT_REPLICATION_{ANALYZER,VALIDATOR,EXPLOITER}` |
| `XAUDITOR_TEAMING_{ANALYZER,VALIDATOR,EXPLOITER}_PROVIDER_LIST` | `XAUDITOR_AUDIT_{ANALYZER,VALIDATOR,EXPLOITER}_PROVIDER_LIST` |

`XAUDITOR_AUDIT_PATH_CONCURRENCY` is no longer recognised
(removed in 0.10.0); set `XAUDITOR_AUDIT_WORKER_COUNT`
instead.

## Sampling parameters

Each provider accepts four optional sampling fields: `temperature`,
`top_p`, `top_k`, and `repetition_penalty`. All four are optional and
propagate to the OpenAI-compatible request — `temperature` and
`top_p` go top-level; `top_k` and `repetition_penalty` route into
`extra_body` alongside `enable_thinking`.

**Range rules** — `temperature >= 0`, `0 < top_p <= 1`, `top_k >= 1`
(integer), `repetition_penalty > 0`. A config with out-of-range
values fails at load time with the offending dot-path in the error
message.

> **Release note — `temperature=0` default removed.** Earlier
> releases forced every LangChain-backed call to `temperature=0`.
> That implicit default is gone. Operators who depended on it must
> set `temperature: 0` explicitly on the active provider. `xauditor
> audit` logs a one-time warning at startup when no provider has
> `temperature` configured.

### Per-role overrides

Per-role overrides let you mix deterministic and creative sampling
within a single run. Overrides overlay the provider's sampling values
— fields you don't set inherit from the provider; fields you do set
take precedence.

```yaml
llm:
  default_provider: shared
  providers:
    shared:
      base_url: https://your-openai-compatible-endpoint/v1
      api_key: your-api-key
      model_name: your-default-model
      temperature: 0
      top_p: 1
agents:
  auditor:
    llm:
      temperature: 0.4        # creative analyzer
  validator:
    llm:
      temperature: 0          # deterministic validator
```

### vLLM-style providers

vLLM-style providers that honor `top_k` and `repetition_penalty`:

```yaml
llm:
  default_provider: vllm
  providers:
    vllm:
      base_url: https://your-vllm-endpoint/v1
      api_key: your-api-key
      model_name: qwen3-coder
      temperature: 0.2
      top_p: 0.9
      top_k: 40
      repetition_penalty: 1.05
```

> Note: `api.openai.com` silently ignores `extra_body` entries like
> `top_k` and `repetition_penalty`; they only take effect on
> providers that explicitly accept them (vLLM, some self-hosted
> OpenAI-compatible runtimes).

## LLM workflow contract

All graph-build, enrichment, and audit-time LLM work resolves models
through xauditor's shared LangChain model-construction layer.
Provider selection follows `llm.default_provider` with optional
`agents.graph_builder.llm.provider`, `agents.auditor.llm.provider`,
and `agents.validator.llm.provider` overrides, so workflow code does
not assemble provider-specific HTTP requests on its own.

`graph.build.enable_llm_enrichment` defaults to `true`. When it
remains enabled, `xauditor graph build` persists class, function, and
path context into Neo4j for later reuse. When it is set to `false`,
graph build still persists the structural graph, and `xauditor audit`
synthesizes any missing path business context or trust-boundary
annotations on demand.

## Audit pipeline shape (xauditor.yml)

The `audit.*` block controls audit mode (`fast` / `deep`),
stage form (`prompt` / `agentic`), worker count, replication,
validator debate, experimental flags, and sink labelling. The
[Audit modes](audit-modes.md) doc covers the prose; this is
the YAML shape:

```yaml
audit:
  mode: fast                  # `fast` | `deep`. Default `fast`.
  worker_count: 1             # `[1, 16]`. 1 = inline (single
                              #   thread); >= 2 = LocalSubprocessPool.
  shutdown_timeout_seconds: 30          # graceful-shutdown budget
  coder:
    shutdown_timeout_seconds: 60        # >= shutdown_timeout_seconds
  max_findings_per_unit: 5    # caps the per-unit analyzer loop

  # Per-stage replica counts for deep mode (one persona-distinct
  # call per replica). `1` (default) keeps fast-mode shape.
  replication:
    analyzer: 1
    validator: 1
    exploiter: 1

  # ValidatorTeam multi-round debate (deep-only).
  validator:
    debate:
      enabled: false
      max_rounds: 3
      halt_on_consensus: true

  # Stage-call form: `prompt` (direct LLM API) or `agentic`
  # (route through xauditor-coder-service's
  # /agent_invocations endpoint, which runs claude-code
  # in a containerized workspace with tool grants).
  stages:
    form: prompt              # `prompt` | `agentic`. Default `prompt`.

  # Agentic transport — only consulted when stages.form: agentic.
  agentic:
    timeout_seconds: 600      # per-invocation hard cap
    transport:
      kind: coder_service     # only `coder_service` accepted today
      coder_service:
        endpoint: http://127.0.0.1:8125  # or unix:///path/to/sock
        bearer_token_env: XAUDITOR_AGENTIC_BEARER_TOKEN
        request_timeout_safety_seconds: 30
        project: ""               # leave empty — auto-derived from
                                  #   coder.workspace_root + repo_root
                                  #   (matches the verification coder).
                                  #   Set explicitly only when the
                                  #   coder-service workspace layout
                                  #   diverges from the audit-host
                                  #   directory layout.

  graph_slice: true           # default true. Broadens GraphSlice
                              #   with type_context,
                              #   cross_path_definitions,
                              #   decorator_chain,
                              #   registration_context,
                              #   entry_classification. Set to false
                              #   only to roll back to the legacy
                              #   three-field payload.

  # Experimental flags — see audit-units.md, audit-modes.md.
  experimental:
    units: false              # planner emits sink/entry/state/boundary
                              #   units alongside path units

  coverage_gaps:
    report: true              # write coverage_gaps.json next to findings.json

  # Sink labelling — see Sink labelling section below.
  sinks:
    well_known: []
    custom: []
```

For deep-mode replica seeding (per-persona prompt overlays),
see the `audit.personas` block documented in
[Audit modes — Personas](audit-modes.md#personas-deep-mode-replica-seeding).

## Audit cancellation

Press `Ctrl+C` once to request a graceful shutdown. Xauditor sets a
run-level cancellation event that every long-running wait observes
(per-path worker pool, deep-mode subagent pools, coder dispatcher drain),
then exits with code `130` within the wall-clock budget defined by
`audit.shutdown_timeout_seconds`. Press `Ctrl+C` a second time to
force-exit immediately via `os._exit(130)` — Python finalisers are
bypassed so a stalled subsystem cannot keep the process alive. The
on-disk snapshot writer uses atomic writes (`tempfile + os.rename`),
so a force-exit at any moment leaves the most recent successful
snapshot intact.

```yaml
audit:
  worker_count: 4
  shutdown_timeout_seconds: 30        # default 30, range [1, 600]
  coder:
    shutdown_timeout_seconds: 20      # default 20, range [1, 600]
```

`audit.shutdown_timeout_seconds` is the total budget after the first
SIGINT before xauditor escalates to forced shutdown of every executor
and SIGTERM/SIGKILL of every tracked subprocess.
`audit.coder.shutdown_timeout_seconds` is the inner cap for the coder
dispatcher's drain — keep it strictly less than the run-level budget
so the coder layer always finishes (or is forced) before the outer
deadline. The runtime emits a startup warning if the inner cap is
greater than or equal to the outer one.

Both keys can be overridden from the environment via
`XAUDITOR_AUDIT_SHUTDOWN_TIMEOUT_SECONDS` and
`XAUDITOR_AUDIT_CODER_SHUTDOWN_TIMEOUT_SECONDS`.

## Sink labelling (audit.sinks.*)

`audit.sinks` controls which Function nodes get
`is_well_known_sink: true` at graph-build time, which in turn
gates `SinkAuditUnit` and `BoundaryAuditUnit` enumeration when
`audit.experimental.units: true`.

```yaml
audit:
  sinks:
    well_known: []        # OPTIONAL allow-list. Empty = use the
                          # full built-in default table (no
                          # pruning). Non-empty = only the listed
                          # FQNs are eligible — useful when
                          # operators know their codebase doesn't
                          # call e.g. `pickle.loads` and want to
                          # suppress the noise.
    custom:               # Additive operator-defined sinks NOT
      - myapp.run_shell   # in the default table. Their `sink_kind`
      - myapp.danger.exec # is "" (uncategorized) today.
```

The default table — defined in
`src/xauditor/graph/sink_labelling.py` — covers Python (heavy) +
Go / Java / JavaScript / Rust / C / C++ subsets, mapping FQNs to
sink kinds (`subprocess`, `http_client`, `deserializer`, `sql`,
`eval`, `file_io`, etc.). See [Sink labels](sink-labels.md) for
the full taxonomy.

## Remote databases

Remote PostgreSQL and remote Neo4j are supported via
`reportdb.remote.url` and `graph.db.remote.url` in `xauditor.yml`.
When either remote is configured, `xauditor init` / `reportdb …` /
`graphdb …` skip managed-container lifecycle for that database. See
[`docs/portal.md`](portal.md) for the full override semantics and
remote-endpoint guidance.

## See also

- [Providers](providers.md) — `kind: openai` vs `kind: anthropic`
  declaration, sampling-field mapping
- [Audit modes](audit-modes.md) — `audit.mode` (`fast` / `deep`) +
  `audit.replication.*` + `audit.validator.debate.*` +
  `audit.max_findings_per_unit` config block (legacy `teaming.*`
  is supported for one minor release with deprecation warnings).
  Also covers `audit.stages.form` (`prompt` / `agentic`),
  `audit.coverage_gaps.report`, `audit.graph_slice`,
  and the `audit.personas` deep-mode replica seeding.
- [Agentic stage runner](agentic-stage-runner.md) —
  `audit.agentic.{max_tool_calls,timeout_seconds,tools.*,
  transport.*}` config block; subprocess `claude-code`
  binary requirement; tool-grant + fallback semantics.
- [Coder verification](coder.md) — `coder.*` config block
- [Audit parallelism](audit-parallelism.md) — `audit.worker_count`
- [Sink labels](sink-labels.md) — default sink table,
  `sink_kind` taxonomy, `audit.sinks.custom` extension syntax
- [Framework heuristics](framework-heuristics.md) — built-in
  decorator → framework / intent table consumed by
  `EntryAuditUnit` enumeration
- [Audit units](audit-units.md) — six unit kinds, the
  `audit.experimental.units` flag, `unit_kind` dispatch
- [`docs/examples/xauditor-with-portal.yml`](examples/xauditor-with-portal.yml)
  — full annotated example
