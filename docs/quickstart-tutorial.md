# Quickstart tutorial

This tutorial walks a first-time user from `pip install` to seeing
the first finding settle in the portal — typically 10–15 minutes,
most of which is waiting on the graph build and the LLM.

The tutorial is repository-agnostic. Run it against any Python, Go,
Java, JavaScript, TypeScript, C, or C++ codebase you can read (see
[supported languages](../README.md#supported-languages) on the
project README). For a first run, pick a small-to-medium repo
(< 100k LOC) to keep wall-clock manageable.

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.12+ | `python3 --version` must report 3.12 or newer. |
| Docker | Required for the managed Neo4j, PostgreSQL, portal, and (optional) coder containers. `docker info` should succeed without sudo, or run xauditor commands with sudo. |
| ~2 GB free RAM | Neo4j JVM heap (~512 MB) + Postgres + portal + Bolt clients. Multiply per `audit.worker_count` if you raise it later. |
| An LLM API key | OpenAI-compatible endpoint by default. Anthropic native API is also supported — see [Providers](providers.md). |
| A target repository | Any local checkout of a supported language. The tutorial uses `cd` into the repo as step 4. |

## 2. Install

```bash
pip install xauditor xauditor-portal xauditor-coder-service
```

This installs the `xauditor` console entry point and brings in the
portal package (required at runtime starting in 0.5.0) and the
optional coder service (skip the third package if you don't plan to
enable [coder verification](coder.md) at all).

For an isolated venv:

```bash
python3 -m venv .venv && .venv/bin/pip install xauditor xauditor-portal xauditor-coder-service
source .venv/bin/activate
```

## 3. Configure your LLM provider

Create a minimal `xauditor.yml` in the repo you want to audit:

```yaml
llm:
  default_provider: openai
  providers:
    openai:
      base_url: https://api.openai.com/v1
      api_key: sk-...
      model_name: gpt-4o
```

This is the smallest config that lets `xauditor audit run` succeed.
For per-agent overrides, sampling parameters, vLLM endpoints, or
Anthropic native API, see [Configuration](configuration.md) and
[Providers](providers.md).

> Place this file in the repo root. xauditor also reads
> `~/.xauditor/xauditor.yml` as a user-default fallback and accepts
> environment overrides — see [Configuration → precedence](configuration.md).

## 4. Bring up the managed runtime

From inside the repo you want to audit:

```bash
cd /path/to/your/repo
xauditor init
```

`xauditor init` builds and starts:

- **Neo4j** (graph storage)
- **PostgreSQL** (audit run persistence) + alembic migrations
- **Portal** (FastAPI backend + Next.js frontend)
- **Coder** (only when `coder.enabled: true` and
  `coder.transport: http` against a local endpoint)

Healthchecks must pass before the command returns. Total wall-clock:
~30–90 seconds on first run (image pulls + container creation), ~10
seconds on subsequent runs.

Check status anytime:

```bash
xauditor graphdb status
xauditor reportdb status
xauditor portal status
```

## 5. Run your first audit

```bash
xauditor audit run
```

This runs in two phases:

1. **Graph build** — parses every supported source file, builds the
   call graph, and writes a build into Neo4j. First-time builds on a
   ~50k-LOC repo typically take 30 seconds to a few minutes. Cached
   builds for unchanged repos return in seconds.
2. **Audit** — enumerates audit paths from entry points, runs the
   per-path Analyzer → Validator → Exploiter chain through your
   configured LLM, and streams findings into PostgreSQL as they
   settle. Expect 30 seconds to 2 minutes per path depending on
   model speed; small repos may have a few paths, large ones
   hundreds.

The CLI prints the run id when it starts:

```
Audit completed for <fingerprint> (run id: 20260429-010203).
Export findings via `xauditor audit export 20260429-010203`.
```

Note the run id — you'll use it for export and resume.

## 6. Open the portal

```bash
xdg-open http://localhost:8080      # `open` on macOS
```

Default credentials:

- Username: `admin`
- Password: `admin`
- Role: `admin` (full administrative — can create users, change roles)

You will be forced to rotate the password on first login. See
[Roles and access control](portal.md#roles-and-access-control) for
the three-role permission matrix and how to add `auditor` / `viewer`
accounts from the **Users** tab.

In the portal:

- **Report tab** (left sidebar) — drill into your run, see findings
  organized by entry point and module.
- **Live progress** — if your audit is still in flight, finding
  cards flip from `Pending` → `Verified` / `Not Verified` as each
  validator (and optional [coder](coder.md)) settles.
- **Filtering** — by severity, validator verdict, coder verdict,
  module, file, etc.
- **Drill into a finding** — see the path through the call graph,
  source-backed evidence, exploitation guidance, validator output,
  and (when enabled) coder verdict.

## 7. Record human feedback

For each finding, the portal exposes a `true_positive` /
`false_positive` / `unlabeled` toggle. Reviewer attribution is
captured automatically. Feedback exports as JSONL for downstream RL
post-training pipelines — see [`docs/portal.md`](portal.md) for the
export format.

## 8. Export the run

JSON envelope (canonical, version-pinned for downstream tooling):

```bash
xauditor audit export 20260429-010203                            # to stdout
xauditor audit export 20260429-010203 > run.json                 # to file
```

Markdown bundle (human-friendly):

```bash
xauditor audit export 20260429-010203 --format markdown \
    --output-dir bundle/
```

The Markdown bundle includes `findings.md`, `false-positives.md`,
`coverage-report.md`, and (when coder ran) `coder-results.md`. Full
schema in [Reports](reports.md).

## 9. Resume an interrupted run

If your shell got killed mid-audit, a network blip dropped the LLM,
or you hit `Ctrl+C`, the run lands in `failed` / `cancelled` /
`in_progress`. Resume from the last checkpoint without re-billing
already-completed paths:

```bash
xauditor audit resume                              # most recent resumable run
xauditor audit resume --run-id 20260429-010203     # specific run
```

See [CLI reference → `audit resume`](cli.md#audit-resume) for the
checkpoint semantics.

## What's next

- **Add coder verification.** Pair the per-path agents with a 4th
  agent that re-reads the whole repo for sibling-module evidence —
  see [Coder verification](coder.md).
- **Enable teaming mode.** Run multiple subagents per stage with
  validator debate rounds — see [Teaming mode](teaming.md).
- **Tune throughput.** Raise `audit.worker_count` for parallel
  path execution — see [Audit parallelism](audit-parallelism.md).
- **Self-host the full stack.** Single-VM compose deployment with
  Neo4j + Postgres + portal + internal PyPI index + analyst-install
  HTTP server — see [`deploy/README.md`](../deploy/README.md).

## Cleanup

Stop runtimes without destroying data:

```bash
xauditor portal stop
xauditor reportdb stop
xauditor graphdb stop
```

Or destroy everything (containers, images, volumes — irreversible):

```bash
xauditor portal reset --yes
xauditor reportdb reset --yes
xauditor graphdb reset --yes
```
