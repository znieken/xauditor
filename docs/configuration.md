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

| Env var | Maps to |
|---|---|
| `XAUDITOR_LLM_DEFAULT_PROVIDER` | `llm.default_provider` |
| `XAUDITOR_LLM_BASE_URL` | `llm.providers.<default>.base_url` |
| `XAUDITOR_LLM_API_KEY` | `llm.providers.<default>.api_key` |
| `XAUDITOR_LLM_MODEL_NAME` | `llm.providers.<default>.model_name` |
| `XAUDITOR_LLM_THINKING_ENABLED` | `llm.providers.<default>.thinking_enabled` |
| `XAUDITOR_AGENTS_GRAPH_BUILDER_LLM_PROVIDER` | `agents.graph_builder.llm.provider` |
| `XAUDITOR_AGENTS_AUDITOR_LLM_PROVIDER` | `agents.auditor.llm.provider` |
| `XAUDITOR_AGENTS_VALIDATOR_LLM_PROVIDER` | `agents.validator.llm.provider` |
| `XAUDITOR_GRAPH_BUILD_ENABLE_LLM_ENRICHMENT` | `graph.build.enable_llm_enrichment` |
| `XAUDITOR_RUNTIME_ROOT_DIR` | `runtime.root_dir` |
| `XAUDITOR_REPOSITORY_EXCLUDES` | `repository.excludes` (comma-separated) |
| `XAUDITOR_GRAPHDB_IMAGE` | graphdb container image |
| `XAUDITOR_GRAPHDB_PASSWORD` | `graph.db.password` |
| `XAUDITOR_GRAPHDB_REMOTE_URL` | `graph.db.remote.url` |
| `XAUDITOR_REPORTDB_IMAGE` | reportdb container image |
| `XAUDITOR_REPORTDB_PASSWORD` | reportdb password |
| `XAUDITOR_REPORTDB_PORT` | reportdb host port |
| `XAUDITOR_REPORTDB_DATABASE` | reportdb database name |
| `XAUDITOR_REPORTDB_REMOTE_URL` | `reportdb.remote.url` |
| `XAUDITOR_LLM_PROVIDERS_<PROVIDER>_{TEMPERATURE,TOP_P,TOP_K,REPETITION_PENALTY}` | per-provider sampling field |
| `XAUDITOR_AGENTS_<AGENT>_LLM_{TEMPERATURE,TOP_P,TOP_K,REPETITION_PENALTY}` | per-agent sampling override |

For the additional [coder](coder.md), [teaming](teaming.md), and
[audit parallelism](audit-parallelism.md) env vars, see those docs.

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

## Audit cancellation

Press `Ctrl+C` once to request a graceful shutdown. Xauditor sets a
run-level cancellation event that every long-running wait observes
(per-path worker pool, teaming subagent pools, coder dispatcher drain),
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
- [Teaming mode](teaming.md) — `teaming.*` config block
- [Coder verification](coder.md) — `coder.*` config block
- [Audit parallelism](audit-parallelism.md) — `audit.worker_count`
- [`docs/examples/xauditor-with-portal.yml`](examples/xauditor-with-portal.yml)
  — full annotated example
