# xauditor-portal

Optional web portal and PostgreSQL mirror for the [xauditor](../..) CLI.

This package is shipped separately from `xauditor` so users who do not want a
UI keep the lean install. When installed, it activates:

- a SQLAlchemy 2.0 ORM over PostgreSQL mirroring every Markdown audit
  artifact (`report` schema), human feedback annotations (`feedback`), UI
  configuration snapshots (`config`), and authentication (`auth`);
- a FastAPI backend (`xauditor_portal.app:create_app`) serving the
  `/api/{health,auth,runs,findings,feedback,config}` endpoints;
- a Next.js 14 frontend (in `frontend/`) with sidebar tabs (Reports and an
  admin-only Users tab), dark/light theming, login, forced first-login
  password change, and a hierarchical report drill-down with human-feedback
  controls. Admins can soft-disable users (a separate state from delete that
  preserves history) and manually mark or delete audit runs from the Reports
  tab when an automated worker has stalled.

## Reports navigation

The Reports tab is a **four-level** drill-down; every level has a stable,
shareable URL and paginated lists with a user-configurable page size
(`10 / 25 / 50 / 100`, persisted per-list in `localStorage`):

1. `/reports` — **Projects**. One row per project: name, repo path, total
   graph builds, total audit runs, added-at timestamp.
2. `/reports/projects/{project_key}/builds` — **Graph Builds** for the
   project: build fingerprint, build time, run count, last-run status.
3. `/reports/projects/{project_key}/builds/{build_fingerprint}/runs` —
   **Audit Runs** for a specific build: run id (the
   `.xauditor/reports/<timestamp>/` directory name), team / single mode,
   status, progress percent, finding counts, start / end timestamps.
4. `/reports/runs/{run_id}` — **Run detail** with three sub-tabs only:
   `Findings`, `Coverage`, `Audit Log`. The legacy `/reports/{runId}`
   URL redirects here for back-compat.

Lists auto-refresh every 3 s while relevant rows are `in_progress`:
- Run header → always (reuses the existing run poll).
- Graph Build list → when any row's last run is still running.
- Audit Run list → when any row on the page is still running.
- Findings sub-view → when the parent run is still running (preserves
  filter state and expanded-card state on every refresh).
- Coverage sub-view → same, across summary + all three paginated cards.

## Run header feedback overlay

The *Valid findings* and *False positives* cards on the run header render a
net count plus an explicit `base + added − removed = net` breakdown so
reviewers can see both the validator-base count and the deltas introduced
by human labelling:

- `base` — count from the validator's `validation_status`.
- `added_by_feedback` — findings whose validator verdict is in the other
  bucket but whose latest human feedback label puts them in this one
  (for Valid: auto-FP → human `true_positive`; for FP: auto-Valid/…/
  Inconclusive → human `false_positive`).
- `removed_by_feedback` — mirror of `added_by_feedback` — findings the
  validator placed here that human feedback moved out.

Feedback never mutates `Finding` rows; it lives as an overlay in
`feedback.finding_annotations`. Saving a label invalidates the run
query so the header updates within one second even on terminal runs.

## Finding cards

- Each card collapses by default; expanding reveals the full Markdown
  finding body.
- Source snippets group into **one `<details>` per file** — the first file
  is expanded by default, the rest are folded so a card with many files
  stays navigable.
- If a finding has no source references but the run produced Markdown
  snippets for it, the card shows an amber "snippets not mirrored"
  notice pointing to the on-disk artifact (indicates a sink-mirror gap).
- Team-mode runs show a folded **Debate** section inside the expanded
  body with the full per-round validator transcript.

## Coverage sub-view

Coverage is served by four endpoints so very large projects (tens of
thousands of functions) no longer OOM the backend or the browser:

- `GET /api/runs/{id}/coverage/summary` — constant-size totals +
  `by_status` counts + `percent_audited` per category.
- `GET /api/runs/{id}/coverage/modules?limit=&offset=&status=&q=`
- `GET /api/runs/{id}/coverage/files?limit=&offset=&status=&q=`
- `GET /api/runs/{id}/coverage/functions?limit=&offset=&status=&q=`

`limit` is capped server-side at 200. The UI renders three independently-
paginated cards with their own status filter, substring search, and
page-size selector.

The legacy `GET /api/runs/{id}/coverage` endpoint remains for one release
as a thin wrapper that caps each category at 500 rows and emits a
`Deprecation: true` header. New clients should call the paginated
endpoints.

## Settings tab

The Settings tab edits the UI-safe subset of `xauditor.yml` with live
yml-override badges. In addition to logging / graph-build / teaming
sections, it exposes LLM configuration:

- **LLM providers** — one card per named provider under `llm.providers.*`
  with `base_url`, `model_name`, `thinking_enabled`, `temperature`,
  `top_p`, `top_k`, `repetition_penalty`. `api_key` is rendered as a
  masked, disabled input — it is strictly yml-only and any PATCH that
  targets it is rejected with HTTP 400 (`api_key is managed in
  xauditor.yml only`).
- **Default provider & per-agent overrides** — a select for
  `llm.default_provider` plus one sub-card per recognized agent
  (`graph_builder`, `auditor`, `exploitation`, `validator`) with provider
  selector + sampling fields + `thinking_enabled` toggle.
- **Effective configuration preview** — a read-only table rendering one
  row per agent with the resolved `(provider, model, temperature, top_p,
  top_k, repetition_penalty, thinking_enabled)` tuple and a source tag
  (`yml` / `db` / `default`) per cell so operators can see exactly what
  each agent will send.

Sampling-parameter range validation (`temperature ≥ 0`, `0 < top_p ≤ 1`,
`top_k ≥ 1 integer`, `repetition_penalty > 0`) runs both client-side
(gates the Save button) and server-side (rejected with HTTP 400) so
invalid values never reach the database.

## Roles and access control

Every user holds exactly one role from the closed set
`{admin, auditor, viewer}`. The role is enforced by FastAPI dependencies
keyed off the JWT cookie's `role` claim, so non-admin clients that
bypass the UI still receive HTTP 403 on protected endpoints.

| Capability                                    | admin | auditor | viewer |
|-----------------------------------------------|-------|---------|--------|
| Read findings / runs / projects / dashboards  |   ✓   |    ✓    |    ✓   |
| Mark finding feedback / write reviewer notes  |   ✓   |    ✓    |    ✗   |
| Create / update / delete users                |   ✓   |    ✗    |    ✗   |
| Reset another user's password                 |   ✓   |    ✗    |    ✗   |
| Save UI configuration snapshots (`PUT /api/config/snapshot`) |   ✓   |    ✗    |    ✗   |

Role flips invalidate the affected user's existing JWT cookies — the
auth dep rejects any token issued before the user's
`sessions_invalid_before` timestamp, so a re-login is required to pick
up the new role. Admin-initiated password resets re-arm the
`must_change_password` flag and force the same flow.

The **Users** tab in the sidebar is visible only to admins. It exposes
create-user, change-role, reset-password, and delete-user actions.
Every action is admin-gated at the API; the UI also disables the
"Demote-from-admin" and "Delete" controls on the row representing the
only remaining admin so the system cannot lose its last administrator.

### Default seed

A fresh install seeds a single user `admin / admin` with role `admin`
and `must_change_password=true`. Operators upgrading from earlier
releases (which seeded `auditor / auditor`) are auto-promoted to role
`admin` by the `0008_users_role` Alembic migration so no operator gets
locked out of their own portal.

## Containers

The portal runs as two containers joined by a docker bridge network
(`xauditor-portal-net`) managed by the main `xauditor` CLI:

- `xauditor-portal-backend`: Python + FastAPI, port `8000` on the internal
  network only.
- `xauditor-portal-frontend`: Node + Next.js standalone server, port `3000`
  internally, published to the host (default `8080`).

## Install

```shell
pip install xauditor-portal
```

## Bring everything up

```shell
# Create both DB containers (managed Neo4j + managed PostgreSQL).
xauditor init

# Build portal images (both) and start the two portal containers.
xauditor portal start

# Open http://localhost:8080 — default credentials admin / admin,
# the first login forces a password change.
```

## Ops verbs

```shell
xauditor reportdb init | start | stop | reset --yes
xauditor portal   build [--service backend|frontend] | start | stop | reset --yes | status
xauditor-portal   migrate | seed | serve      # run inside the backend container / dev shell
```

## Layout

```
packages/xauditor-portal/
├── alembic.ini               # shared Alembic config; schema-scoped migrations under src/.../db/migrations/versions/
└── src/xauditor_portal/
    ├── app.py                # FastAPI factory
    ├── cli.py                # `xauditor-portal` helper CLI
    ├── api/                  # routers (auth, runs, findings, feedback, config, health)
    ├── auth/                 # JWT + bcrypt
    ├── db/                   # SQLAlchemy engine, models, Alembic migrations
    ├── docker/
    │   ├── backend.Dockerfile   # python:3.12-slim runtime
    │   └── requirements.txt     # runtime deps (mirrors pyproject)
    ├── frontend/                # Next.js 14 standalone app (own Dockerfile inside)
    ├── sinks/                # PostgresReportSink — read by the main xauditor package
    └── config_resolver.py    # yml-over-DB merge + "overridden" tagging

The Dockerfiles and the frontend source tree ship **inside** the Python
package so `pip install xauditor-portal` carries the build contexts, and
`xauditor portal start` can locate them via `importlib.resources`.
```

## Development

```shell
# Python side
cd packages/xauditor-portal
pip install -e .
xauditor-portal migrate       # (Phase 5)
xauditor-portal serve --port 8000

# Frontend side
cd frontend
npm install
npm run dev                   # http://localhost:3000
```

When developing against a running backend container, set
`BACKEND_INTERNAL_URL` on the frontend so `/api/*` proxies to the right
place (default `http://xauditor-portal-backend:8000` for the managed
network).

## Remote databases

`xauditor.yml` controls whether the report database is a managed container
or a remote endpoint. Remote connection info lives **only in yml** and is
never editable from the portal UI:

```yaml
reportdb:
  remote:
    url: postgresql+asyncpg://user:pass@host:5432/xauditor

graph:
  db:
    remote:
      url: neo4j+s://graph.example.com:7687
```

When either `*.remote.url` is set, `xauditor init` skips the managed
container init for that database and connects to the remote endpoint.
