# Portal Settings page reference

The **Settings** tab in `xauditor-portal` is a UI editor over the
subset of `xauditor.yml` that is safe to change at runtime without
restarting the audit pipeline. It is backed by the
`config.config_snapshots` PostgreSQL table — every save writes a new
versioned row, and a snapshot is mirrored to
`.xauditor/portal/effective-config.yml` for diffing against the
on-disk yml.

This page is the canonical reference for which keys are editable, how
the source-tagging rules work, and how the secret-handling guarantee
is implemented.

## Resolution order

The portal resolves the effective value for every key in this priority
order, with each level fully shadowing the lower ones:

```
env > xauditor.yml > config.config_snapshots > built-in defaults
```

`env` covers the canonical `XAUDITOR_*` variables (see `ENV_KEY_MAP`
in `src/xauditor/config.py`) and the special
`XAUDITOR_GRAPH_BUILD_NEO4J_CHUNK_SIZE` opt-out path.

Each rendered field carries a coloured **source tag** (`yml` / `env` /
`db` / `default`). Whenever the source is `yml` or `env`, the field is
rendered **read-only** with the original value visible — UI edits are
ignored at the resolver, so the page disables them up front rather than
silently dropping a save.

This is a stricter rule than the legacy `overridden_by_yml` flag. Under
the old behaviour a yml-only field (set in yml, never in DB) would
render empty and editable, and a UI save would shadow yml on the next
load. The new rule keys read-only off the source tag alone, so any
yml-sourced or env-sourced field is locked regardless of whether the DB
also names the key.

## Editable surface

| Section | Key prefix / key | Type | Range |
|---|---|---|---|
| Logging | `logging.level` | enum | `error \| warning \| info \| debug` |
| Logging | `logging.file` | string | — |
| Repository | `repository.excludes` | list of glob | — |
| Graph build | `graph.build.enable_llm_enrichment` | boolean | — |
| Graph build | `graph.build.max_file_bytes` | int | `≥ 1` |
| Graph build | `graph.build.paths_max_depth` | int | `≥ 1` |
| Graph build | `graph.build.paths_max_count` | int | `≥ 1` |
| Graph build | `graph.build.neo4j_chunk_size` | int | `[100, 50000]` |
| Audit | `audit.worker_count` | int | `[1, 16]` |
| Audit mode | `audit.mode` | enum | `fast \| deep` |
| Audit mode | `audit.max_findings_per_unit` | int | `[1, 20]` |
| Audit mode | `audit.replication.<stage>` | int | `[1, 20]` |
| Audit mode | `audit.validator.debate.enabled` | boolean | — |
| Audit mode | `audit.validator.debate.max_rounds` | int | `[1, 20]` |
| Audit mode | `audit.coverage_gaps.report` | boolean | default `true`; suppresses the Coverage Gaps section + portal panel when `false` |
| Audit mode (legacy) | `teaming.<team>.provider_list` | list of provider names | required when `audit.mode` is `deep` |
| Coder | `coder.enabled` | boolean | — |
| Coder | `coder.transport` | enum | `subprocess \| http` |
| Coder | `coder.cli_command` | list of tokens | — |
| Coder | `coder.concurrency` | int | `[1, 64]` |
| Coder | `coder.thinking_effort` | enum or `(unset)` | `low \| medium \| high \| xhigh \| max` |
| Coder | `coder.{model_url, model_name}` | string | — |
| Coder | `coder.request_timeout_seconds` | int | `≥ 1` |
| Coder | `coder.working_directory` | string | — |
| Coder (HTTP only) | `coder.endpoint` | string | — |
| Coder (HTTP only) | `coder.enable_auth` | boolean | — |
| Coder (HTTP only) | `coder.poll_interval_seconds` | float | `> 0` |
| Coder (HTTP only) | `coder.preflight_timeout_seconds` | int | `≥ 1` |
| Coder (container only) | `coder.container_image` | string | — |
| Coder (container only) | `coder.container_name` | string | — |
| Coder (container only) | `coder.runtime_socket_path` | string | — |
| Coder (container only) | `coder.workspace_root` | string | — |
| Coder (container only) | `coder.project_name` | string | — |
| LLM providers | `llm.providers.<name>.base_url` | string | — |
| LLM providers | `llm.providers.<name>.model_name` | string | — |
| LLM providers | `llm.providers.<name>.kind` | enum | `openai \| anthropic` |
| LLM providers | `llm.providers.<name>.thinking_enabled` | boolean | — |
| LLM providers | `llm.providers.<name>.thinking_effort` | enum or `(unset)` | `low \| medium \| high \| xhigh \| max` |
| LLM providers | `llm.providers.<name>.request_timeout_seconds` | float | `> 0` |
| LLM providers | `llm.providers.<name>.max_tokens` | integer or `(unset)` | `> 0`; defaults to 64000 on `kind: anthropic` |
| LLM providers | `llm.providers.<name>.thinking_budget_tokens` | integer or `(unset)` | `> 0`; defaults to 32000 on `kind: anthropic` legacy thinking branch |
| LLM providers | `llm.providers.<name>.{temperature, top_p, top_k, repetition_penalty}` | number | sampling rules |
| Default provider | `llm.default_provider` | enum | one of the configured providers |
| Per-agent overrides | `agents.<agent>.llm.provider` | enum | one of the configured providers |
| Per-agent overrides | `agents.<agent>.llm.thinking_enabled` | boolean | — |
| Per-agent overrides | `agents.<agent>.llm.request_timeout_seconds` | float | `> 0` |
| Per-agent overrides | `agents.<agent>.llm.{temperature, top_p, top_k, repetition_penalty}` | number | sampling rules |

Sampling rules:

- `temperature` ≥ 0
- `top_p` in `(0, 1]`
- `top_k` integer ≥ 1
- `repetition_penalty` > 0

The Coder section's HTTP- and container-only fields are rendered as
**disabled-with-hint**: visible but not editable when the gate
condition isn't met, with a tooltip naming the gate. The deprecated
`coder.repo_mount_path` is *not* rendered — operators should migrate to
`coder.workspace_root` + `coder.project_name`.

## Secret handling

Every secret-shaped key is redacted at the API boundary. A key is
**secret-shaped** when its dotted path:

- ends in `.api_key`, `.password`, `.model_api_key`, or
  `.endpoint_token`, OR
- matches the glob `*.remote.url` (database connection URLs that may
  carry inline credentials).

For each secret-shaped key the `GET /api/config/effective` response
SHALL:

- omit the key from `values` (replacing with `null`),
- omit the key from `fields`,
- emit an entry under a top-level `redacted_keys` map of the shape
  `{ "<key>": { "present": bool, "source": "yml" | "env" | "default" } }`,
  where `present` is `true` iff the underlying value is a non-empty
  string.

The Settings page renders these keys as a read-only "configured" /
"not configured" stub with the source badge. The
`PUT /api/config/snapshot` endpoint additionally rejects any submission
that attempts to set a secret-shaped key with HTTP 400 and a message
referencing the offending dot-path.

A guardrail unit test
(`packages/xauditor-portal/tests/config/test_secret_schema_walk.py`)
walks every dataclass under `XAuditorConfig` and asserts every leaf
field whose name contains `password|api_key|model_api_key|endpoint_token`
is matched by `is_secret_key`. A future refactor that adds a new such
field without updating
`config_resolver.FORBIDDEN_UI_KEY_SUFFIXES` /
`SECRET_VALUE_KEY_PATTERNS` fails this test loudly.

## Page layout

```
┌──────────────────────────────────────────┐
│  Settings header                         │
│  Override banner (yml + env keys)        │
│  About panel (secret guarantee)          │
│  ────────────────────────────────────── │
│  TOC (anchor links + per-section badge)  │
│  ────────────────────────────────────── │
│  ▾ Logging        (open)                 │
│  ▸ Repository                            │
│  ▸ Graph build                           │
│  ▸ Audit                                 │
│  ▸ Teaming                               │
│  ▸ Coder                                 │
│  ▾ LLM providers (open)                  │
│  ▾ Default provider & per-agent (open)   │
│  ▾ Effective configuration preview (open)│
│  ▾ Save snapshot (open)                  │
└──────────────────────────────────────────┘
```

Section headers carry a **count badge** equal to the number of keys
inside that section whose source is `yml` or `env`, so operators can
spot overrides without expanding every section.

## Multi-value fields

`teaming.<team>.provider_list` (legacy), `repository.excludes`, and
`coder.cli_command` are rendered with a single shared `<ChipInput>`
control that:

- adds chips on `Enter`, `,`, or blur;
- removes the trailing chip on `Backspace` when the input is empty;
- splits pasted content on `,` / `\n` / `;`;
- runs an optional per-chip validator and renders failing chips in a
  rose tone with the validator's message in a tooltip;
- offers an autocomplete popover when `suggestions` is provided
  (used by `teaming.<team>.provider_list` to suggest configured
  provider names).
