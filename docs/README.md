# xauditor documentation

Topical reference for xauditor. See the [project README](../README.md)
for the elevator pitch, quickstart, and feature highlights.

## Getting started

- [**Quickstart tutorial**](quickstart-tutorial.md) — first-time
  walkthrough from `pip install` to seeing your first finding settle
  in the portal.

## Reference

- [**Configuration**](configuration.md) — full `xauditor.yml` schema,
  environment variables, sampling parameters, and the LLM workflow
  contract.
- [**Providers**](providers.md) — `kind: openai` (default) vs `kind:
  anthropic` declaration, sampling-field mapping, and per-agent
  provider mixing.
- [**CLI reference**](cli.md) — every command (`init`, lifecycle
  verbs, `graph build`, `audit run` / `resume` / `export`, `reset`).

## Features

- [**Coder verification**](coder.md) — the optional 4th agent that
  re-reads the whole repository against each finding. Subprocess vs
  HTTP transport, multi-project workspace, verdict semantics.
- [**Audit modes (`fast` / `deep`)**](audit-modes.md) — pick the
  preset (or override per-stage replication and validator-debate
  knobs directly).
- [**Audit parallelism**](audit-parallelism.md) — `audit.worker_count`,
  inline vs subprocess pool, memory model, and resilience model.
- [**Portal**](portal.md) — FastAPI + Next.js triage UI, login,
  feedback capture, run drill-in, and yml-over-DB precedence.
- [**Reports**](reports.md) — JSON envelope (`format_version: "1"`)
  and Markdown bundle file list.

## Deployment

- [`deploy/README.md`](../deploy/README.md) — single-VM compose stack
  (Neo4j + Postgres + portal + internal PyPI index +
  analyst-install HTTP server).
- [`deploy/coder-service/README.md`](../deploy/coder-service/README.md)
  — coder microservice sidecar, docker-compose unix-socket variant,
  and k8s deployment sketch.

## Release notes

- [`CHANGELOG.md`](../CHANGELOG.md) — version history including
  breaking-change migration runbooks.

## Examples

- [`docs/examples/xauditor-with-portal.yml`](examples/xauditor-with-portal.yml)
  — annotated full configuration example.
