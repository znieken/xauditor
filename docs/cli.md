# CLI reference

`pip install xauditor` ships an `xauditor` console script on your
`PATH`. All commands below use it directly; equivalents with
`python3 -m xauditor …` work the same way for environments where the
console script is unavailable.

## Lifecycle commands

xauditor manages four runtimes — all sharing the same six lifecycle
verbs (`build` where applicable, `init`, `start`, `stop`, `reset --yes`,
`status`):

| Runtime | Purpose | Command prefix |
|---|---|---|
| `graphdb` | Neo4j (graph storage) | `xauditor graphdb …` |
| `reportdb` | PostgreSQL (audit run persistence) | `xauditor reportdb …` |
| `portal` | FastAPI backend + Next.js frontend | `xauditor portal …` |
| `coder` | Claude Code worker container | `xauditor coder …` |

### Umbrella initialization

Initialize the full managed runtime in one shot (Neo4j + PostgreSQL +
portal + coder when configured):

```bash
xauditor init
```

This runs `graphdb init`, `reportdb init` (with migrations), `portal
init`, and (when `coder.enabled: true` with `coder.transport: http`
against a local endpoint) `coder init`.

### Per-runtime initialization

```bash
xauditor graphdb init        # Neo4j
xauditor reportdb init       # PostgreSQL + alembic migrations
xauditor portal init         # FastAPI backend + Next.js frontend
xauditor coder init          # Claude Code worker (when configured)
```

### Lifecycle verbs

Every managed runtime supports the same six verbs:

```bash
xauditor <runtime> build [--service backend|frontend]   # rebuild images (where applicable)
xauditor <runtime> init                                 # build + create + start + healthcheck
xauditor <runtime> start                                # restart existing containers
xauditor <runtime> stop                                 # stop without removing
xauditor <runtime> reset --yes                          # destroy containers + images + volumes
xauditor <runtime> status                               # show state
```

`xauditor portal start` is idempotent: it ensures the network, builds
any missing image (only if `xauditor-portal` is installed), starts
the backend, probes `/api/health` via `docker exec`, then starts the
frontend and probes its proxied `/api/health` from the host.

`xauditor portal reset --yes` removes only portal resources. Your
managed `graphdb` / `reportdb` containers, images, and volumes are
untouched.

## Graph commands

Build a repository graph from the current directory:

```bash
xauditor graph build --exclude tests --exclude vendor
```

`--exclude` patterns are matched against directory names during the
streaming walker. Defaults come from `repository.excludes` in
`xauditor.yml` (see [Configuration reference](configuration.md)).

## Audit commands

### `audit run`

Run a fresh audit against the current directory:

```bash
xauditor audit run
# → Audit completed for <fingerprint> (run id: 20260429-010203).
#   Export findings via `xauditor audit export 20260429-010203`.
```

### `audit resume`

Resume an interrupted audit. With no flags, the most recent failed /
cancelled / in-progress run is selected:

```bash
xauditor audit resume
```

Or target a specific run by its run id (the `YYYYMMDD-HHMMSS` string
the CLI printed when the run started):

```bash
xauditor audit resume --run-id 20260419-083512
```

Resume reads the per-path checkpoint from `audit_runs.resume_state
JSONB` in PostgreSQL — already-completed paths are not re-executed
or re-billed through the LLM. The run's `status` column flips from
`failed` / `cancelled` back to `in_progress` and lands on the new
terminal outcome on the same row, so the portal stays continuous
across the interruption.

Runs started on xauditor 0.4.x have no in-DB resume state and cannot
be resumed on 0.5.0+; the resume command surfaces a clear "started
on a previous version, please re-run from scratch" error.

> The bare `xauditor audit [--build FP]` form is accepted for one
> release via a deprecation shim that dispatches to `audit run`. A
> warning is printed to stderr; update scripts to the sub-verb form
> before the shim is removed.

### `audit export`

Render a persisted run from PostgreSQL to JSON (canonical) or
Markdown (operator-friendly):

```bash
xauditor audit export 20260429-010203                                 # JSON envelope to stdout
xauditor audit export 20260429-010203 --format markdown \
    --output-dir bundle/                                              # Markdown bundle
xauditor audit export 20260429-010203 --include-debug                 # add cli_exit_code / cli_stderr fields
```

See [Reports](reports.md) for the JSON envelope schema and the
Markdown bundle file list.

## Reset commands

Reset managed database resources (destroys data — the `--yes` flag
is required):

```bash
xauditor graphdb reset --yes
xauditor reportdb reset --yes
xauditor portal reset --yes
xauditor coder reset --yes
```

## Configuration precedence

`CLI flags > environment > project file > user default file >
built-in defaults`

See [Configuration reference](configuration.md) for the full list of
config knobs and env vars.

## See also

- [Configuration reference](configuration.md)
- [Audit parallelism](audit-parallelism.md) — `audit.worker_count`
  and resilience model
- [Coder verification](coder.md) — coder lifecycle and config
- [Portal](portal.md) — portal-specific lifecycle and login
