# xauditor portal

The portal is an **optional** web UI layered on top of the xauditor audit
runtime. It is shipped as a separate Python package
(`xauditor-portal`) so `xauditor` itself stays lean.

## What it adds

- A FastAPI backend that serves `/api/{auth,runs,findings,feedback,config,users,health}`.
- A Next.js 14 (App Router, standalone output) frontend with a left-sidebar
  **Report** tab (hierarchical run drill-down, live progress, filterable
  findings), **Settings** tab, and an admin-only **Users** tab.
- A PostgreSQL mirror of every Markdown artifact. Markdown generation is
  preserved verbatim; the database is additive.
- Human feedback annotations on every finding (`true_positive` /
  `false_positive` / `unlabeled`) with reviewer attribution and a JSONL
  export for reinforcement-learning post-training pipelines.
- Login with a default `admin / admin` account, forced password
  rotation on first login, bcrypt hashing, HttpOnly SameSite=Lax JWT
  cookies (12h TTL, with session-revoke on password change, role
  change, or admin-initiated reset).
- Three-role RBAC (`admin` / `auditor` / `viewer`) enforced at every
  write endpoint. See [Roles and access control](#roles-and-access-control).

## Architecture

Two managed containers on a dedicated docker bridge network
`xauditor-portal-net`:

| Service  | Image                                  | Port (internal) | Port (host)             |
|----------|----------------------------------------|-----------------|-------------------------|
| backend  | `xauditor-portal-backend:local`        | 8000            | not published (debug-only flag) |
| frontend | `xauditor-portal-frontend:local`       | 3000            | 8080 (configurable)     |

The frontend proxies `/api/*` to `http://xauditor-portal-backend:8000`
over the network. Browsers only ever talk to the frontend origin, so
same-origin cookies stay intact.

Backend image is Python 3.12-slim + FastAPI + SQLAlchemy async + asyncpg.
Frontend image is node 20-slim carrying only the Next.js standalone
bundle — no dev deps, no build tooling.

## Commands

All commands live under the main `xauditor` CLI so you never have to
`docker run` the images manually.

```bash
# One-shot: bring up both Neo4j and PostgreSQL.
xauditor init

# Report database only.
xauditor reportdb init | start | stop | reset --yes

# Portal runtime.
xauditor portal init
xauditor portal start
xauditor portal stop
xauditor portal reset --yes
xauditor portal status
```

`xauditor portal init` is the first-time setup verb: it ensures the
network, builds both images from the Dockerfiles bundled with
`xauditor-portal`, creates the containers, starts them, and probes
`/api/health` end to end. Re-runs are idempotent — existing images and
containers are reused. After the first `init`, lifecycle commands
(`start`, `stop`, `reset`, `status`) are sufficient.

`xauditor portal reset --yes` removes only portal resources. Your
managed `graphdb` / `reportdb` containers, images, and volumes are
untouched.

## Settings tab — env/yml-over-DB precedence

The portal stores UI-edited configuration in the `config` PostgreSQL
schema (append-only versioned snapshots). At every page load the
backend merges sources in this order:

1. `XAUDITOR_*` environment variables (canonical map plus the
   `XAUDITOR_GRAPH_BUILD_NEO4J_CHUNK_SIZE` opt-out path).
2. The effective `xauditor.yml` (the same loader `xauditor` itself
   uses — project file, user default, CLI overrides).
3. The latest `config_snapshots` row.
4. Built-in defaults.

Each field is tagged with `source ∈ {env, yml, db, default}`. Fields
whose source is `env` or `yml` are rendered **read-only** with the
original value visible — UI edits would be silently shadowed otherwise.
Each read-only field shows a coloured override badge (yellow for yml,
blue for env) with a tooltip naming the yml key / env var, and a banner
at the top of the Settings tab lists every read-only key for at-a-glance
visibility. Each section header also carries a count badge equal to the
number of yml-/env-sourced keys inside that section.

The Settings page IA is a TOC with collapsible cards: Logging, LLM
providers, default-provider & per-agent overrides, the effective-config
preview, and Save snapshot are open by default; Repository, Graph
build, Audit, Teaming, and Coder are collapsed (each still announces
override counts on its summary).

### What's editable from the UI

- `logging.level`, `logging.file`
- `repository.excludes` (chip input)
- `graph.build.{enable_llm_enrichment, max_file_bytes, paths_max_depth, paths_max_count, neo4j_chunk_size}`
- `audit.worker_count`
- `audit.mode` (`fast` / `deep`), `audit.replication.{analyzer,validator,exploiter}`,
  `audit.validator.debate.{enabled,max_rounds}`, `audit.max_findings_per_unit`
- Legacy `teaming.{analyzer,validator,exploiter}.provider_list` (chip
  input for provider rotation; required when `audit.mode` is `deep`
  until provider rotation is rewritten onto the `audit.*` shape in a
  follow-up change)
- `coder.*` operational + infrastructure knobs (HTTP- and container-only
  fields auto-disable when transport / endpoint conditions don't match;
  deprecated `coder.repo_mount_path` is not exposed)
- `llm.default_provider`
- `llm.providers.<name>.{base_url, model_name, kind, thinking_enabled, thinking_effort, request_timeout_seconds, temperature, top_p, top_k, repetition_penalty}`
- `agents.<role>.llm.*` including `request_timeout_seconds`

### Secrets — yml-only, redacted at the API boundary

Every secret-shaped key is dropped from the `/api/config/effective`
response body before it reaches the browser. A key is secret-shaped
when its dotted path ends in `.api_key`, `.password`,
`.model_api_key`, or `.endpoint_token`, OR matches the glob
`*.remote.url`. The portal renders these keys as a read-only
"configured" / "not configured" stub driven by the `redacted_keys`
map in the same response. The snapshot endpoint additionally rejects
any save attempt that names a secret-shaped key with HTTP 400.

See [`portal-settings.md`](./portal-settings.md) for the full
key-by-key reference, the chip-input behaviour spec, and the
schema-walk guardrail test.

Attempts to `PUT /api/config/snapshot` with any forbidden key return
HTTP 400 with the rejected keys listed in the response body. Those
values must live in `xauditor.yml` only — they are credentials and /
or connection info that the operator controls out-of-band.

Each save also writes a human-readable nested YAML dump to
`.xauditor/portal/effective-config.yml` so you have a diffable
textual record of whatever the backend is currently using.

## Remote databases

When either of these is set, `xauditor` skips the managed container
lifecycle for that database and connects to the remote endpoint:

```yaml
reportdb:
  remote:
    url: postgresql+asyncpg://user:pass@host:5432/xauditor_reportdb
    # optional:
    # ssl_ca: /etc/ssl/cert.pem
    # pool_size: 10

graph:
  db:
    remote:
      url: neo4j+s://graph.example.com:7687
```

When a remote URL is configured:

- `xauditor graphdb init/start/stop/reset` and `xauditor reportdb init/start/stop/reset`
  refuse to run against that database with a clear message.
- `xauditor init` honors both remote flags and skips only what's remote.
- `xauditor audit` connects via the remote URL; Markdown artifacts are
  produced as usual.
- The remote URL is loaded **only** from `xauditor.yml` (or the
  matching env vars `XAUDITOR_REPORTDB_REMOTE_URL` /
  `XAUDITOR_GRAPHDB_REMOTE_URL`). The UI cannot read or write it.

Accepted schemes: `postgresql://`, `postgresql+asyncpg://`,
`postgres://` for the report DB; `bolt://`, `bolt+s://`, `bolt+ssc://`,
`neo4j://`, `neo4j+s://`, `neo4j+ssc://` for the graph DB. Anything
else is rejected at config load time.

## Authentication + sessions

- Default user: `admin / admin` with role `admin`, seeded on first
  Alembic run only when `auth.users` is empty (safe to re-run
  migrations). Pre-existing rows from earlier portal versions are
  backfilled to role `admin` by the same migration so no operator is
  locked out by the upgrade.
- First login forces a password change. Until rotated, the API rejects
  every endpoint except `POST /api/auth/change-password` and
  `GET /api/health`; the UI blurs the main shell behind the modal.
- Password policy: >= 12 characters, at least one letter and one digit,
  no reuse of the current password.
- JWT: HS256, HttpOnly SameSite=Lax cookie named
  `xauditor_portal_session`, 12h TTL. The token carries the user's
  current `role` claim — endpoints check it on every request rather
  than re-reading the DB. Signing secret is
  `XAUDITOR_PORTAL_JWT_SECRET` if set, else
  `.xauditor/portal/jwt.secret` (0600, auto-generated on first boot).
- Session revocation uses a per-user `sessions_invalid_before`
  timestamp instead of enumerating outstanding `jti` values. The
  `current_user` dependency rejects any cookie whose `iat` predates
  that timestamp, which closes the staleness window without needing a
  global revocation list. The timestamp is bumped (and a fresh cookie
  is rotated for the active session) on:
  - **password change** by the user themselves;
  - **role change** by an admin;
  - **admin-initiated password reset** of the user's account.
  Other browsers / API clients holding a pre-bump cookie are forced
  to re-authenticate on their next request.

## Roles and access control

The portal enforces three roles, ordered admin ≥ auditor ≥ viewer:

| Role | Read findings | Write feedback / notes | Edit Settings | Manage users |
|---|---|---|---|---|
| `admin`   | ✓ | ✓ | ✓ | ✓ |
| `auditor` | ✓ | ✓ | — | — |
| `viewer`  | ✓ | — | — | — |

- **viewer** is read-only across the entire portal. The feedback
  control on every finding card is rendered disabled with a tooltip
  ("read-only role"); the Settings tab is reachable but `PUT
  /api/config/snapshot` is rejected with HTTP 403.
- **auditor** can label findings (`true_positive` / `false_positive`)
  and add / edit reviewer notes, but cannot save Settings snapshots
  and cannot see the Users tab.
- **admin** can do everything an auditor can, plus save Settings
  snapshots and manage other accounts via the **Users** tab. Only an
  admin can change another user's role or trigger an admin-initiated
  password reset.

The role is stored in `auth.users.role` as an enum (`admin` / `auditor`
/ `viewer`) and is included as a top-level `role` field in the
`POST /api/auth/login` and `GET /api/auth/me` response bodies. The
sidebar filters tabs against the current user's role, so a viewer
or auditor never sees the Users entry in the navigation.

### Users tab (admin-only)

The **Users** tab is the operator-facing surface for managing accounts:

- **Create user** — username + initial password (must satisfy the
  same password policy as a self-service change) + role. The newly
  created user is forced to rotate the password on first login.
- **Per-row role select** — admin can demote / promote any other
  account between the three roles. Changing a role bumps that user's
  `sessions_invalid_before` so any active cookie they hold becomes
  invalid on the next request.
- **Reset password** — replaces the password and forces re-rotation
  on the user's next login. Also bumps `sessions_invalid_before`.
- **Delete user** — soft-locked guards:
  - the **last admin** account cannot be deleted or demoted (server
    rejects with HTTP 409 inside an advisory-lock-protected
    transaction; the UI greys out the Delete button and the
    non-admin role options for that row);
  - admins **cannot delete their own account**, even when other
    admins exist (server returns HTTP 409, UI greys out the Delete
    button on the self row with the tooltip "You cannot delete your
    own account"). This prevents the session-revoke-on-cascade from
    stranding the admin mid-action.

The CRUD endpoints live under `/api/users/*` and every one is gated
on `require_admin`. Auditor and viewer requests get HTTP 403 before
the handler runs.

## Human feedback export

For reinforcement-learning post-training, stream every finding in a
run paired with its latest annotation:

```bash
curl -b /tmp/xauditor.cookies \
     -X POST http://localhost:8080/api/auth/login \
     -H "Content-Type: application/json" \
     -d '{"username": "admin", "password": "<your-password>"}'

curl -c /tmp/xauditor.cookies -b /tmp/xauditor.cookies \
     "http://localhost:8080/api/runs/<run-id>/feedback-export?format=jsonl" \
     > run.jsonl
```

Each line is one JSON object with every Markdown-contract finding
field (analysis, reason, context, source refs, validation analysis,
exploitation status, etc.) plus a `feedback` sub-object:

```json
{
  "run_id": "...",
  "finding_id": "F001",
  "finding_name": "Command injection via `ls {user_input}`",
  "confidence_level": "High",
  "analysis": "...",
  "reason": "...",
  "validation_status": "Valid",
  "validation_analysis": "...",
  "exploitation_status": "exploitable",
  "exploitation_steps": "...",
  "feedback": {
    "label": "true_positive",
    "researcher_note": "confirmed against the fuzz harness",
    "reviewer_username": "alice",
    "created_at": "2026-04-18T01:02:03Z",
    "updated_at": "2026-04-18T01:02:03Z"
  }
}
```

Findings that have never been labeled carry `"feedback": null`. A
conservative secret-redaction pass runs over the free-text fields so
obvious credentials in finding bodies are stripped before export.

## Run hierarchy

The Report tab top-level view lists every audit run with project name,
repo path, `fast` / `deep` mode badge, status, live progress
percentage, and aggregate finding counts (total / valid / false
positives / unlabeled).

Opening a run reveals three sub-tabs:

- **Findings** — filterable list of finding cards (collapsed by
  default; expand to see every field plus syntax-highlighted source
  snippets, referenced symbols, exploitation steps, validation
  analysis, and the feedback control). For deep-mode runs the
  expanded body also includes a **Validator debate** section
  rendered inline as a `<details>` element (folded by default);
  expanding it shows the full per-round transcript — final verdict,
  convergence state, round cap, each round's participating
  subagents, verdicts, rebuttals, and raw responses — plus the path
  fingerprint footer. The chip appears on the collapsed header only
  when the backend has a debate row joined to that finding.
  Above the findings list, the **Coverage panel** (see below)
  surfaces the per-mode Coverage Gaps payload; expanded finding
  cards additionally render a **Per-Unit Verdicts panel** when
  the cross-unit reconciler consolidated multiple per-unit
  verdicts into one finding.
- **Coverage** — modules / files / functions audited vs unaudited vs
  excluded vs skipped. (Distinct from the Coverage panel above —
  this sub-tab is symbol-level; the panel is vulnerability-class-level.)
- **Audit Log** — `progress_events` timeline (`started` →
  `progress` → `stage_completed` → `finished` / `failed`).

### Coverage panel (Findings sub-tab)

The Coverage panel renders directly above the findings list and
mirrors the run's `coverage_gaps` JSONB payload (Phase 5A+). Three
groups, color-tagged:

- **Audited** — vulnerability classes the run actually covered
  (success tone).
- **Skipped by mode** — classes a different `audit.mode` would
  have covered (warning tone). In fast-mode runs this group
  drives the **CTA banner** at the top of the panel: a
  copy-to-clipboard button for `xauditor audit run --mode deep`,
  dismissable per run via a `localStorage` key
  (`coverage-cta-dismissed-{runId}`).
- **Out of scope** — classes xauditor structurally cannot cover
  (e.g. `supply_chain`, `cryptographic_primitives`); use the
  named external tooling (neutral tone).

Default expansion: fast-mode runs with a non-empty
`skipped_by_mode` group default to expanded; deep-mode runs default
to collapsed. Pre-Phase-5 runs (no `coverage_gaps` JSONB) render
a `n/a — pre-Phase-5 audit` placeholder instead.

The `audit.coverage_gaps.report` toggle in the **Settings → Audit
mode → Advanced overrides** section suppresses the panel by
emitting `coverage_gaps: null` on subsequent audit runs.

### Per-Unit Verdicts panel (finding card)

When the cross-unit reconciler consolidates multiple per-unit
verdicts into a single finding (e.g. the same SQL-injection finding
surfaces from both a `PathAuditUnit` and a `SinkAuditUnit`), the
finding card's expanded body renders a **Per-Unit Verdicts panel**
directly under the Validation Analysis lines. Each row shows the
unit-kind badge, color-coded per-unit verdict, and a truncated
analysis (click the row to expand the full text). When the
reconciler emitted a `consolidation_reasoning` prose block, it
renders below the per-unit list.

Phase 5A's `PassthroughReconciler` writes `NULL` to the
`findings.reconciliation` JSONB column for path-only audits, so
the panel returns `null` and the card stays compact. The panel
lights up once the agentic reconciler from
`agentic-stage-runner-real` runs against multi-unit-finding
fixtures.

Per-subagent outputs (analyzer / validator / exploiter) remain
reachable via the REST API rather than as dedicated sub-tabs.

Running audits refresh at 3-second intervals via React Query polling;
completed audits stop polling automatically.

## Operational notes

- **Postgres is mandatory.** `xauditor audit run` and `xauditor audit
  resume` pre-flight aborts before any LLM call when the configured
  Postgres endpoint is unreachable, authentication fails, or the
  schema is below the required Alembic revision. The error message
  recommends `xauditor reportdb start` / `init` or a config review.
- **The portal sink is canonical.**
  `xauditor_portal.sinks.PostgresReportSink` receives per-row UPSERTs
  as the audit streams, so the portal shows verdicts settle live.
  There is no longer a Markdown sink at audit-time — render Markdown
  on demand via `xauditor audit export <run_id> --format markdown`.
- **`xauditor reportdb init`** runs Alembic migrations via
  `xauditor_portal.db.migrations.upgrade_to_head` when the portal
  package is importable. When the DB is up but the portal package is
  not installed, the init skips migrations with an info log; they
  run later via `xauditor-portal migrate` once the package arrives.
