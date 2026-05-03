# Coder verification (optional 4th agent)

Both single-agent mode and [teaming mode](teaming.md) can dispatch a
fourth, **repo-global** verification stage — the **coder** — that
reads the entire repository and judges whether each finding holds up
against sibling-module evidence (sanitizers, capability checks,
control-flow guards). The coder runs the [Claude Code
CLI](https://docs.claude.com/en/docs/claude-code) — verified against
the actual CLI surface of claude-code 2.x (env vars + `-p` /
`--effort` flags), not from inference.

The stage is **opt-in** (default disabled) and **asynchronous** to
the per-path pipeline — analyzer / validator / exploiter latency is
unchanged. Verdicts stream into the report sinks per-finding the
moment each verification settles, so the portal flips `coder_status`
chips live during long runs (see [Streaming verdicts](#streaming-verdicts)
below).

## Minimal config

```yaml
coder:
  enabled: true                                  # default false
  transport: http                                 # subprocess | http (default: subprocess)
  concurrency: 4                                  # max parallel Claude Code subprocesses
  thinking_effort: high                           # low | medium | high | xhigh | max
  model_url: https://api.fwb-aiserver.example     # NO trailing /v1 — claude SDK appends /v1/messages itself
  model_name: claude-sonnet-4-7                   # model name your endpoint accepts
  model_api_key: sk-ant-...                       # secret, redacted in logs; baked into container env
  request_timeout_seconds: 1800                   # SIGTERM then SIGKILL after grace
```

> **Note on `model_url`**: Anthropic's SDK convention is for the base
> URL to point at the host, not at `/v1`. The SDK appends
> `/v1/messages` itself. Setting `model_url: https://example/v1`
> produces requests to `https://example/v1/v1/messages` — almost
> always a 404 from the gateway. Drop the trailing `/v1`.

The three model knobs (`model_url`, `model_name`, `model_api_key`)
translate to claude-code's three driving env vars
(`ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL`, `ANTHROPIC_API_KEY`).

- Under `transport: http`, xauditor bakes them into the worker
  container at `xauditor coder init` time. Rotating any of the three
  requires `xauditor coder reset --yes && xauditor coder init` — the
  trade-off for an architecture that doesn't need to plumb every
  value through the HTTP request body.
- Under `transport: subprocess`, xauditor injects them into each
  spawned `claude` subprocess directly.

Either way, secrets are redacted from `repr(CoderConfig)` and
registered with the runtime logger so they never appear in stderr /
`logging.file`.

## Pre-flight

When `coder.enabled: true` and the pre-flight check fails (CLI
missing for subprocess transport, `/health` unreachable for http
transport), `xauditor audit run` and `xauditor audit resume`
fail-fast before any LLM call with an actionable error. The reported
claude version is recorded on the run.

## Streaming verdicts

Each coder verification fires a per-finding callback the moment it
settles, emitting a fresh snapshot to all report sinks (Markdown
artifact + portal Postgres). Operators watching the portal during a
run see findings flip from `Pending` → `Verified` / `Not Verified` /
`Inconclusive` individually as each Claude Code subprocess returns.

The run-end drain blocks for any remaining pending verifications
before declaring `completed`; cancelling (`Ctrl+C`) snaps every
in-flight finding to `Skipped (cancelled by user)` deterministically
and exits within ~2 seconds.

## Verdicts and artifacts

Each run produces an additional `coder-results.md` artifact alongside
the existing per-stage Markdown files. The portal renders a `Coder
Verification` subsection inside each finding card (collapsed by
default) plus a `Coder: <status>` chip on the collapsed-card header
when the verdict is non-`Skipped`.

| Verdict | Meaning | Operator action |
|---|---|---|
| `Verified` | claude confirmed the finding | review |
| `Not Verified` | claude rejected the finding (sanitizer / capability check / unreachable in practice) | confirm + close |
| `Inconclusive` | claude deliberated and was unsure | add context, re-run, or escalate |
| `Skipped` | run disabled or cancelled | n/a |
| `Pending` | in-flight | wait |
| `Fail` | dispatch never reached claude (network, HTTP error, timeout, parse error, subprocess crash, `project not found`) | fix the underlying issue, then `xauditor audit resume` |

The portal's run-detail "LLM providers" header reads e.g.
`coder: claude-code 2.1.123, claude-sonnet-4-7 · auditor: ... · validator: ...`.

### `Fail` is distinct from `Inconclusive`

`Fail` is distinct from `Inconclusive` so reviewers can grep for
"things that need a retry" vs "things claude actually answered".
Every `Fail` outcome emits exactly one structured ERROR-level
audit-log entry:

```
event=coder.transport_failure run_id=<id> finding_id=<id>
project=<name|-> endpoint=<url|-> error_kind=<kind> error_detail=<short>
```

`error_kind` is one of `connection_refused`, `http_4xx`, `http_5xx`,
`timeout`, `parse_error`, `subprocess_exit`, `subprocess_crashed`,
`auth`, `project_not_found`, `idempotency_conflict`. Operators can
grep / forward to centralised logging without scraping `coder_reason`
text.

## Two transports: subprocess (default) and HTTP microservice

The minimal config block above describes the **`subprocess`
transport** — xauditor spawns `claude` directly on the host, one
subprocess per finding. It needs Claude Code installed on the host,
has zero external moving parts, and is the right choice for audits
run on a developer machine.

The **`http` transport** points xauditor at a long-lived
containerised microservice (`xauditor-coder-service`) that holds a
warm pool of Claude Code workers. Pick this when:

- Per-finding cold-start of `claude` adds meaningful wall-clock to
  your runs.
- You want a hardened sandbox (read-only rootfs, dropped Linux
  capabilities) around the model's full filesystem access.
- You're targeting k8s or any other production-grade deployment story.

Install the service package alongside xauditor:

```bash
pip install xauditor-coder-service
```

Then bring up the container via the dedicated lifecycle (recommended):

```bash
xauditor coder init        # build image, create container, start, wait /health
xauditor coder status      # image / container / endpoint / live /health
xauditor coder stop        # stop without removing
xauditor coder reset --yes # remove container + image + named volume + socket
```

`xauditor coder init` is also invoked automatically by the umbrella
`xauditor init` when `coder.enabled: true` and `coder.transport: http`
against a local endpoint, alongside `graphdb init` and `reportdb init`.

**What the lifecycle bakes into the container:**

- `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` env
  vars (from `coder.model_api_key`, `model_url`, `model_name`).
- A read-only rootfs with `/tmp` as tmpfs and a docker named volume
  at `/home/coder` (so claude can write its session state without
  RAM-leaking into the host or losing it across container restarts).
  Per-project subdirectories under `/home/coder/<project>/` isolate
  session caches across concurrent verifications targeting different
  projects.
- A read-only bind-mount of `coder.workspace_root` at `/workspace`.
  Subdirectories of `workspace_root` at any depth ARE the audit-able
  projects — see [Multi-project workspace](#multi-project-workspace) below.
- `--cap-drop ALL --security-opt no-new-privileges`.

### HTTP transport configuration

**Minimum config** (local sidecar — pairs with `coder init`):

```yaml
coder:
  enabled: true
  transport: http
  # endpoint defaults to unix://<runtime.root_dir>/coder.sock
  # — `xauditor coder init` brings up the matching container.
```

**Full config** (override defaults — typical for remote / k8s):

```yaml
coder:
  enabled: true
  transport: http
  endpoint: https://coder-pool.internal/    # default: unix://<runtime.root_dir>/coder.sock
  enable_auth: true                         # explicit on/off; default false
  endpoint_token: sk-coder-...              # required iff enable_auth: true
  poll_interval_seconds: 1.0                # GET /verifications/{id} cadence
  preflight_timeout_seconds: 5              # /health probe budget
```

Pre-flight under `transport: http` probes `GET <endpoint>/health`
instead of resolving a CLI binary. Connection refused / non-200 /
timeout fails the run with a clear `PreflightError`. On success
xauditor emits one `INFO` log line of the form `Coder transport: http
via <endpoint> (auth: enabled|disabled)`. The token value never
appears in the line.

Lifecycle commands refuse on a remote endpoint with a clear error —
remote container lifecycle is a deployment-tier concern. `xauditor
coder status` is the exception: it probes `/health` on remote
endpoints too, just doesn't try to manage docker.

If you'd rather drive raw docker, the shipped compose file works:

```bash
docker compose -f deploy/coder-service/docker-compose.coder.yml up -d
```

See [`deploy/coder-service/README.md`](../deploy/coder-service/README.md)
for the local-sidecar walkthrough, the docker-compose unix-socket
variant, and a k8s deployment sketch.

## Multi-project workspace

Since xauditor 0.11.0 a single `coder` container can verify findings
for many repos. Set `coder.workspace_root` to a host directory whose
subdirectories ARE the audit-able projects. Adding a new project is
`mkdir <workspace_root>/<name> && git clone …` on the host — no
`xauditor coder` lifecycle command, no container restart.

```yaml
coder:
  enabled: true
  transport: http
  workspace_root: /home/user/coder-workspace   # host dir containing project subdirs
  # project_name: secmind                       # optional override; default = basename(realpath(audit.repo_root))
```

### Nested project layouts

Project names may be slash-separated paths (e.g. `team/repo`,
`org/team/sub/repo`). The discovery walk descends the workspace tree
without a depth cap, applying these rules:

- **Every directory at every depth is a project**, identified by its
  `/`-joined path relative to `workspace_root`. No marker file is
  required at any depth. Operators choose which path to set
  `coder.project_name` to.
- **Dotfile-prefixed and `__`-prefixed names are filtered at every
  depth** (so `.git/`, `__pycache__/`, `.venv/` never appear).
- **Symlinks are followed once via `os.path.realpath`;** symlink loops
  are detected and pruned silently.
- **The walk is capped at 10 000 directory inspections per call AND
  3.0 seconds of wall-clock time** — pathological trees truncate with
  the `WalkInfo.truncated` flag set; operators split workspaces or
  prune subtrees that trigger truncation.

Example: a layout

```
coder-workspace/
  legacy/                 # depth-1 project: legacy
  team/                   # depth-1 project: team
    repo/                 # depth-2 project: team/repo
    notes/                # depth-2 project: team/notes
  org/                    # depth-1 project: org
    team/                 # depth-2 project: org/team
      sub/                # depth-3 project: org/team/sub
        repo/             # depth-4 project: org/team/sub/repo
```

surfaces in `GET /projects` as
`["legacy", "org", "org/team", "org/team/sub", "org/team/sub/repo", "team", "team/notes", "team/repo"]`.

Noisy build dirs (`node_modules/`, `dist/`, `target/`, `vendor/`)
will appear in the listing because they are plain folders. If your
workspace produces an unmanageable list, prune those subtrees on the
host or split the workspace into a smaller curated tree.

Project names travel only in JSON request bodies (never as URL path
segments), so the `/` separator is unambiguous and needs no encoding.

xauditor derives the project name from `repo_root` relative to
`coder.workspace_root` whenever both resolve cleanly: e.g.
`workspace_root=/home/user/coder-workspace` plus
`audit.repo_root=/home/user/coder-workspace/team/repo` yields
`project=team/repo` automatically. The relative-path inference falls
back to `basename(realpath(repo_root))` only when the two paths don't
overlap (legacy single-repo containers, mis-configured layouts).
Override via `coder.project_name` only for genuine edge cases
(worktrees pointing outside the workspace, symlinks crossing the
workspace boundary).

Pre-flight probes the service's `GET /projects` endpoint and aborts
the run with an actionable error before the first verification
dispatch when the resolved project name does not exist:

```
Coder service has no project named "newproj". Available projects:
  [secmind, othertool]. The project name is derived from `repo_root`
  relative to `coder.workspace_root` (or `basename(repo_root)` when
  the two don't overlap). Either point `audit.repo_root` at a
  subdirectory under `coder.workspace_root`
  (/home/user/coder-workspace), set `coder.project_name` in
  xauditor.yml to override the derivation, or `mkdir
  /home/user/coder-workspace/newproj` on the host where the coder
  container runs.
```

Concurrent verifications across different projects do not share
claude session state — each invocation runs with
`HOME=/home/coder/<project>/` scoped under the named
`xauditor-coder-home` volume (nested project names map to nested
`HOME` paths under the same volume; intermediate directories are
created with `mkdir -p` mode `0700` on first use). The volume persists
across `coder stop` / `start`; `coder reset --yes` wipes it.

`xauditor coder status` lists the discovered projects (sourced from
the live `GET /projects` when reachable, with a host-side fallback
annotated as `(host scan; container unreachable)` when the service is
down):

```
Coder transport: http
Coder endpoint: http://127.0.0.1:8090 (kind=loopback)
  container: running (name=xauditor-coder-service)
  image: present (tag=xauditor-coder-service:local)
  health: HTTP 200 (claude_cli_version='2.1.123', in_flight=0)
  workspace: /home/user/coder-workspace
  projects: [legacy, team, team/repo]
```

> **Deprecation**: `coder.repo_mount_path` and
> `XAUDITOR_CODER_REPO_MOUNT_PATH` are deprecated in 0.11.0. Configs
> using only the legacy field continue to work via a one-release shim
> (one-time WARN; `effective_workspace_root = parent(repo_mount_path)`,
> `effective_project_name = basename(repo_mount_path)`). Switch to
> `coder.workspace_root` + (optional) `coder.project_name` before the
> version after next, when the shim is removed.

## Future direction

A queue-driven transport (`coder.transport: queue`, postgres-backed
jobs) is proposed in `openspec/changes/add-coder-queue-transport/`
for fully decoupled multi-host deployments. Not implemented yet.

## Environment overrides

| Env var | Maps to |
|---|---|
| `XAUDITOR_CODER_ENABLED` | `coder.enabled` |
| `XAUDITOR_CODER_TRANSPORT` | `coder.transport` |
| `XAUDITOR_CODER_CLI_COMMAND` | `coder.cli_command` (subprocess only) |
| `XAUDITOR_CODER_CONCURRENCY` | `coder.concurrency` |
| `XAUDITOR_CODER_THINKING_EFFORT` | `coder.thinking_effort` |
| `XAUDITOR_CODER_MODEL_URL` | `coder.model_url` |
| `XAUDITOR_CODER_MODEL_NAME` | `coder.model_name` |
| `XAUDITOR_CODER_MODEL_API_KEY` | `coder.model_api_key` |
| `XAUDITOR_CODER_REQUEST_TIMEOUT_SECONDS` | `coder.request_timeout_seconds` |
| `XAUDITOR_CODER_ENDPOINT` | `coder.endpoint` |
| `XAUDITOR_CODER_ENABLE_AUTH` | `coder.enable_auth` |
| `XAUDITOR_CODER_ENDPOINT_TOKEN` | `coder.endpoint_token` |
| `XAUDITOR_CODER_POLL_INTERVAL_SECONDS` | `coder.poll_interval_seconds` |
| `XAUDITOR_CODER_PREFLIGHT_TIMEOUT_SECONDS` | `coder.preflight_timeout_seconds` |
| `XAUDITOR_CODER_CONTAINER_IMAGE` | container image tag (http transport) |
| `XAUDITOR_CODER_CONTAINER_NAME` | container name (http transport) |
| `XAUDITOR_CODER_RUNTIME_SOCKET_PATH` | unix socket path (http transport) |
| `XAUDITOR_CODER_REPO_MOUNT_PATH` | **deprecated** — use `coder.workspace_root` + `coder.project_name` |

An empty-string env value is treated as "unset" so it does not
override a configured value.

## See also

- [Configuration reference](configuration.md) — full xauditor.yml
  schema and all env vars
- [Teaming mode](teaming.md) — pairs naturally with coder
  verification for analyst-grade rigor
- [`deploy/coder-service/README.md`](../deploy/coder-service/README.md)
  — sidecar / compose / k8s deployment walkthrough
