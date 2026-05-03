# Changelog

All notable changes to `xauditor` are logged here. The portal package
(`xauditor-portal`) tracks its own version inside
`packages/xauditor-portal/pyproject.toml`. The coder microservice
package (`xauditor-coder-service`) tracks its own version inside
`packages/xauditor-coder-service/pyproject.toml`.

## [1.0.0] - 2026-05-02

### Coder service (`xauditor-coder-service`)

- **Nested project layouts (`support-nested-coder-projects`).** The
  coder service and `xauditor coder status` now accept slash-separated
  project names (`team/repo`, `org/team/sub/repo`). The discovery walk
  descends the workspace tree without a depth cap and yields **every
  directory at every depth** — no marker file required. A two-phase
  walk guarantees the depth-1 listing always completes (even on slow
  HGFS / 9p mounts), bounded by per-call entry (10 000) and
  wall-clock (3.0 s) budgets. Audit-side derivation now computes the
  project name as `repo_root` relative to `coder.workspace_root`
  (e.g. `workspace=/home/user/ws` + `repo_root=/home/user/ws/team/repo`
  → `project=team/repo`), so operators do not need to set
  `coder.project_name` for nested layouts. Pre-flight performs a
  local-filesystem trust check before falling back to the
  `GET /projects` listing probe so the listing's truncation on slow
  filesystems no longer blocks the audit. The host-side
  `_discover_projects` shares `walk_projects` with the service
  (vendored copy + drift test). Bumps `xauditor-coder-service` to
  `0.4.x` (see package CHANGELOG for the per-version evolution).

### Removed

- **`xauditor portal build` CLI verb (and its underlying
  `services.portal_build` / `PortalRuntimeManager.build` /
  `InMemoryPortalRuntimeManager.build` surface).** Image building was
  always required for first-time setup, and `xauditor portal init`
  already auto-builds missing images via `_ensure_image_present` —
  the standalone `build` verb was redundant. Operators who used
  `xauditor portal build` (or `--service backend|frontend`) should
  run `xauditor portal init` instead; it is idempotent and reuses
  existing images. The portal-runtime-packaging spec is updated to
  drop `build` from the supported verb list and remove the
  "Build a single service" scenario.

### Portal (`xauditor-portal`)

- **User soft-disable, sidebar rename, first-login fix, self-role lock,
  admin run management.** A new bundle of portal admin and UX changes:
  - Admins can soft-disable users via a new `disabled_at TIMESTAMPTZ`
    column on `auth.users` (Alembic `0009_users_disabled_at`) plus
    `POST /api/users/{id}/disable` and `POST /api/users/{id}/enable`
    endpoints. Disable revokes the user's active session immediately
    (the `current_user` dep now rejects disabled users with HTTP 401)
    and refuses fresh logins with HTTP 403. The "at least one enabled
    admin" invariant now counts only `role='admin' AND disabled_at IS
    NULL` rows, and the last-admin guard extends to disable as well as
    delete and demote. The Users tab gains an Enable/Disable button
    and a `disabled` status badge.
  - **Admins cannot change their own role.** `PATCH /api/users/{id}`
    with `id == caller.id` and a `role` field in the body now returns
    HTTP 409 unconditionally, regardless of how many admins exist —
    even self-PATCHing the same role. Username self-edits without a
    `role` field still succeed. The Users-tab UI hides the role
    selector on the signed-in admin's own row with an "Ask another
    admin to change your role" hint.
  - **Reports tab rename.** The sidebar label flips from `Report` to
    `Reports`, matching the route, breadcrumb, and page heading. The
    tab id (`report`) and the route (`/reports`) are unchanged so deep
    links keep working.
  - **First-login `Failed to load projects.` bug fixed.** The
    authenticated layout now suppresses page children entirely while
    `must_change_password === true`, rendering only the existing
    `PasswordChangeModal`. Previously the children mounted (blurred)
    and fired `useProjects()`, producing a 403 from
    `forbid_must_change_password` that surfaced as a red error toast
    behind the modal.
  - **Deleted runs disappear from the Audit Run list immediately, not after
    a network round-trip.** The list now optimistically removes the row
    on the API's HTTP 204 — within one React render frame — instead of
    waiting for the invalidate-driven refetch. An admin who clicked the
    row's link in the brief window between click and refetch could
    previously navigate to a 404; that race is closed by hiding the row
    before the refetch fires. Page-empty handling: removing the last row
    on a non-first page navigates back one page rather than leaving a
    phantom "Page 3 of 2" empty state. The existing run-detail "Could
    not load this run." friendly fallback remains as the safety net for
    concurrent peer-admin deletes.
  - **`DELETE /api/runs/{id}` now safely deletes runs that contain
    duplicate-labeled findings.** Previously, deleting a run whose
    findings had `finding_annotations` with `label='duplicate'` pointing
    at sibling findings in the same run would race the cascade: the
    SET NULL trigger on `finding_annotations.duplicate_of_finding_id`
    fired before the CASCADE delete on `finding_annotations.finding_id`,
    briefly producing rows with `label='duplicate'` AND
    `duplicate_of_finding_id=NULL`, which violates the
    `ck_finding_annotations_duplicate_pointer` CHECK constraint and
    aborts the delete with HTTP 500. The handler now pre-deletes
    `finding_annotations` for the run inside the same transaction
    before issuing the `audit_runs` delete, so the SET NULL trigger
    has no rows to update. The schema-level fragility (CHECK + SET NULL
    are inconsistent for any "delete a finding" path) is captured as a
    follow-up; the handler-level workaround is sufficient for the
    full-run-delete case this change introduced.
  - **Run-deleted abort signal pierces every defensive `except Exception`
    in the audit workflow and the sink bus.** The original detect-and-
    abort work added the typed `RunDeletedExternallyError` and the
    workflow / CLI handlers that route it, but six defensive
    `except Exception` clauses in `ReportSinkBus.write_progress`,
    `ReportSinkBus.write_resume_state`, `AuditWorkflow._on_settled`
    (per-row + snapshot fallback), and `AuditWorkflow._drain_coder_results._apply`
    (per-row + snapshot fallback) silently swallowed the abort signal
    and demoted it to a warning, leaving the audit running and burning
    LLM tokens until the next un-swallowed sink call eventually
    propagated. Each clause now re-raises `RunDeletedExternallyError`
    before the generic catch-and-log. The drain-side patches run on
    the main thread and propagate immediately; the stream-callback
    patches run on coder worker threads and are absorbed by Python's
    done-callback machinery, but the warning log line is now accurate
    (`RunDeletedExternallyError` shows up explicitly instead of being
    masked as "Per-finding upsert for streamed coder verdict failed").
    The actual abort latency is bounded by one in-flight path
    completion (snapshot emit + drain on the main thread fire at that
    point), not by the number of pending coder tasks.
  - **Worker-side detect-and-abort on external run deletion.** When the
    `PostgresReportSink` discovers the run row has been deleted out from
    under a still-running audit worker (typically by an admin running
    `DELETE /api/runs/{id}`), it now raises a typed
    `RunDeletedExternallyError` instead of silently creating an orphan
    "(orphan — open_run not called)" row or no-op'ing the write. The
    audit workflow catches the exception before its generic failure
    handler (so it does NOT try to flush a `failed` status to a row
    that no longer exists), and the CLI surfaces a single-line message
    plus exit code `75` (EX_TEMPFAIL) so wrapper scripts can route the
    "rerun me" case distinctly from generic exit-1 failures. The
    orphan-row diagnostic remains for the genuine
    `open_run`-was-never-called programming bug; the two failure modes
    are now distinguished cleanly.
  - **Admin run management.** New admin-only `POST /api/runs/{id}/cancel`,
    `POST /api/runs/{id}/complete`, and `DELETE /api/runs/{id}`
    endpoints let admins manually transition a run's status or remove
    it entirely. Cancel/complete are idempotent and allowed from any
    starting status (admin override surface — terminal `completed_at`
    timestamps are preserved). Delete cascades through every dependent
    row (findings, progress events, debates, subagent records,
    coverage tables, finding annotations) via existing `ON DELETE
    CASCADE` foreign keys. Every action records a row in the new
    `report.audit_run_admin_actions` audit-log table (Alembic
    `0010_audit_run_admin_actions`); audit-log INSERT failures log a
    warning but do NOT fail the API response. The Reports tab adds
    admin-only row-level action menus on the Audit Run list and a
    header action set on the Run detail page, both hidden from
    non-admins. A confirmation dialog with an optional free-text
    reason precedes every action.
- **BREAKING — RBAC and Users tab.** Every account now holds exactly one
  role from the closed set `{admin, auditor, viewer}`. The default
  first-boot seed changes from `auditor / auditor` to `admin / admin`
  (still with `must_change_password=true`). Existing installs are
  auto-migrated by the new `0008_users_role` Alembic migration —
  every pre-existing user is promoted to `admin` so no operator is
  locked out of their own portal. New `Users` sidebar tab and
  `/api/users/*` admin-only CRUD let admins create / patch / reset /
  delete users and assign roles. Last-admin deletion / demotion is
  rejected with HTTP 409 (and disabled in the UI). Feedback-write
  endpoints now require the writer tier (`admin` or `auditor`); viewer
  attempts return 403 and the UI renders the controls read-only.
  Role changes and admin-initiated password resets stamp a
  `sessions_invalid_before` timestamp on the affected row; the auth
  dep rejects any cookie issued before that boundary, forcing a
  re-login that picks up the new role.
- **Settings page now covers every UI-editable knob in `XAuditorConfig`.**
  Adds new sections for Audit (`audit.worker_count`), Coder (the full
  `coder.*` block except the deprecated `repo_mount_path` and the
  yml-only secrets), and Repository (`repository.excludes`). Existing
  Graph build / LLM provider / per-agent / Teaming sections gained
  every field shipped since the last UI touch (`graph.build.{max_file_bytes,
  paths_max_depth, paths_max_count, neo4j_chunk_size}`,
  `llm.providers.<name>.{kind, thinking_effort, request_timeout_seconds}`,
  `agents.<a>.llm.request_timeout_seconds`,
  `teaming.<team>.provider_list`).
- **Source-based read-only rule.** Any field whose effective source is
  `yml` or `env` is now read-only with the original value visible —
  earlier behaviour only blocked editing on the both-sources case,
  which let yml-only fields silently shadow yml on save. New `env`
  source tag surfaces `XAUDITOR_*` env-var overrides distinctly from
  yml.
- **Secret-handling tightened at the API boundary.**
  `GET /api/config/effective` now redacts every secret-shaped key
  before serialisation (suffixes `.api_key`, `.password`,
  `.model_api_key`, `.endpoint_token` and the `*.remote.url` glob).
  Redacted values appear as `null` in the response body and a new
  `redacted_keys: { <key>: { present, source } }` map drives a
  read-only "configured" / "not configured" stub in the UI.
  `PUT /api/config/snapshot` rejects any save attempt that names a
  secret-shaped key with HTTP 400. A schema-walk guardrail test
  fails if a future config refactor adds a new such field without
  updating the redaction list.
- **Generic `<ChipInput>` control** for `teaming.<team>.provider_list`,
  `repository.excludes`, and `coder.cli_command`. Supports add on
  Enter / comma / blur, paste-multi-add on `,`/`\n`/`;`, per-chip
  validator (used to flag unknown providers), and an optional
  autocomplete popover.
- **Settings IA rework**: anchor TOC at the top of the page, every
  section becomes a collapsible card, and each section header carries
  a count badge with the number of yml/env-sourced (read-only) keys
  in that section.

## [0.11.0] - 2026-05-01

### Added

- **Multi-project coder service.** The HTTP-transport coder
  container now serves any number of projects from a single bind-
  mount: set `coder.workspace_root` to a host directory whose direct
  child subdirectories ARE the audit-able projects. New project =
  `mkdir <workspace_root>/<name> && git clone …` on the host —
  no `xauditor coder` lifecycle command, no container restart. The
  audit derives the project name from `basename(realpath(repo_root))`
  by default; override via `coder.project_name` for worktrees /
  symlinks / monorepo subdirectories. Pre-flight probes the
  service's new `GET /projects` endpoint and aborts the run with an
  actionable error before the first verification dispatch when the
  resolved project name does not exist.
- **`coder_status: "Fail"` terminal verdict.** Transport-level
  failures (network, HTTP 4xx/5xx, timeout, JSON parse error,
  subprocess crash) now produce `Fail` instead of `Inconclusive`,
  so reviewers can distinguish "claude rejected the finding"
  (`Not Verified`) and "claude was unsure" (`Inconclusive`) from
  "we couldn't reach claude" (`Fail`). The portal renders `Fail`
  with a danger-tone chip and a hover tooltip exposing
  `coder_reason`; Markdown artifacts label the analysis / evidence
  as "Not produced (transport failure)".
- **Structured transport-failure logging.** Every Fail outcome
  emits exactly one ERROR-level audit-log entry of the form
  `event=coder.transport_failure run_id=<id> finding_id=<id>
  project=<name|-> endpoint=<url|-> error_kind=<kind>
  error_detail=<short>`. `error_kind` is one of
  `connection_refused`, `http_4xx`, `http_5xx`, `timeout`,
  `parse_error`, `subprocess_exit`, `subprocess_crashed`, `auth`,
  `project_not_found`, `idempotency_conflict`. Operators can grep /
  forward to centralised logging without scraping `coder_reason`
  text.
- **Per-project `$HOME` isolation.** Concurrent verifications
  targeting different projects no longer share claude's session
  cache or MCP server state — each invocation runs with
  `HOME=/home/coder/<project>/`, scoped under the named
  `xauditor-coder-home` docker volume. The `mkdir 0700` happens
  on first use; the volume persists subdirectories across
  `xauditor coder stop` / `start`.
- **`xauditor coder status` now lists projects.** New `workspace:`
  and `projects:` lines surface the resolved `coder.workspace_root`
  and the discovered subdirectories. The list comes from the
  service's `GET /projects` when reachable; falls back to a host-
  side scan with `(host scan; container unreachable)` annotation
  otherwise.
- **`POST /verifications` request shape gains a `project` field**
  (server enforces `^[a-zA-Z0-9._-]+$` allowlist + realpath child-
  of-/workspace, returning HTTP 400 / 404 / 409 with structured
  bodies on rejection).

### Deprecated

- **`coder.repo_mount_path` and
  `XAUDITOR_CODER_REPO_MOUNT_PATH`** — replaced by
  `coder.workspace_root` (+ optional `coder.project_name`). One-
  release deprecation shim: configs using only the legacy field
  emit a one-time WARN and continue to work, with
  `effective_workspace_root` derived from `parent(repo_mount_path)`
  and `effective_project_name` from `basename(repo_mount_path)`.
  Setting both prefers `workspace_root` and WARNs that
  `repo_mount_path` is ignored. Removed in the version after next.

### Changed

- **`coder.transport: http` POST body adds `project`** (string,
  defaults to "") — backward-compatible for legacy single-repo
  containers (server-side validation passes through when empty).
- **`audit/coder.py` transport-error mapping.** All ten internal
  call sites that previously emitted `CoderResult(status=
  "Inconclusive", reason="transport error: …")` now emit `Fail`.
  Claude-side deliberation outcomes (claude returning `unknown` /
  `uncertain` or any unrecognised verdict value) keep mapping to
  `Inconclusive` — only the dispatch-couldn't-reach-claude
  category becomes `Fail`.

## [0.10.0] - 2026-04-30

### Breaking

- **Removed `audit.path_concurrency` config field** and the
  `XAUDITOR_AUDIT_PATH_CONCURRENCY` env var. xauditor 0.10+ uses a
  single concurrency knob — `audit.worker_count`. yaml configs that
  still set `audit.path_concurrency` raise `ConfigError` at
  config-parse time with a translation message walking the operator
  through the upgrade. The env var no longer overlays anything; it
  emits a one-time WARN at startup and is otherwise ignored, so
  stale shell rcs do not block startup.
- **Removed `LocalThreadPool`.** `worker_count: 1` (the default) now
  routes through a new `InlineExecutor` that runs paths synchronously
  on master's thread (no thread pool, no queue, no executor). For
  in-process parallelism use `worker_count: N >= 2` — the
  `LocalSubprocessPool` semantics are unchanged.
- **`build_worker_pool` factory signature changed.** The
  `path_concurrency` parameter is gone:
  `build_worker_pool(*, worker_count, config=None, source_descriptor=None, logger=None)`.
- **Removed validator rule** `worker_count >= 2 ⇒ path_concurrency == 1`
  along with its predicate field.

### Added

- **`InlineExecutor`** — synchronous-on-master `WorkerPool`
  implementation; replaces `LocalThreadPool` for the
  `worker_count: 1` path.
- **Single-shape "Audit pool" log line** at audit start:
  `Audit pool: workers=N (<inline|subprocess>; host total in-flight=N <path|paths>)`.
- **`Neo4jGraphRepository.write_module_chunk`** is a no-op stub for
  API parity with the in-memory variant (modules are reconstructed
  at audit time from `(:File)` rows; no separate `:Module` label).

### Removed

- `LocalThreadPool` class and its `path_concurrency` constructor.
- `audit.path_concurrency` field on `AuditConfig`.
- `XAUDITOR_AUDIT_PATH_CONCURRENCY` from the env-var overlay map
  (still detected, but only to emit the deprecation WARN).
- The `_audit_path_concurrency` parser block and the
  `worker_count >= 2 ⇒ path_concurrency == 1` validator block.
- Two-shape audit-pool log (one for thread mode, one for subprocess);
  collapsed to a single shape parameterised by pool kind.

### Migration

Operators with `audit.path_concurrency: N, audit.worker_count: 1`
in yaml: drop `path_concurrency` entirely. The audit will run
sequentially (`InlineExecutor`); raise `worker_count: N` if you
want N-fold subprocess parallelism back.

Operators with `audit.path_concurrency: N, audit.worker_count: 1`
who exported `XAUDITOR_AUDIT_PATH_CONCURRENCY` from a CI script:
the env var is silently ignored after a one-line WARN; remove the
export from the script to silence the WARN.

The pre-0.10.0 in-process thread-mode default (`worker_count: 1,
path_concurrency: 4` → 4 threads) becomes strictly sequential. Set
`audit.worker_count: 4` to restore 4-way concurrency via subprocess
workers (~80 MB Bolt client per worker).

## [0.9.0] - 2026-04-30

### Breaking

- **Removed in-memory graph-source backend.** `InMemoryAuditGraphSource`,
  `InMemorySourceDescriptor`, and the polymorphic
  `AuditGraphSource | GraphBuildResult` argument shape are gone.
  `Neo4jAuditGraphSource` is the only production source — every audit
  starts from a Neo4j-resident build. Tests that previously
  constructed `GraphBuildResult(...)` use `_TestOnlyGraphSource`
  under `tests/_helpers/` instead.
- **Removed `GraphBuildResult` dataclass.** Code that previously read
  `graph.functions`, `graph.function_by_id()`, etc. now goes through
  the expanded `AuditGraphSource` Protocol (`iter_functions()`,
  `lookup_function_by_id()`, `coverage_inventory()`, …). The four
  helper-method indexes from `GraphBuildResult` are now Cypher-backed
  on `Neo4jAuditGraphSource` (cached per source instance).
- **`services.build_graph(...)` returns `(build_fingerprint: str, status)`**
  instead of `(GraphBuildResult, status)`. Downstream callers
  construct a `Neo4jAuditGraphSource` from the fingerprint.
- **Removed `neo4j_sync` build stage.** `canonical_finalize` now
  streams records directly into Neo4j in chunks; the manifest's
  `neo4j_synced` flag is an alias for `final_build_ready`.
- **Removed `_check_in_memory_descriptor_budget` memory pre-check
  and its env vars** (`XAUDITOR_AUDIT_SKIP_MEM_CHECK`,
  `XAUDITOR_AUDIT_MEM_INFLATION`). The failure mode they protected
  against (linear graph copies per subprocess worker) is
  architecturally impossible now — Neo4jSourceDescriptor is ~200 bytes
  regardless of graph size.

### Added

- **Streaming `canonical_finalize`.** Reads per-stage JSONL records
  from disk and writes them to Neo4j in bounded chunks (default
  5,000 records per UNWIND-MERGE batch). Build-process resident heap
  no longer scales with total graph size.
- **`XAUDITOR_GRAPH_BUILD_NEO4J_CHUNK_SIZE` env var** (also
  `graph.build.neo4j_chunk_size` in `xauditor.yml`), clamped to
  `[100, 50000]`, default `5000`. Out-of-range raises `ConfigError`.
- **Startup Neo4j connectivity check** in `xauditor graph build` and
  `xauditor audit run`. On unreachable Neo4j, the error message
  prints a numbered three-option block walking through
  `xauditor graphdb init` (first run), `xauditor graphdb start`
  (subsequent), and configuring a remote endpoint via
  `graph.db.remote.url`.
- **`AuditGraphSource` Protocol expansion** — nine new methods
  (`iter_functions`, `iter_classes`, `iter_edges`,
  `iter_module_symbols`, `lookup_function_by_id`,
  `lookup_class_by_id`, `lookup_path_by_fingerprint`,
  `lookup_function_by_name`, `coverage_inventory`) cover the
  surface that `GraphBuildResult` exposed in 0.8.x and earlier.
- **`Neo4jGraphRepository` chunk writers** — `write_function_chunk`,
  `write_edge_chunk`, `write_path_chunk`,
  `write_module_symbol_chunk`, `write_symbol_use_chunk`,
  `write_class_chunk`, `write_class_member_chunk`,
  `write_file_chunk`, plus `begin_build` / `finalize_build`
  bookends that stamp the `(b:Build)` node.
- **`Neo4jDriverProtocol.run_write`** for parameterised UNWIND
  writes; the bulk-string `run_batch` API is reserved for schema
  setup.
- **`tests/_helpers/_TestOnlyGraphSource`** — minimal in-memory
  `AuditGraphSource` for unit-test ergonomics. Production code MUST
  NOT import from `tests/_helpers/` (CI lint enforces this).

### Removed

- `InMemoryAuditGraphSource`, `InMemorySourceDescriptor`, the
  in-memory branch of `rebuild_source`, and helper functions
  (`_ensure_paths`, `_synthesize_paths_from_edges`,
  `_build_coverage_from_graph`).
- `GraphBuildResult` dataclass + its four helper-method indexes.
- `_check_in_memory_descriptor_budget` + the `_emit` stderr
  fallback helper.
- `services._graph_for_persistence`.
- `Neo4jGraphRepository.load_build` (returned `GraphBuildResult`).
- `neo4j_sync` build stage label.

### Migration

0.8.x users with cached graph builds: re-run `xauditor graphdb init`
(if not already set up) followed by `xauditor graph build`. The
per-stage record caches (inventory, reference, synthesized) are
reused as-is; only `canonical_finalize` re-runs in earnest, this
time streaming records into Neo4j (typical wall-clock ~1 minute
for a 100k-function repo on local Neo4j).

0.8.x users running fully in-memory (no Neo4j configured) hit the
new fail-fast on `xauditor audit run`. The error walks the operator
through `xauditor graphdb init` (first time) and the
`graph.db.remote.url` config option for externally-managed Neo4j.

## [0.8.0] - 2026-04-30

### Breaking

- **Subprocess mode (`audit.worker_count >= 2`) now requires
  `audit.path_concurrency == 1`.** The combination
  `worker_count >= 2 && path_concurrency > 1` is rejected at
  config-parse time with a `ConfigError` naming both fields. To
  scale up host-wide in-flight count in subprocess mode, raise
  `audit.worker_count`, NOT `audit.path_concurrency`. For large
  in-memory graphs, consider switching to a Neo4j-backed source
  so worker memory does not scale linearly with `worker_count`.

  Migration recipe — anyone running parallel audits before 0.8.0:

  - **Old** (`worker_count: 2, path_concurrency: 4`, host-wide
    in-flight = 8): pick one of:
    - **Same in-flight, more memory** —
      `worker_count: 8, path_concurrency: 1` (8 subprocess copies
      of the in-memory graph).
    - **Half the in-flight, half the memory** —
      `worker_count: 4, path_concurrency: 1` (4 subprocess copies).
    - **Switch backend** — keep `worker_count: 8` but use a
      Neo4j-backed source (graph lives in the daemon, workers are
      Bolt clients).

  Thread mode (`worker_count: 1`) is unchanged — `path_concurrency`
  still controls in-process thread parallelism in master.

### Changed

- `LocalSubprocessPool` workers now process paths sequentially —
  one path at a time per subprocess. The internal per-worker
  `LocalThreadPool` is gone. Host-wide peak in-flight in subprocess
  mode is `worker_count` (was `worker_count × path_concurrency`).
  This eliminates the in-process thread-safety surface that
  motivated the `last_thinking` `threading.local` shim and the
  removed-in-0.7.4 `CircuitBreaker` shared-global race.

- The `_subprocess_worker_main` signature drops its
  `path_concurrency` kwarg. `LocalSubprocessPool.__init__` drops
  its `path_concurrency` parameter and the `path_concurrency`
  property. The `build_worker_pool(...)` factory still accepts
  `path_concurrency` — it's now only forwarded to `LocalThreadPool`
  (thread mode); subprocess-mode calls ignore it.

- The master-side `_claims` dict now keys by `worker_id`
  (`dict[str, int]`) instead of `path_index` (`dict[int, str]`).
  A subprocess worker holds at most one in-flight claim at any
  instant. Crash routing (`_check_dead_workers`) is now O(1) per
  dead worker — direct dict pop by worker_id, no scan over claims.
  Worker → master result messages now carry the worker_id
  (`("result", PathResult, worker_id)`) so master pops the claim
  entry by key.

- The "Audit pool" INFO log line at audit start drops the
  `× path_concurrency` factor in subprocess mode. New shapes:
  - thread mode: `Audit pool: workers=1 path_concurrency=N (host total in-flight=N paths)`
  - subprocess mode: `Audit pool: workers=N (host total in-flight=N paths)`

### Added

- **Memory pre-check** at `LocalSubprocessPool` construction: when
  the source descriptor is `InMemorySourceDescriptor`, master
  estimates total worker-side memory (`pickled_bytes × worker_count
  × inflation_factor`, default inflation = `4`) and emits a single
  WARN line if the estimate exceeds 60% of currently-available
  host RAM. The WARN is informational — it does NOT abort the
  audit — and names the pickled bytes, worker count, inflation,
  estimated total, available RAM, and recommends lower
  `worker_count` or a Neo4j-backed source. Suppress with
  `XAUDITOR_AUDIT_SKIP_MEM_CHECK=1`. Tune inflation with
  `XAUDITOR_AUDIT_MEM_INFLATION` (clamped to `[2, 8]`). Neo4j
  source descriptors are exempt (they don't deserialize a graph
  payload at spawn). If `psutil` is unavailable at import time,
  the helper emits a single INFO and proceeds without the check.

- New runtime dependency: `psutil>=5.9` (used by the memory
  pre-check; degrades gracefully if missing).

- New env vars: `XAUDITOR_AUDIT_SKIP_MEM_CHECK`,
  `XAUDITOR_AUDIT_MEM_INFLATION`.

### Spec

- `openspec/changes/flatten-path-concurrency-into-worker-count/`
  — proposal, design, delta spec, tasks. Delta MODIFIES three
  requirements in `multi-agent-path-auditing` (WorkerPool
  partitioning, deterministic finding-ids, orphan routing) and
  ADDS one new requirement (memory pre-check WARN). Strict
  validation passes.

## [0.7.4] - 2026-04-30

### Fixed

- **Same code base scanned under parallel topology produced
  fewer findings than the 0.5.x serial baseline.** Operator
  reported a meaningful gap; diagnosis traced it to the
  shared LLM-side `CircuitBreaker` introduced before parallel
  execution shipped. Under `path_concurrency >= 2`, three
  concurrent failures inside a few hundred milliseconds
  tripped the breaker; the next 30-second cooldown
  condemned every subsequent path's first agent call to
  `CircuitOpenError`, which `langchain_support.invoke_agent`
  rewrapped as `LLMError`, which the per-path workflow
  handler classified as `PathOutcome.LLM_FAILED`. Each
  affected path emitted zero findings — exactly the gap
  the operator observed.

  Fix: the LLM-side circuit breaker is removed entirely.
  Provider-side rate limiting (`429` + `Retry-After`) is
  the authoritative source of overload back-pressure;
  `xauditor.resilience.retry()` already caps recursion
  against an unresponsive provider with a bounded attempt
  count + exponential backoff. A path whose retries
  exhaust gets classified as `LLM_FAILED` in isolation —
  exactly the per-path failure isolation
  `add-failed-path-state-on-llm-error` (xauditor 0.5.4)
  was designed to deliver. The shared client-side breaker
  was actively breaking that isolation.

  Spec: see
  `openspec/changes/remove-llm-circuit-breaker/` —
  `error-handling-resilience` requirement narrowed to
  scope circuit-breaker semantics to Neo4j only. The
  Neo4j integration keeps its breaker (different failure
  shape; not concurrency-amplified).

  Touched files:
  - `src/xauditor/langchain_support.py`: dropped the
    `_MODEL_CIRCUIT_BREAKERS` global, the
    `breaker.call(lambda: retry(...))` wrapper, and the
    `CircuitBreaker` / `CircuitOpenError` imports. The
    `RetryExhaustedError` → `LLMError` rewrap stays.
  - `src/xauditor/llm.py`: dropped the
    `circuit_breaker: CircuitBreaker` field on
    `LLMClient`, the wrapper around the retry, and the
    `CircuitBreaker` / `CircuitOpenError` imports.
  - `src/xauditor/resilience.py`: `CircuitBreaker` class
    stays (Neo4j integration uses it). No deletion.
  - `src/xauditor/errors.py`: `CircuitOpenError` stays
    defined (Neo4j path raises it). No deletion.

  Behavioral change to be aware of: if an LLM provider is
  truly persistently down for the duration of an audit,
  every path now retries through the full exponential-
  backoff schedule before failing, rather than fast-failing
  via the shared breaker. Worst-case wall-clock cost:
  `path_count × retry_max_total_delay` ≈ 5 minutes for a
  200-path audit. Operators can Ctrl+C to abort early; the
  partial run carries the failure context.

- **Race on `LangChainChatModel.last_thinking` /
  `last_reasoning_tokens`.** These were instance attributes
  that `invoke_text` wrote and surrounding code read back
  for log decoration. Concurrent path workers sharing one
  chat-model instance could observe each other's response
  metadata (one path's debug log line surfacing another
  path's thinking text). Logging-correctness bug only — not
  the cause of missing findings — but lived in the same
  neighborhood.

  Fix: replaced the instance attributes with properties
  backed by a per-instance `threading.local()`. Each thread
  reads its own most-recent values; concurrent writers
  don't stomp. `AnthropicLangChainChatModel` gets the same
  treatment.

### Tests

- `tests/test_llm_no_circuit_breaker.py` (new, 3 cases) —
  pins the absence of `_MODEL_CIRCUIT_BREAKERS`,
  `CircuitBreaker`, `CircuitOpenError` in
  `langchain_support` and `llm`; pins that `LLMClient` no
  longer holds a `circuit_breaker` dataclass field.
- `tests/test_chat_model_thinking_race.py` (new, 4 cases) —
  verifies default empty values, per-thread isolation of
  the metadata under two concurrent workers, main-thread
  reads not affected by worker writes, and same shape on
  the Anthropic model class.

### Validation (operator-side, deferred)

- V1. Re-run the audit that prompted this change. Expected:
  same code under `path_concurrency=4` produces a finding
  count within the same band as the 0.5.x serial baseline.
- V2. Provoke a transient provider 429 burst mid-audit.
  Expected: only the affected paths retry-then-fail;
  subsequent paths are not sentenced.

### Out of scope (intentional)

- Neo4j-side `CircuitBreaker` is NOT removed. Different
  failure shape (DB outages aren't transient
  rate-limiting); per-path Neo4j fan-out is much lower
  than LLM fan-out. Revisit if mis-trips observed.

## [0.7.3] - 2026-04-29

### Fixed

- **`RuntimeLogger` flushes the stream after every emit.**
  Subprocess audit workers that emit a few hundred bytes of debug
  output then block on a 30s+ LLM call were leaving their output
  stranded in the stderr buffer when stderr was redirected to a
  file or pipe (Python's default `sys.stderr` is fully buffered
  at 8 KB in non-TTY contexts, so partial buffers never flushed
  until the buffer filled or the worker exited). Effect:
  `xauditor audit run 2> audit.log` showed master output but
  worker output was delayed by minutes.

  Fix: `RuntimeLogger._emit` now calls `self.stream.flush()` after
  each `write()`. ~one syscall per emit, negligible relative to
  the surrounding LLM I/O. The flush is wrapped in try/except so
  a stream that doesn't expose `flush` (or whose flush fails) does
  not crash the audit — losing one log flush is preferable to
  aborting a multi-hour run. The `log_file` branch already flushed
  implicitly via `with open("a")` so it's unchanged.

### Tests

- `tests/test_runtime_logger_tag.py` (3 new cases, total 8) —
  every `info()` triggers `write` then `flush` in that order
  with no batching; streams without a `flush` attribute work
  fine; a `flush` that raises `OSError` doesn't propagate.

## [0.7.2] - 2026-04-29

### Fixed

- **Subprocess audit workers were silent on structured logging.**
  Under `audit.worker_count >= 2` the subprocess workers spawned
  by `LocalSubprocessPool` called
  `AuditWorkflow.from_config(config, with_coder=False)` without
  passing `logger=`, so each worker's `self.logger` was `None`.
  Every `RuntimeLogger` emit site in `_process_one_path` is
  guarded by `if self.logger is not None:` — so under
  `worker_count >= 2` operators saw none of the per-path
  structured output (`Audit path start`,
  `Recorded audit checkpoint`, `Completed audit path N/M`,
  `Audit path failed: LLM error`,
  `Audit path failed: Neo4j transport error`,
  `Finding emitted`, etc.). The single-process thread-pool
  topology (`worker_count: 1`) was unaffected.

  Fix: `_subprocess_worker_main` now builds its own
  `RuntimeLogger.from_config(config, tag=worker_id)` and threads
  it into `AuditWorkflow.from_config`. `RuntimeLogger` gains an
  optional `tag: str = ""` field that prefixes the bracketed
  segment of every emitted line — examples:

  ```
  INFO [audit-worker-2/audit.workflow]: Completed audit path 47/200
  INFO [audit-worker-0/audit.workflow]: Recorded audit checkpoint | path=fp::abc | status=Valid
  ```

  Multi-worker output thus interleaves but is grep-able by
  worker_id (`grep 'audit-worker-2' audit.log` pulls one
  worker's full event stream). When `tag=""` (the default) the
  format is identical to pre-tag releases so single-process
  topologies see no change.

### Concurrency notes

- Multiple workers writing to the same `log_file` is safe at
  the line-atomicity level: `RuntimeLogger._emit` issues one
  `write()` per emitted line, and POSIX `O_APPEND` atomicity
  for writes < `PIPE_BUF` (typically 4096B) guarantees lines
  don't tear under concurrent emits. Lines from different
  workers interleave in event-time order; the `tag` prefix
  lets operators reconstruct each worker's stream.

### Tests

- `tests/test_runtime_logger_tag.py` (new, 5 cases) — default
  empty tag preserves the legacy format; tag appears inside
  the bracket segment as `<tag>/<module>`; tag-only when
  module is unknown; `from_config(tag=...)` propagates;
  N=4 threads emitting concurrently to a shared log_file
  produce no torn lines and exactly one tag per line bracket.

## [0.7.1] - 2026-04-29

### Fixed

- **Portal stuck on `in_progress` after Ctrl+C cancellation.**
  Sequence: user presses Ctrl+C, the audit doesn't stop
  immediately because `pool.shutdown(wait=True)` is draining
  in-flight subprocess workers (up to ~13s grace), user
  presses Ctrl+C several more times out of impatience, the
  process exits — but the portal still shows the run as
  `in_progress` instead of `cancelled`.

  Root cause: the second/third `KeyboardInterrupt` fired
  inside the workflow's `finally: pool.shutdown(wait=True)`,
  replacing the in-flight `UserCancelledError` that was
  about to propagate out (Python 3 exception-during-finally
  semantics). `services.py`'s
  `except UserCancelledError` block — the only thing that
  calls `sink_bus.fail_run(status="cancelled")` — never ran.

  Fix lands in two places:

  - `AuditWorkflow.run` gains an `on_cancel: Callable[
    [AuditRun, str], None] | None = None` hook that fires
    THE INSTANT a `KeyboardInterrupt` is observed, BEFORE
    `pool.cancel_all` and BEFORE the long
    `pool.shutdown(wait=True)` drain. `services.py` wires
    `_on_cancel` to call `sink_bus.fail_run(status=
    "cancelled")` + `write_progress` so the portal
    flips to "cancelled" immediately. Even if the rest of
    the cleanup is hard-killed by repeated Ctrl+C, the
    portal already reflects the correct state.
  - The `finally: pool.shutdown(wait=True)` block now
    suppresses `SIGINT` for its duration via
    `signal.signal(SIGINT, SIG_IGN)`. A frantic user
    mashing Ctrl+C during the drain no longer replaces
    the propagating `UserCancelledError` with a fresh
    `KeyboardInterrupt`, so `services.py`'s cancellation
    bookkeeping (`state_store.save_audit_run`,
    `write_resume_state`) always runs.

  The existing
  `except UserCancelledError` handler in `services.py`
  now treats the `fail_run` write as belt-and-suspenders:
  if `on_cancel` already flushed (the common path), we
  skip the duplicate; if `on_cancel` failed for any
  reason (sink momentarily unreachable), the handler
  retries.

### Tests

- `tests/test_audit_workflow_cancel_hook.py` (new, 3 cases)
  — `on_cancel` fires exactly once with the conventional
  cancel message, fires BEFORE the post-cleanup
  `on_progress` snapshot, and remains optional (workflow.run
  works without an `on_cancel` arg).

## [0.7.0] - 2026-04-29

### Added (additive — backward compatible)

- **`LocalSubprocessPool` now supports `Neo4jAuditGraphSource`.**
  Audits running against a Neo4j-persisted graph build can finally
  use `audit.worker_count >= 2`. Pre-0.7.0 the subprocess pool
  rejected non-`InMemoryAuditGraphSource` sources outright.

  Mechanism: every `AuditGraphSource` impl now exposes a
  `pool_descriptor() -> AuditGraphSourceDescriptor` method whose
  return value is a frozen-dataclass union — either
  `InMemorySourceDescriptor(graph: GraphBuildResult)` or
  `Neo4jSourceDescriptor(neo4j_config, host, build_fingerprint,
  repo_root, page_size)`. Both pickle byte-identically. Subprocess
  workers call a module-level
  `xauditor.audit.source.rebuild_source(descriptor)` factory on
  entry; backend-specific imports (e.g. neo4j-driver) live inside
  the factory so InMemory-only workers don't pay the import cost.

  Each subprocess worker holds its own `Neo4jDriver` instance —
  the host-wide Neo4j connection count is `worker_count + 1` (one
  driver per worker plus master's). Neo4j Community Edition's
  default `dbms.connector.bolt.thread_pool_max_size: 400` covers
  any sane `worker_count` value with margin to spare.

- **Per-path orphan routing on worker crash.** Closes the
  Phase 3 `tasks/6.5` deferral. Workers push
  `("claim", index, worker_id)` to the result queue between
  dequeue and `_process_one_path` invocation. The master tracks a
  `claims: dict[int, str]` keyed by path index. The monitor
  thread's empty-queue tick now scans for workers that
  transitioned alive → dead since the last tick; for each such
  worker, claimed paths get resolved as
  `PathOutcome.WORKER_CRASHED` with
  `error_info={"reason": "<worker> died with this path in flight",
  "worker_id": ..., "exit_code": ...}`. Master no longer hangs on
  `future.result()` when a single worker dies mid-path.

  Workers' shutdown `finally` block now calls
  `result_queue.close()` followed by `result_queue.join_thread()`
  so a worker that pushed a result then exited cleanly is NOT
  mis-classified as a crash. Order matters; explicit
  `join_thread()` is load-bearing.

- **Neo4j network failures classified as `PathOutcome.LLM_FAILED`.**
  `_process_one_path` now catches `neo4j.exceptions.ServiceUnavailable`
  and `neo4j.exceptions.SessionExpired` (lazily imported so
  non-Neo4j installs don't take the dependency) and returns a
  `PathResult` with `outcome=LLM_FAILED,
  error_info={"reason": "neo4j_unavailable", ...}`. The path
  lands in `failed_paths` and `xauditor audit resume` re-runs it
  on the next pass — same UX as a transient LLM failure.

- **`AuditGraphSource.close(self) -> None`.** Default no-op for
  the in-memory source; `Neo4jAuditGraphSource.close()` forwards
  to the underlying driver's `close()` so subprocess workers
  don't leak Bolt sockets at exit.

### Yaml example (no config change vs 0.6.0)

```yaml
audit:
  path_concurrency: 4   # 4 threads per worker
  worker_count: 4       # 4 subprocess workers
                        # → 16 paths in flight host-wide
                        # → 5 Neo4j connections (4 + master)
```

### Tests

- `tests/audit/test_source_descriptor.py` — 8 cases covering
  pickle round-trip for both descriptor types, factory dispatch
  (with mocked neo4j-driver constructors), pool-descriptor →
  pickle → rebuild equivalence, and `close()` no-op semantics.
- `tests/audit/test_subprocess_orphan_routing.py` — 5 cases
  exercising `_check_dead_workers` in isolation: orphans route
  to `WORKER_CRASHED`, idempotency across ticks, alive-worker
  no-op, no-claims-for-dead-worker safety, `_fail_outstanding`
  clears `claims` alongside `futures`.

### Known limitations (documented, not fixed)

- Hung (deadlocked) subprocess workers — `is_alive()` only
  detects exit. A worker stuck on a deadlock shows alive
  forever; mitigation requires per-path heartbeat-based
  liveness which is out of scope for 0.7.0.
- Connection storm at worker spawn — `worker_count: 16`
  produces 16 simultaneous Bolt handshakes against Neo4j.
  Acceptable in practice (Neo4j default thread pool is 400);
  stagger logic deferred until measured in the wild.

### Removed restriction

- The pre-0.7.0 `ValueError("audit.worker_count >= 2 requires
  an InMemoryAuditGraphSource ...")` is gone. The matching
  isinstance guard in `AuditWorkflow.run` is replaced with a
  single `source.pool_descriptor()` call that works for any
  source the audit can be built against.

## [0.6.0] - 2026-04-29

### Added (additive — backward compatible)

- **`audit.path_concurrency` + `audit.worker_count` config block.**
  `xauditor audit run` now parallelises path execution through a
  pluggable `WorkerPool`. Two knobs compose:
  - `audit.path_concurrency` — in-process threads per worker
    (default `4`, bounded `[1, 32]`).
  - `audit.worker_count` — subprocess workers
    (default `1`, bounded `[1, 16]`).

  Host-wide peak in-flight path count is
  `worker_count × path_concurrency`. Defaults preserve 0.5.x
  behaviour modulo the speedup from threading I/O-bound LLM calls.

- **`LocalThreadPool` (`worker_count == 1`).** In-process
  `ThreadPoolExecutor` of size `audit.path_concurrency`. Lowest
  startup cost; recommended for single-host audits against a
  rate-limit-tolerant provider.

- **`LocalSubprocessPool` (`worker_count >= 2`).** Spawns N
  subprocess workers via `multiprocessing.spawn` (fresh
  interpreter, no inherited fds / locks). Each worker runs its
  own `LocalThreadPool` of `audit.path_concurrency` size. Master
  is a pure orchestrator that submits paths via a request queue,
  fans `PathResult`s back via a result queue, and dispatches the
  coder stage centrally so there's exactly one `CoderDispatcher`
  regardless of worker count.

- **Crash recovery.** Subprocess worker death (SIGSEGV, OOM kill,
  any non-zero exit) is detected by the master's monitor thread.
  Outstanding paths on the dead worker resolve as
  `PathOutcome.WORKER_CRASHED` so `future.result()` never hangs;
  remaining workers continue processing the rest of the plan.

- **Cancellation.** `KeyboardInterrupt` sets the workflow's
  `_cancelling` flag, calls `pool.cancel_all()` (drops queued
  work; SIGTERMs live subprocess workers), and raises
  `UserCancelledError` with a partial-run snapshot. Thread-pool
  workers bail at the next agent-stage boundary; subprocess
  workers escalate to SIGKILL after a 13-second grace window.

- **Plan-order deterministic finding ids.** Aggregator walks
  futures in plan order regardless of which thread or subprocess
  produced each result — two runs against the same graph build
  with different topologies produce identical `finding_id`
  strings in the same relative order.

- **`XAUDITOR_AUDIT_PATH_CONCURRENCY`** and
  **`XAUDITOR_AUDIT_WORKER_COUNT`** env-var bindings.

### Yaml example

```yaml
audit:
  path_concurrency: 8     # 8 threads per worker
  worker_count: 2         # 2 subprocess workers
                          # → 16 paths in flight host-wide
```

### Validation

- Both fields are validated at config-parse time. Out-of-range
  values raise `ConfigError` naming the dot-path and the valid
  range.

### Tests

- `tests/audit/test_worker_pool.py` — 16 cases covering Protocol
  satisfaction, `submit_path` futures, concurrent overlap,
  `cancel_all` semantics, `PathResult` pickle round-trip (3
  shapes), `LocalSubprocessPool` constructor validation, and a
  real subprocess spawn smoke test that builds a minimal
  config + empty `GraphBuildResult`, spawns 2 workers, sends
  the sentinel, and verifies clean exit.
- `tests/config/test_audit_config.py` — 8 cases (defaults, yaml
  override, env-var override, out-of-range rejection for both
  fields).

### Restructure (no behaviour change)

- Per-path body extracted into `AuditWorkflow._process_one_path`.
  Pure with respect to workflow mutable state — only reads
  `self` (config, logger, agents) and the per-path `source`
  reader. Returns a pickle-friendly `PathResult` for the master
  aggregator to integrate.
- `AuditWorkflow.from_config` gains a `with_coder=True` kwarg.
  Subprocess workers use `with_coder=False` so coder dispatch
  remains centralized on the master.
- Heartbeats now fire from the master aggregator (per integrated
  `PathResult`) instead of from worker threads. Behaviour is
  identical for thread pool topology; subprocess pool topology
  emits in plan order rather than real time.

## [0.5.4] - 2026-04-29

### Fixed

- **Markdown JSON fence kills `json.loads` cold.** Anthropic
  Claude and several OpenAI-compat gateways wrap structured
  output in a ```` ```json\n{...}\n``` ```` fence even when the
  prompt forbids it. The first byte is a backtick, so
  `json.loads` failed immediately with
  `Expecting value: line 1 column 1 (char 0)` and the call
  burned its retry budget without changing anything.
  `model_factory.py` gains a string-aware
  `_strip_markdown_fences` helper that
  `LangChainChatModel.invoke_json` and
  `AnthropicLangChainChatModel.invoke_json` call before
  `json.loads`. The existing `_repair_json_typos` fallback
  chains after for unterminated / typoed JSON.

### Changed (per-path resilience)

- **Single-path LLM failure no longer kills the whole run.**
  Post-retry `LLMError` previously propagated out of the
  per-path try/except and aborted the audit even though every
  other path was independent. `audit/workflow.py` now has an
  `except LLMError as exc:` per-path handler (sibling of the
  existing context-window one) plus a `failed_paths: set[str]`
  threaded through `_build_audit_run` and the per-path
  `shared_state["failed"]` block (`operation`, `provider`,
  `model`, `cause`). New checkpoint label: `failed_llm_error`.
  Operators recover via `xauditor audit resume`, which now
  re-runs both context-skipped and LLM-failed paths.

- **Coverage accounting.** `models.py` adds
  `CoverageState.FAILED = "failed"`; `CoverageInventory.percentage`
  includes `FAILED` in the denominator (matching `INTERRUPTED`)
  so a failed path no longer artificially inflates the audited
  percentage.

- **Source builders.** `audit/source.py` and
  `integrations/audit_source_neo4j.py` — `build_coverage`
  accepts `failed_paths` and maps them to
  `CoverageState.FAILED`.

## [0.5.3] - 2026-04-30

### Added (additive — backward compatible)

- **`request_timeout_seconds` provider field.** Each
  `llm.providers.<name>` entry now accepts an optional
  `request_timeout_seconds` (positive number, seconds) that
  controls the per-call request timeout on the underlying
  LangChain client:
  - `kind: openai` → `ChatOpenAI(request_timeout=<value>)`
  - `kind: anthropic` →
    `ChatAnthropic(default_request_timeout=<value>)`

  When omitted the SDK's own default (~600s on both the OpenAI
  and Anthropic Python SDKs as of late 2025) applies. Operators
  on `thinking_effort: max` or vLLM deep-reasoning workloads
  should set a higher value here — the previous SDK default
  occasionally tripped on legitimate slow responses, surfacing
  as `httpx.TimeoutException` and discarding the model's actual
  response.

- **Per-agent override.** `agents.<agent>.llm.request_timeout_seconds`
  overlays the resolved provider value, mirroring the existing
  sampling-overlay rule. Useful when one agent is fast (auditor)
  and another is slow (validator deep review).

- **`LLMSettings.request_timeout_for(agent_name)`** — resolution
  helper returning the effective timeout (agent override →
  provider field → `None` for SDK default).

- **`XAUDITOR_LLM_REQUEST_TIMEOUT_SECONDS`** env-var binding
  (legacy single-provider shim, parity with
  `XAUDITOR_LLM_BASE_URL` / `XAUDITOR_LLM_API_KEY`).

### Yaml example

```yaml
llm:
  default_provider: anthropic
  providers:
    anthropic:
      kind: anthropic
      base_url: https://api.anthropic.com
      api_key: sk-ant-...
      model_name: claude-sonnet-4-7
      thinking_effort: max
      request_timeout_seconds: 1800   # ← 30 min ceiling for max-effort

agents:
  validator:
    llm:
      request_timeout_seconds: 600    # ← validator on shorter ceiling
```

### Validation

- `request_timeout_seconds` is validated as a finite positive
  number at config-parse time. `0`, negative, `nan`, `inf`, or
  non-numeric input raises `ConfigError` naming the offending
  dot-path.

### Tests

- `tests/config/test_request_timeout.py` — 7 cases (parse default,
  parse explicit, agent override overlay, agent-only override,
  zero/negative rejection, non-numeric rejection, legacy
  top-level shim).
- `tests/test_model_factory_request_timeout.py` — 7 cases
  (OpenAI dispatch with/without timeout, Anthropic dispatch
  with/without timeout, dataclass field exposure on both
  wrappers, explicit-override-wins).
- 14 new tests; 0 regressions on the existing OpenAI / Anthropic
  paths (483 → 497 OK).

### Migration

No migration required. Existing yaml without
`request_timeout_seconds` continues to use the underlying SDK
default (≈600s), which is the same behaviour as 0.5.2 and earlier.

## [0.5.2] - 2026-04-30

### Fixed (db containers join the shared network at creation, not after)

Operators reported `xauditor init` shipping a portal stack that
"looked healthy" but produced `socket.gaierror: Name or service not
known` on the first portal login — while the same operator running
`xauditor reportdb init` then `xauditor portal init` separately
succeeded. Root cause was structural:

- 0.5.1 created graphdb / reportdb containers on the default docker
  bridge, then **post-attached** them to `xauditor-portal-net` from
  inside `portal init` via a helper that
  [silently swallowed errors](src/xauditor/integrations/portal/runtime.py).
  When `xauditor init` fired all three steps within a few hundred
  milliseconds, the post-attach occasionally lost a race with the
  backend's first DB connection, and the failure surfaced only at
  login time with no log line operators could find.

The 0.5.2 fix moves network membership to **container-creation
time**:

- `Neo4jConfig.network_name` and `ReportDBConfig.network_name`
  fields, default `"xauditor-portal-net"`, configurable via yaml
  (`graphdb.network_name` / `reportdb.network_name`) or environment
  (`XAUDITOR_GRAPHDB_NETWORK_NAME` /
  `XAUDITOR_REPORTDB_NETWORK_NAME`).
- `ContainerLifecycleManager.init_runtime` ensures the configured
  docker network exists (idempotent) **before** `docker create`,
  appends `--network <name>` to the create argv, and on an
  already-existing container verifies it is connected to the
  configured network — re-attaching with `docker network connect`
  if not. Re-attach failure raises hard (no silent swallow).
- The portal runtime's post-create attach helper
  (`_attach_database_containers_to_network` and friends) is **gone**.
  `portal init` and `portal start` no longer touch db containers'
  network membership.
- `portal reset --yes` no longer removes `xauditor-portal-net` —
  the network is shared infrastructure now (graphdb / reportdb /
  portal all join it). Operators who want a fully clean slate run
  the per-runtime `reset --yes` then optionally
  `docker network rm xauditor-portal-net`.

### Migration from 0.5.1

- **Fresh installs**: no operator action required. Every container
  is created on the right network from the start; no race window
  exists.
- **Existing 0.5.1 deployments**: on the next `xauditor init` (or
  any per-runtime `init`), the lifecycle manager detects existing
  containers' missing network membership, runs `docker network
  connect` once, and emits an INFO log line. No yaml change needed.
- **Custom topology operators**: set `graphdb.network_name` /
  `reportdb.network_name` (or the matching env vars) to point at
  the network you want. The portal honours
  `portal.network_name` independently.

### Tests

- `tests/integrations/docker/test_lifecycle_network.py` (new) — 5
  tests covering: network created when missing, network skip when
  present, `--network` arg present in create command, upgrade
  re-attach for off-network existing containers, attach failure
  surfaces hard.
- 478 → 483 OK total; 0 regressions on the OpenAI / Anthropic /
  coder paths.

## [0.5.1] - 2026-04-30

### Added (additive — backward compatible)

- **`thinking_effort` provider field for Anthropic.** Each
  `llm.providers.<name>` entry now accepts an optional
  `thinking_effort` value: ``"low" | "medium" | "high" | "xhigh" |
  "max"``. On a `kind: anthropic` provider this routes to the
  Anthropic SDK's `effort=` kwarg (the supported dial Anthropic
  expects since late 2025). When unset, the provider falls back to
  the legacy `thinking_enabled: true` shape (`thinking={"type":
  "enabled", "budget_tokens": 8192}`) and emits a one-time WARNING
  per instance recommending operators migrate to `thinking_effort`,
  because Anthropic is deprecating that older path. Values are
  validated against the closed set at config-parse time; a typo
  (e.g. `thinking_effort: turbo`) raises `ConfigError` with the
  offending dot-path + valid options. The field is ignored on
  `kind: openai` providers.

- **Native Anthropic provider via `kind: anthropic`.** Each
  `llm.providers.<name>` entry now accepts a `kind` field selecting
  the wire protocol the provider speaks:
  - `kind: openai` (default — preserves every existing yaml byte-for-byte)
    routes through `langchain_openai.ChatOpenAI` against the configured
    `base_url`.
  - `kind: anthropic` routes through `langchain_anthropic.ChatAnthropic`
    against the configured `base_url`. The `base_url` is forwarded as
    `anthropic_api_url` so operators can target `api.anthropic.com`,
    an internal Bedrock-style proxy, FortiAI / fwb-aiserver fronts, or
    any other Claude-API-compatible endpoint without going through
    Anthropic's OpenAI-compat shim.

  Yaml example:

  ```yaml
  llm:
    default_provider: anthropic
    providers:
      anthropic:
        kind: anthropic
        base_url: https://api.anthropic.com
        api_key: sk-ant-...
        model_name: claude-sonnet-4-7
        thinking_enabled: true
        temperature: 0
        top_k: 64
  ```

- **`thinking_enabled: true` on Anthropic providers** translates to
  Anthropic's extended-thinking mode
  (`thinking={"type": "enabled", "budget_tokens": 8192}`) on the
  underlying `ChatAnthropic` constructor, with the SDK-required
  `max_tokens` floor automatically set above the budget.
- **Sampling-field mapping per kind** (see
  [openspec change](openspec/changes/add-anthropic-provider-kind/)
  design D4):

  | sampling field | openai | anthropic |
  |---|---|---|
  | `temperature`, `top_p` | top-level | top-level |
  | `top_k` | `extra_body["top_k"]` | top-level (Anthropic accepts it natively) |
  | `repetition_penalty` | `extra_body["repetition_penalty"]` | dropped + once-per-run WARNING |

  Operators with an existing yaml carrying `repetition_penalty` who
  switch a provider to `kind: anthropic` will see one warning per run
  per provider naming the dropped value, instead of silently pretending
  the parameter took effect.

- **`langchain-anthropic>=0.3` is now a hard dependency** of
  xauditor. Existing operators upgrading via `pip install --upgrade
  xauditor` pick it up automatically. Air-gapped operators on
  internal mirrors should ensure the wheel is mirrored alongside
  `langchain-openai`.

### Changed (operator-visible diagnostics)

- **`LLMConfig.__repr__` includes `kind=...`** so the redacted log
  representation surfaces the provider type. Existing log lines
  gain one new field; secret redaction (`api_key=***`) is unchanged.

### Validation

- `llm.providers.<name>.kind` is validated against the closed set
  `["openai", "anthropic"]` at config-parse time. A typo (e.g.
  `kind: antropic`) raises `ConfigError` with the offending provider
  name + the valid options listed in the message — config never
  silently falls through to "default" behavior.

### Tests

- `tests/config/test_provider_kind.py` — yaml parse for omitted /
  explicit / mixed-case / unknown `kind` values.
- `tests/test_model_factory_anthropic.py` — dispatch by kind,
  sampling-field mapping, `repetition_penalty` warning,
  `thinking_enabled` kwarg shape, missing-package preflight error.
- 18 new tests; 0 regressions on the existing OpenAI path
  (472 OK total).

### Migration

No migration required. Existing yaml without a `kind` field
continues to load as `kind: openai`. Operators who previously
routed through Anthropic's OpenAI-compat shim
(`base_url: https://api.anthropic.com/v1/`) keep working unchanged
unless they opt in to `kind: anthropic`.

## [0.5.0] - 2026-04-29 — breaking

> **Breaking release** — Phase 2 of
> [`make-postgres-the-canonical-sink`](openspec/changes/make-postgres-the-canonical-sink/).
> Migration steps for 0.4.x users are listed at the end of this entry.

### Removed (breaking)

- **`MarkdownReportSink` is gone.** No `<reports_dir>/<timestamp>/`
  directory is created during ``xauditor audit run`` / ``audit
  resume`` — the runtime no longer writes Markdown files. The
  Markdown shapes operators expect (`findings.md`, `false-positives.md`,
  `coverage-report.md`, `coder-results.md`) are now produced on demand
  by the new `xauditor audit export <run_id>` verb (see below).
- **`RunMeta.report_dir` is removed.** ``RunMeta`` now carries
  ``run_label: str`` (the per-invocation ``YYYYMMDD-HHMMSS`` slug).
- **`xauditor.reporting.resume_state` module is removed.** The
  on-disk ``<report_dir>/resume-state.json`` file is dead code in
  0.5.0; resume state lives in ``audit_runs.resume_state JSONB``
  in Postgres now.
- **`services.resolve_most_recent_resumable` is removed.** The
  resume target picker queries Postgres
  (``fetch_resume_target``) instead of walking
  ``.xauditor/reports/`` on disk.
- **The legacy ``ReportSinkBus(primary, secondaries=...)``
  constructor signature is removed.** The bus now wraps a single
  sink: ``ReportSinkBus(sink)``.

### Changed (breaking)

- **`services.run_audit` and `services.resume_audit` return
  `(AuditRun, run_label)`** instead of the prior
  ``(AuditRun, findings_path, coverage_path)``. CLI message updated
  to print the run label and recommend ``xauditor audit export
  <run_label>``.
- **Postgres is mandatory.** ``audit run`` / ``audit resume``
  pre-flight aborts before any LLM call when:
  - the configured Postgres endpoint is unreachable (recommends
    ``xauditor reportdb start`` / ``init``),
  - authentication fails (recommends reviewing
    ``reportdb.connection`` in ``xauditor.yml``),
  - the schema is below alembic revision
    ``0006_audit_runs_resume_state`` (recommends migration).
  No silent best-effort sink fallback — failing fast surfaces the
  setup gap immediately rather than 30 minutes into an audit.
- **Audit-resume reads from the DB column.** A run started on
  xauditor 0.4.x (``audit_runs.resume_state IS NULL``) cannot be
  resumed on 0.5.0; the resume command surfaces a clear error
  asking the operator to re-run from scratch with ``audit run``.

### Changed

- **`xauditor init` now also starts the portal.** Phase 2 makes
  Postgres mandatory; the portal is the canonical UI for triaging
  persisted runs, so it joins the umbrella init alongside graphdb /
  reportdb / coder. Operators who genuinely don't want the portal
  containers can still run the individual ``graphdb init`` /
  ``reportdb init`` verbs directly and skip ``init``.

### Added

- **`xauditor audit export <run_label>` CLI verb.** Reads only from
  the report database — does not touch the original repo or any
  on-disk run directory. Flags:
  - `--format json|markdown` (default `json`).
  - `--output-dir PATH` (required for `markdown`, ignored for `json`
    which goes to stdout).
  - `--include-debug` (off by default; adds operator-debug fields
    like `cli_exit_code`, `cli_stderr`).
  Output passes through `RuntimeLogger.redact` so configured
  secrets never leak into exported artefacts.
- **JSON envelope is versioned (`format_version: "1"`).** Top-level
  keys: ``format_version``, ``run`` (id / build / status / mode /
  totals / providers), ``findings`` (with `source_references`,
  `referenced_symbols`, optional `coder` payload), ``coverage``
  (modules / files / functions), ``validator_debates``. Naive
  consumers see a clean envelope; ``--include-debug`` adds a
  top-level ``debug`` object.
- **Markdown export** writes the four findings-derived files
  operators care about most: ``findings.md``,
  ``false-positives.md``, ``coverage-report.md``, and (when coder
  ran) ``coder-results.md``. **Known gap**: per-stage bundles
  (analyzer / validator / exploitation / no-findings / per-stage
  subagent transcripts / debate files) are deferred to a follow-up
  — they require unpacking ``path_raw_outputs.raw_markdown``
  payloads back into the renderer's ``shared_state`` shape.
- **`audit_runs.resume_state JSONB`** column (alembic migration
  ``0006_audit_runs_resume_state``). Stores per-run resumption
  state previously written to disk. The portal can read this
  column for "this run is resumable" badges.
- **`ReportSink.write_resume_state(payload)`** Protocol method.
  ``PostgresReportSink`` UPDATEs the JSONB column. The audit
  runtime fires this at every path-completion checkpoint and on
  cancellation so the resume mechanism stays current.
- **`RuntimeLogger.redact(text)`** public alias for ``_redact`` —
  used by the export verb.
- **`xauditor.reporting.exporter`** module — JSON envelope builder
  + Markdown bundle renderer + ``ResumeTarget`` lookup helpers.
- **`InMemoryReportSink`** test double + factory hooks
  (``ApplicationServices.report_sink_factory`` /
  ``resume_target_lookup`` / ``report_db_preflight``) so the
  test suite runs end-to-end without a live Postgres.

### Migrating from 0.4.x → 0.5.0

The four operator actions, in order:

1. **Install xauditor-portal alongside xauditor** (or update if you
   already had it). xauditor 0.5.0 imports the schema + sink from
   `xauditor_portal.sinks.postgres_sink` directly. ``xauditor`` will
   ImportError on first audit if `xauditor-portal>=0.4` is not
   importable.
2. **`xauditor reportdb init`** brings up the Postgres container and
   runs migrations, including the new
   ``0006_audit_runs_resume_state``. If Postgres was already up,
   `xauditor-portal migrate` (or whatever your alembic upgrade verb
   is) suffices.
3. **Replace MD-file CI scripts** that grep
   ``<reports_dir>/<timestamp>/findings.md`` with two-step:
   ``xauditor audit run && xauditor audit export $RUN_ID --format
   markdown --output-dir bundle/``. The CLI summary line on a
   successful run prints the run id.
4. **Finish or discard mid-flight 0.4.x runs.** A run started on
   0.4.x has no ``audit_runs.resume_state`` row; ``audit resume``
   on 0.5.0 refuses with a clear "started on a previous version,
   please re-run from scratch" message. ``audit run`` for fresh
   runs is unaffected.

### Deferred (future change)

- **`xauditor-report-store` package carve-out** (spec §5). Phase 2
  ships option A (xauditor depends on xauditor-portal directly);
  the package split into a separate `xauditor-report-store` layer
  is tracked as a follow-up openspec change so the audit kernel
  doesn't pull FastAPI / Next.js for users who don't run the UI.
- **Live-DB byte-equivalence drift test** for Markdown export
  (vs. 0.4.10 run-time output). Requires a CI-friendly Postgres
  fixture not yet in place.
- **Per-stage Markdown bundles** in ``audit export --format
  markdown`` (`analyzer-results.md`, `validator-results.md`,
  `exploitation-results.md`, `no-findings.md`, debate / subagent
  transcripts). The runtime persists everything needed
  (`path_raw_outputs.raw_markdown`); the export-side reconstruction
  is the missing piece.

## [0.4.11] - 2026-04-29

### Changed (run-time write pattern: O(N²) → O(N))

- **Streaming coder verdicts now use per-row UPSERT, not full-snapshot
  re-emit.** Phase 1 of the
  [`make-postgres-the-canonical-sink`](openspec/changes/make-postgres-the-canonical-sink/)
  spec. Previously, every coder settle in
  `AuditWorkflow._make_stream_callback` called `on_progress(snapshot)`
  which fanned out to `ReportSinkBus.emit_snapshot`, which in turn
  rewrote every Finding row + children for the run. For an N-finding
  run this was ~N × N = O(N²) Postgres UPSERTs over the lifetime of
  the run (~250 K UPSERTs at N=500). The streaming path is now
  per-row UPSERT, so the run-time write count drops to O(N) (≤ ~600
  UPSERTs at N=500, accounting for path-boundary snapshot emits).
  No operator-visible behaviour change beyond a noticeably faster
  portal during streaming verdict settlement.

### Added

- **`ReportSink.upsert_finding(run_id, finding)` Protocol method.**
  Every production sink now implements it.
  - `MarkdownReportSink.upsert_finding` debounces rapid bursts (50
    settles inside 100 ms) into a single rewrite of the three
    findings-derived files (`findings.md`, `false-positives.md`,
    `coder-results.md`) at most once per ~1 s — bounding rewrite
    frequency to ≤ 1 Hz regardless of settle rate. Coverage / per-stage
    / debate files keep updating only at path boundaries via
    `emit_snapshot`.
  - `PostgresReportSink.upsert_finding` runs as a single-row UPSERT
    against `findings` plus zero-or-one row UPSERTs against
    `coder_findings` and per-finding evidence; siblings on the same
    run are not touched.
- **`AuditWorkflow.run(..., on_finding_upsert=...)`** optional
  callback that fires per-finding-settle. When omitted, the legacy
  `on_progress(snapshot)` path is preserved for backward compatibility
  so external callers / unit tests do not break. `services.run_audit`
  and `services.resume_audit` pass `sink_bus.upsert_finding` as the
  callback so production runs take the per-row path.

### Tests

- `tests/reporting/test_sinks.py` adds three Bus tests
  (`upsert_finding` fan-out, secondary-swallow, primary-propagates)
  and two Markdown tests (debounce of 50 rapid calls into one render
  burst; `emit_snapshot` cancels a pending debounce).
- `tests/test_audit_workflow_coder.py` extends the cancellation
  regression test to assert `on_finding_upsert` fires (and the
  legacy `on_progress` path does NOT) for streamed settles.
- `tests/test_sink_perf.py` (skip-by-default; opt in with
  `XAUDITOR_RUN_PERF_BENCH=1`): drives 500 settles through the
  stream callback and asserts the resulting write shape is exactly
  500 per-row upserts and zero snapshot emits.
- `packages/xauditor-portal/tests/sinks/test_postgres_sink_integration.py`
  adds two live-DB tests for `upsert_finding` (single-row write
  without prior snapshot; sibling findings untouched after a
  per-row UPSERT against an emit_snapshot baseline).

### Notes for operators

- This is a backward-compatible patch. The Markdown report directory
  layout is unchanged. Postgres schema is unchanged. `audit run`
  flags are unchanged. The next breaking release (Phase 2 of the
  same spec) will drop the run-time Markdown sink and make Postgres
  mandatory; track progress in
  `openspec/changes/make-postgres-the-canonical-sink/tasks.md`.

## [0.4.10] - 2026-04-28

### Changed (portal "LLM providers" cosmetic)

- **Coder entry now reads `claude-code <version>, <model_name>`**
  instead of `claude-code <version> (Claude Code)`. Two changes:
  1. The `(Claude Code)` parenthetical from `claude --version`
     output is stripped — it duplicates our `claude-code` prefix.
  2. The model name from `coder.model_name` (yaml) is appended
     after a comma so operators can see both the runtime AND the
     model the runtime is talking to. For most non-Anthropic
     deployments the model name is the more important piece.

  Display now reads, e.g.:
  ```
  coder: claude-code 2.1.123, fortiai180-multimodal · auditor: qwen3-5 · validator: coder-next
  ```

  Falls back gracefully:
  - no version, no model → `claude-code`
  - version only → `claude-code 2.1.123`
  - model only → `claude-code, fortiai180-multimodal`
  - both → `claude-code 2.1.123, fortiai180-multimodal`

### Tests

- `tests/test_coder_preflight.py` adds three cases covering
  parenthetical-stripping, model-name appending, and the
  partial-input fallbacks.

## [0.4.9] - 2026-04-28

### Fixed (coder verifications were marked Inconclusive when the model wrapped its JSON in markdown fences)

- **Real-world claude-code 2.1.122 ignores "no Markdown" instructions
  in the system prompt** roughly half the time and wraps its JSON
  output in ```json ... ``` fences. `json.loads` chokes on the
  fences; `parse_coder_response` flagged every such response as
  `Inconclusive (transport error: invalid JSON)` even though the
  model's analysis was perfectly valid and complete. Operators saw
  good findings get downgraded to Inconclusive with no useful
  diagnostic text.

  Fix: `parse_coder_response` now runs every claude stdout through
  a new `_extract_json_object` helper that:

  1. Strips leading ```` ```json ```` (or plain ```` ``` ````) opener
     and trailing ```` ``` ```` closer if present.
  2. Walks brace pairs (string-aware: respects `"..."` and `\\`
     escapes) to locate the first complete `{...}` JSON object.
     Anything before the opening `{` (preamble like "Here is the
     analysis:") and after the matching `}` (postamble like "Let
     me know if you need clarification.") is dropped.
  3. Returns `None` when no balanced object is present (true
     refusal / pure-prose response — distinct case).

- **Two error reasons now, not one**:
  - `transport error: no JSON object in stdout` — case 3 above
    (model refused or returned pure prose). Operator should look at
    `coder_analysis` to see what claude actually said.
  - `transport error: malformed JSON` — extracted candidate that
    still failed `json.loads` (rare; corruption / truncation).

  The original `transport error: invalid JSON` is removed — its
  semantics were ambiguous between "JSON wrapped in fences" (now
  fixed transparently) and "no JSON at all" (now distinct).

- **System prompt strengthened** to repeat the "no markdown,
  first character `{`, last character `}`" rule and call out the
  consequence ("the orchestrator will flag your verdict as a
  transport error"). The lenient parser is the real fix; the
  prompt change is incremental defence-in-depth.

### Operator action required

Both `xauditor` (0.4.9) and `xauditor-coder-service` (0.2.3) carry
the matching parser fix:

```bash
pip install --upgrade xauditor-0.4.9-py3-none-any.whl
pip install --upgrade xauditor_coder_service-0.2.3-py3-none-any.whl
xauditor coder reset --yes
xauditor coder init     # rebuilds the worker container with 0.2.3
```

Re-run any audit whose findings are stuck at `Inconclusive
(transport error: invalid JSON)` — they will now produce real
verdicts.

### Tests

`tests/test_coder_agent.py::ParseCoderResponseTests` adds five
regression tests:

- `test_markdown_fenced_json_is_extracted` — the exact format
  observed in production (the user's report that drove this fix).
- `test_preamble_before_json_is_tolerated` — "Here is the
  analysis:" prefix.
- `test_nested_braces_in_strings_do_not_break_extraction` —
  string-aware brace walking.
- `test_no_json_in_stdout_yields_inconclusive` — pure prose /
  refusal (distinct from malformed-JSON case).
- `test_malformed_json_after_extraction_yields_inconclusive` —
  the new `malformed JSON` reason.

The vendored `parse_coder_response` in
`xauditor-coder-service._vendored` mirrors the same change; the
existing drift test asserts both copies produce identical results
across the full input matrix.

## [0.4.8] - 2026-04-28

### Fixed

- **Portal "LLM providers" header showed `coder: [object Object]`**
  for any run that included a coder verification stage. The backend
  was writing `RunMeta.llm_providers_used["coder"]` as a structured
  dict (`{cli_command, resolved_path, version}`) while the auditor
  and validator entries were bare model-name strings. The portal
  rendered each value via JS `String(v)` — strings rendered cleanly
  but `String({...})` returns `"[object Object]"`.

  Backend fix: `services.run_audit` now writes the coder entry as
  a flat string (`"claude-code <version>"` from a new
  `CoderPreflightResult.as_provider_summary()`), matching the shape
  of the other entries. The structured `as_provider_manifest()`
  method is preserved for any future caller that wants the full
  triple.

### Tests

- `tests/test_coder_preflight.py::test_as_provider_summary_returns_flat_string`
  asserts the new method returns a bare string (`"claude-code 2.1.122"`
  with version, `"claude-code"` without).

## [0.4.7] - 2026-04-28

### Fixed (cancellation race for in-flight coder verifications)

- **A worker thread whose claude subprocess was just SIGTERM'd by
  `cancel_all` was racing the cancel handler.** The worker's
  future settled with `Inconclusive (transport error: exit -15)`
  AFTER `_mark_pending_coder_skipped` had set the finding to
  `Skipped (cancelled by user)`, and the late-firing `on_settled`
  callback overwrote the Skipped state. Operators viewing the
  cancelled run on the portal saw a confusing mix: some findings
  showed "Skipped — cancelled by user", others showed
  "Inconclusive — transport error: exit -15", for the same
  cancelled run. The run-level `status='cancelled'` was correct,
  but the per-finding state was inconsistent.

- **`AuditWorkflow._cancelling` is a new `threading.Event`** that
  the cancel handler sets BEFORE issuing `cancel_all`. The
  per-finding stream callback (added in 0.4.1) now checks this
  flag at entry and snaps any incoming result to `CoderResult(
  status="Skipped", reason="cancelled by user", ...)` regardless
  of what the worker actually produced. Cancellation now wins
  the race deterministically: every in-flight verification ends
  in the cancelled run as `Skipped (cancelled by user)`, never
  as a transport-error Inconclusive.

### Tests

- New `tests/test_audit_workflow_coder.py::CoderCancellationTests::test_late_worker_callback_after_cancel_does_not_overwrite_skipped`
  simulates the race directly: builds a stream callback, sets
  `_cancelling`, fires it with an Inconclusive result mimicking
  the post-SIGTERM worker. Asserts the finding ends as Skipped
  with `coder_reason="cancelled by user"` and that the worker's
  "transport error" reason does NOT leak into the finding.

### Operator-visible effect

After upgrading and rebuilding the container, the next time an
audit is cancelled mid-run, the portal will show ALL coder
findings as `Skipped (cancelled by user)` — no more
inconsistent display. The run header continues to show
`Cancelled` correctly (already worked in earlier versions).

```bash
pip install --upgrade xauditor-0.4.7-py3-none-any.whl
xauditor coder reset --yes && xauditor coder init    # only needed if upgrading from 0.4.4 or earlier; the cancel fix is xauditor-side
```

## [0.4.6] - 2026-04-28

### Fixed (claude was hanging on container startup; 0.4.5 had RAM-leak)

- **Claude Code 2.x writes session state to `~/.claude/` and hangs
  silently when that directory can't be created.** Our worker
  container runs with `--read-only` rootfs and only `/tmp` was
  writable; claude tried to `mkdir /home/coder/.claude/...`,
  hit `EROFS`, and blocked forever.
- **0.4.5 fixed this with a tmpfs at `/home/coder` (128 MiB).**
  That patch is **withdrawn** (see "Reverted" below): tmpfs is
  RAM-backed, so under sustained load claude's session state
  pages would accumulate in host memory until OOM.
- **0.4.6 uses a docker NAMED VOLUME instead.**
  `build_run_argv` now mounts `<container_name>-home:/home/coder`.
  - Disk-backed: no RAM pressure regardless of campaign length.
  - Persisted across `coder stop` / `coder start` (cheap warm-start
    of any cached claude state, e.g. agents already discovered).
  - Wiped by `coder reset --yes`: `CoderRuntimeManager.reset_runtime`
    now issues `docker volume rm <container_name>-home` after the
    container removal step. Reset still tolerates the volume
    being absent (no error if `coder init` was never run).

### Reverted (0.4.5)

- The tmpfs `/home/coder` mount is removed. 0.4.5 should be
  skipped — operators on 0.4.4 jump directly to 0.4.6, operators
  on 0.4.5 upgrade and rebuild the container so the tmpfs is
  replaced by the named volume.

### Operator action required

Whether on 0.4.4 (still hanging) or 0.4.5 (still hanging or
slowly leaking RAM):

```bash
pip install --upgrade xauditor-0.4.6-py3-none-any.whl
xauditor coder reset --yes      # cleans container + image + volume
xauditor coder init              # recreates with the named volume
```

Verify:

```bash
# The mount is on disk, not RAM
docker exec xauditor-coder-service mount | grep /home/coder
# Expect: /dev/... on /home/coder type ext4 ...   (NOT 'tmpfs on ...')

# The volume exists and is owned by docker
docker volume ls | grep -- "-home$"
# Expect: local   xauditor-coder-service-home

# claude no longer hangs on startup
docker exec -i xauditor-coder-service claude -p --effort low "say OK only"
```

### Tests

- `tests/test_coder_runtime.py::BuildRunArgvTests::test_home_coder_is_named_volume_for_claude_state`
  replaces the 0.4.5 tmpfs assertion. Asserts:
  - `/home/coder` is NEVER backed by tmpfs.
  - Exactly one `--volume <name>:/home/coder` mount is present.
  - The volume name is derived from the container name
    (`<container_name>-home`).
- `tests/test_coder_runtime.py::CoderRuntimeManagerLifecycleTests::test_reset_removes_home_named_volume`
  asserts `docker volume rm <container_name>-home` is issued
  during `reset_runtime` and that the volume name appears in the
  returned `deleted` list.

## [0.4.5] - 2026-04-28 — WITHDRAWN

This version shipped a tmpfs at `/home/coder` to give claude a
writable HOME under `--read-only` rootfs. The tmpfs idea was
correct for cleaning up state across restarts, but tmpfs is
RAM-backed: a long-running coder service would gradually
exhaust host memory as claude session state grew. **Skip this
version.** Upgrade directly to 0.4.6, which uses a
disk-backed named volume instead.

## [0.4.4] - 2026-04-28

### Changed (architecture cleanup — supersedes the 0.4.2 / 0.4.3 churn)

- **Claude's three driving values now live in the worker container's
  env, not in the per-request HTTP body.** Verified against actual
  claude-code 2.1.122 (`strings /usr/local/bin/claude` plus `claude
  --help`):

  | xauditor config | claude env var | how it's set |
  |---|---|---|
  | `coder.model_api_key` | `ANTHROPIC_API_KEY` | baked into container by `build_run_argv` at `coder init` time |
  | `coder.model_url` | `ANTHROPIC_BASE_URL` | baked into container by `build_run_argv` at `coder init` time |
  | `coder.model_name` | `ANTHROPIC_MODEL` | baked into container by `build_run_argv` at `coder init` time |
  | `coder.thinking_effort` | (CLI flag `--effort`) | per-request via HTTP body |

- **`HttpCoderTransport`'s `claude_args` HTTP body shrinks** to
  `{"thinking_effort": ...}` — no more `model_url`, `model_name`, or
  `model_api_key` fields. Service workers no longer read them from
  the request; they inherit from container env.

- **`build_cli_argv` reduces to two flags**: `-p` (always) and
  `--effort <level>` (when configured). The `model_name` /
  `model_url` kwargs are removed from the function signature.
  Regression test asserts they raise `TypeError` if reintroduced.

### Reverted (vs 0.4.2)

- **`coder.model_api_key` IS now baked into the container env again.**
  0.4.2 removed this for a security argument (don't persist the
  secret in docker daemon state). That argument no longer holds
  with claude-code 2.x: `model_url` and `model_name` MUST go
  through env (no CLI flag exists for the base URL), so the secret
  is not the ONLY thing in `docker inspect --format
  '{{.Config.Env}}'` — it's just one of three values that all live
  there. Trying to keep ONE of them out for security while the
  other two are baked is incoherent.
- **Trade-off accepted**: rotating the API key requires
  `xauditor coder reset --yes && xauditor coder init` rather than a
  yaml edit. The simpler architecture (no per-request plumbing of
  three values through HTTP body and subprocess env) outweighs the
  rotation friction at our current scale.

### Added (defence-in-depth, "no more guessing")

- **Dockerfile build-time assertions** in
  `xauditor_coder_service/docker/Dockerfile`. After
  `npm install -g @anthropic-ai/claude-code`, the build validates:
  - `claude --help` lists `-p, --print` and `--effort <level>` flags.
  - The binary contains the env-var strings `ANTHROPIC_API_KEY`,
    `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL`.

  If any check fails, `docker build` exits non-zero with an
  identifiable `CLAUDE-FLAG-CHECK:` / `CLAUDE-ENV-CHECK:` prefix so
  the operator knows immediately a future claude-code version
  renamed something. This catches the exact class of bug that
  caused 0.4.2 / 0.4.3 to ship with `--thinking-effort` /
  `--model-name` / `--model-url` flags that claude 2.x doesn't
  accept.

### Operator action required

Upgrade both `xauditor` (0.4.4) and `xauditor-coder-service`
(0.2.2) together — the wire format change is bilateral.

```bash
pip install --upgrade xauditor-0.4.4-py3-none-any.whl
pip install --upgrade xauditor_coder_service-0.2.2-py3-none-any.whl
xauditor coder reset --yes
xauditor coder init     # rebuilds with the new Dockerfile + bakes env
docker exec xauditor-coder-service env | grep ANTHROPIC
# Expect three lines: ANTHROPIC_API_KEY=..., ANTHROPIC_BASE_URL=...,
# ANTHROPIC_MODEL=...
```

## [0.4.3] - 2026-04-28

### Fixed (CRITICAL — coder verifications were silently failing)

- **`build_cli_argv` was emitting flags Claude Code 2.x does not
  accept**, causing every coder verification to fail with
  `error: unknown option '--thinking-effort'` (or similar) on
  stderr, exit non-zero, and surface as `coder_status:
  "Inconclusive"` with `coder_reason="transport error: exit N"`.
  Operators inspecting only the high-level status would mistake
  these for "the model couldn't reach a verdict" rather than "the
  CLI rejected our argv".

  Three flag names were wrong, verified against `claude --help`
  output from claude-code 2.1.122:

  | xauditor was emitting | Claude Code 2.x actually accepts |
  |---|---|
  | `--thinking-effort high` | `--effort high` |
  | `--model-name claude-...` | `--model claude-...` |
  | `--model-url https://...` | (no flag — env var only) |

  All three are now corrected. The base URL was never expressible
  as a CLI flag in claude code 2.x; it goes through the
  `ANTHROPIC_BASE_URL` environment variable. The xauditor wire
  format (YAML keys `coder.thinking_effort` / `coder.model_name` /
  `coder.model_url` and the HTTP request body's
  `claude_args.{thinking_effort,model_name,model_url}`) is
  unchanged — only the translation to claude argv / env inside
  `build_cli_argv` and the transport's env construction changed.

- **`build_cli_argv` now always emits `-p` (`--print`)** for
  non-interactive print-and-exit mode. The worker pipes JSON into
  stdin and reads JSON from stdout; without `-p` claude's stdin
  handling is fuzzy across versions. The flag forces the
  documented contract.

- **`ENV_ALLOWLIST` adds `ANTHROPIC_BASE_URL`** so it passes
  through `scrub_environment` from the parent shell when set.
  `ClaudeCodeCliTransport.invoke` (subprocess transport) writes
  `env["ANTHROPIC_BASE_URL"] = self.config.model_url` when
  configured, taking precedence over the parent-shell value.

### Operator action required

If your `~/.xauditor/xauditor.yml` has `coder.transport: "http"`:
- Upgrade `xauditor-coder-service` to 0.2.1 (which carries the
  matching fix on the worker side).
- `xauditor coder reset --yes && xauditor coder init` to rebuild
  the container against the 0.2.1 image.
- Re-run any audit whose findings are stuck at `Inconclusive` —
  they should now produce real verdicts.

If your config has `coder.transport: "subprocess"`:
- Upgrade `xauditor` to 0.4.3.
- No container actions needed.
- Re-run audits.

### Tests

- `tests/test_coder_agent.py::BuildCliArgvTests` updated:
  - `test_minimal` now asserts the bare argv is `["claude", "-p"]`.
  - `test_with_flags` asserts `--effort` and `--model` (not
    `--thinking-effort` / `--model-name`).
  - New `test_does_not_accept_model_url_kwarg` regression guard:
    passing `model_url=` to `build_cli_argv` now raises
    `TypeError` (the kwarg is removed because there is no such
    flag).
- `tests/test_coder_service_vendor_drift.py` updated to mirror
  the new signature (`model_url` kwarg dropped from the test
  matrix).

## [0.4.2] - 2026-04-28

### Fixed (security)

- **`coder.model_api_key` is no longer baked into the coder
  container's environment at creation time.** Previously
  `xauditor.integrations.coder.helpers.build_run_argv` injected
  `--env ANTHROPIC_API_KEY=<key>` when the operator's yaml set
  `coder.model_api_key`, which persisted the secret into docker
  daemon state (visible via `docker inspect xauditor-coder-service
  --format '{{.Config.Env}}'`) and required `xauditor coder reset
  --yes && coder init` to rotate the key.

  The secret has always also travelled per-request via the HTTP
  transport's `claude_args.model_api_key` body field, where the
  service worker writes it into the spawned `claude` subprocess's
  env for the duration of one verification — that's the only
  place the secret needs to exist. The container-level injection
  was redundant and exposed the key to longer-lived state.

  After this fix:
  - The container env no longer contains `ANTHROPIC_API_KEY`.
  - Per-request injection still works exactly the same — yaml
    keeps `coder.model_api_key`, the HTTP body carries it, the
    worker writes it into subprocess env transiently.
  - Rotating the key is now a yaml edit + `audit run`; no
    container rebuild needed.
  - Existing operators upgrading from 0.4.1 SHOULD run
    `xauditor coder reset --yes && xauditor coder init` once to
    drop the previously-baked secret from their running
    container's env.

### Tests

- New regression test
  `tests/test_coder_runtime.py::BuildRunArgvTests::test_model_api_key_is_not_baked_into_container_env`
  asserts neither the literal `ANTHROPIC_API_KEY` env-var name nor
  any configured key value appears in the `docker run` argv.

## [0.4.1] - 2026-04-28

### Added (`stream-coder-results-per-finding`)

- **Coder verdicts now stream to report sinks per finding instead of
  per path.** When a coder verification settles, its result lands in
  the Markdown artefact and the portal Postgres immediately — no
  waiting for the next per-path drain in the audit main loop. Portal
  users see ``coder_status`` flip from ``Pending`` → terminal as each
  Claude Code subprocess returns.
- ``CoderDispatcher.submit`` now accepts an optional ``on_settled``
  callback. When provided, the dispatcher registers
  ``Future.add_done_callback`` so the callback fires the moment the
  task settles (typically from a worker thread). The audit workflow
  uses this hook to update the in-place ``findings`` list and emit a
  fresh snapshot through ``ReportSinkBus.emit_snapshot``.
- ``ReportSinkBus`` gains a process-local ``threading.Lock`` so
  worker-thread snapshot emissions serialise against main-thread
  per-path emissions. Sink fan-out (Markdown + Postgres) now sees one
  writer at a time regardless of which thread initiated the call.

### Changed

- The legacy poll-driven path (``CoderDispatcher.poll_completed`` /
  ``drain``) is unchanged at the API surface but is now mostly empty
  in practice — the per-finding callback usually pops tasks first via
  the dispatcher's ``_lock``-guarded ``_pending`` map. The poll path
  still serves as a defensive backstop for callers that don't pass
  ``on_settled`` (none exist in xauditor itself).

### Operator notes

- **Performance**: ``ReportSinkBus.emit_snapshot`` is called more
  often (per-finding instead of per-path). The Markdown sink rewrites
  the artefact each time; the Postgres sink re-syncs every finding
  (``_upsert_finding`` per row). For a 100-finding run this is
  ~10K UPSERTs total — sub-second on modern Postgres but worth
  monitoring on very large runs (1000+ findings).
- **Backward-compatible**: existing report sinks see the same
  ``open_run`` → ``emit_snapshot`` × N → ``close_run`` shape; only
  the cadence increased. No schema change, no API change at the sink
  layer.
- **Test fakes**: ``_FakeDispatcher`` test doubles in the audit-side
  test suite now defer ``on_settled`` callbacks until the next
  ``poll_completed`` / ``drain`` call (mirroring how real worker
  threads run after the workflow has appended the finding). Custom
  fakes elsewhere should follow the same pattern.

## [0.4.0] - 2026-04-28

### Changed (breaking) (`align-portal-lifecycle-with-coder`)

- **Portal lifecycle now mirrors the coder / graphdb / reportdb
  pattern.** Added `xauditor portal init` for first-time setup
  (build + create + start + healthcheck) and made `xauditor portal
  start` strict — it refuses if either container is missing and
  points operators at `portal init`. Previously `portal start` did
  the full first-time setup itself; that conflated two distinct
  operations.

  | service | build | init | start | stop | reset | status |
  |---|---|---|---|---|---|---|
  | portal | ✅ | ✅ ← **new** | ✅ (strict) | ✅ | ✅ | ✅ |
  | coder | ✅ | ✅ | ✅ (strict) | ✅ | ✅ | ✅ |
  | graphdb | — | ✅ | ✅ | ✅ | ✅ | — |
  | reportdb | — | ✅ | ✅ | ✅ | ✅ | — |

  All four runtime managers now share one mental model: `init` for
  first-time, `start` for restart after `stop`.

### Migration

- Operator scripts that ran `xauditor portal start` for first-time
  setup MUST switch to `xauditor portal init`. After the first
  `init`, subsequent `start` / `stop` cycles work as before.
- The umbrella `xauditor init` is unchanged at the user surface —
  it never invoked `portal_start` (portal is not part of the audit
  pipeline init), so no script sees a difference there.
- `PortalRuntimeManager.start_runtime()` now raises `PortalError`
  with `Portal container(s) missing: …` when called against a fresh
  install. Programmatic callers (rare) that relied on the auto-build
  behaviour SHOULD switch to `init_runtime()`.

## [0.3.4] - 2026-04-28

### Fixed

- **`xauditor coder init` no longer dies on the first transient probe
  error during container startup.** The retry loop in
  `CoderRuntimeManager._wait_until_healthy` only caught
  `(PreflightError, XAuditorError, OSError)`. The default
  `_default_coder_health_probe` raised raw `httpx.ConnectError` /
  `httpx.ReadError` (neither is an `OSError` subclass), so a single
  `Connection reset by peer` from a still-binding uvicorn — perfectly
  normal during the first ~1s after `docker run` — bubbled past the
  catch and killed the whole `init` even on otherwise-healthy
  containers. Two-part fix:
  1. `_default_coder_health_probe` now wraps `httpx.HTTPError` as
     `PreflightError` so the existing retry-catch tuple recognises it.
  2. `_wait_until_healthy` adds a defensive `except Exception` arm so
     any future probe implementation that raises a non-OSError
     transport error is still treated as "not ready yet" rather than
     fatal. Persistent failures continue to surface via the deadline-
     exhausted `XAuditorError`.

## [0.3.3] - 2026-04-28

### Changed (`align-coder-service-with-portal`)

- **`xauditor coder build` now mirrors `xauditor portal build` exactly.**
  Operators install both packages with `pip` and the build verb finds
  everything it needs through normal Python package introspection — no
  staging directories, no wheel files to drop on disk, no source
  checkout required.

  ```bash
  pip install xauditor xauditor-coder-service
  xauditor coder init     # build + start, zero cp
  ```

- **`resolve_coder_package` now goes through `importlib.metadata`.**
  Resolution mirrors `xauditor.integrations.portal.package`:
  `metadata.distribution("xauditor-coder-service")` plus
  `xauditor_coder_service.__file__` give the package root; the
  Dockerfile lives at `<package_root>/docker/Dockerfile`. The
  source-checkout path (`<repo>/deploy/coder-service/Dockerfile`) and
  the 0.3.2 wheel-bundled fallback under `xauditor/_data/coder_service/`
  are gone.
- **`CoderRuntimeManager._docker_build` no longer stages a tempdir.**
  It runs `docker build --tag <img> --file <pkg>/docker/Dockerfile <pkg>`
  directly against the resolved package directory; the Dockerfile uses
  `COPY .` to pick up the package source tree. The
  `_resolve_wheel_sources` and `_coder_wheels_dir` helpers are removed.
- **Backward incompatible (operator action required) — the
  `~/.xauditor/coder-wheels/` directory introduced in 0.3.2 is no longer
  used.** Operators upgrading from 0.3.2 should run
  `pip install xauditor-coder-service` and then re-run
  `xauditor coder init`. The previous wheel-staging directory can be
  deleted.

### Removed

- `tools/build_xauditor_wheel.sh` — bare `uv build` is sufficient again.
  The 0.3.2 staging step (copying the Dockerfile into
  `src/xauditor/_data/`) was a workaround for the wheel-only build path
  which is no longer needed.
- `xauditor/_data/coder_service/` package data entry from
  `pyproject.toml`; the Dockerfile lives in the
  `xauditor-coder-service` wheel exclusively now (mirrors how the portal
  Dockerfiles live in the `xauditor-portal` wheel).

## [0.3.2] - 2026-04-28

### Fixed (superseded by 0.3.3)

- **`xauditor coder build` now works under wheel-only installs.** The
  build verb previously assumed a source checkout — it could only find
  the Dockerfile at `<repo_root>/deploy/coder-service/Dockerfile`, and
  it expected the operator to have manually staged
  `deploy/coder-service/wheels/*.whl` before any `docker build`. Both
  assumptions broke for users who installed xauditor from a wheel.

### Changed (superseded by 0.3.3)

- The xauditor wheel bundled the coder-service Dockerfile under
  `xauditor/_data/coder_service/Dockerfile`. `resolve_coder_package`
  preferred the source-checkout location when present and fell back to
  the bundled copy otherwise.
- `xauditor coder build` staged a tempdir build context with the
  Dockerfile + both wheels copied in, then ran `docker build` against
  that directory.
- A new wheel-resolution chain looked at `<repo>/dist/` (source
  checkout) and `<runtime.root_dir>/coder-wheels/` (wheel-only).
- Wheel build orchestration moved to `tools/build_xauditor_wheel.sh`.

  All of the above were replaced in 0.3.3 by alignment with the
  `xauditor-portal` package pattern (Dockerfile inside the
  `xauditor-coder-service` wheel itself).

## [0.3.1] - 2026-04-28

### Changed

- **`coder.endpoint` is now optional under `transport: http`.** When
  unset (or empty), xauditor SHALL default the endpoint to
  `unix://<runtime.root_dir>/coder.sock` so the minimum HTTP-transport
  config drops to a two-line block (`enabled: true`, `transport: http`).
  Operators with non-default deployments (remote URL, custom socket
  path) still set the explicit value.
- The startup `INFO` log line continues to show the resolved endpoint
  so operators (and SIEM rules) see exactly where xauditor connects,
  whether the value came from explicit config or the smart default.
- Backward-compatible: existing configs with explicit `coder.endpoint`
  keep working unchanged; subprocess transport is unaffected.

## [0.3.0] - 2026-04-28

### Added (`add-coder-http-microservice`)

- **HTTP transport for the coder verification stage** — `coder.transport: "http"`
  points xauditor at a long-lived `xauditor-coder-service` microservice
  that holds a warm pool of Claude Code workers. The default stays
  `"subprocess"`; this is purely additive for existing users.
- **`HttpCoderTransport`** in `xauditor.audit.coder` uses a synchronous
  `httpx.Client`, supports `unix://`, `http://`, and `https://`
  endpoints, long-polls `GET /verifications/{id}` at the configured
  cadence, and maps every transport failure to
  `coder_status: "Inconclusive"` with a per-error reason.
- **Pre-flight HTTP probe** (`check_coder_http`) replaces the CLI
  executability check when `transport: "http"`. Failure raises
  `PreflightError` directing the operator at the sidecar startup or
  the alternative `coder.transport: subprocess`.
- **One-line startup INFO log** — `Coder transport: http via <endpoint>
  (auth: enabled|disabled)` — emitted after pre-flight succeeds. The
  token value never appears in the line.
- **New configuration keys**: `coder.transport`, `coder.endpoint`,
  `coder.enable_auth` (explicit on/off, replaces implicit "token
  presence implies auth"), `coder.endpoint_token` (secret, redacted
  from `repr` and registered with the runtime logger redactor),
  `coder.poll_interval_seconds`, `coder.preflight_timeout_seconds`.
  All exposed via matching `XAUDITOR_CODER_*` env vars.
- **`xauditor` now has a direct `httpx>=0.27,<1.0` dependency** —
  previously transitive through `langchain-openai`, now explicit so
  the resolver can't surprise us with a major bump.

### Added (`add-coder-runtime-management`)

- **`xauditor coder` parent command** with six lifecycle sub-verbs
  (`build`, `init`, `start`, `stop`, `reset`, `status`), mirroring
  `xauditor portal`'s vocabulary so operators don't re-learn anything.
  All five mutating verbs refuse with a clear error when
  `coder.endpoint` resolves to a remote target — that lifecycle is
  delegated to your deployment layer. `coder status` is the documented
  exception: it works for both local AND remote endpoints.
- **`xauditor coder init`** is the canonical local-sidecar setup:
  build image (if missing) → create container → start → wait for
  `/health`. Idempotent against an already-running deployment.
- **Pre-flight container-state inspection** — when
  `coder.transport: "http"` AND endpoint is local, `audit run` and
  `audit resume` inspect the container BEFORE the `/health` probe.
  Stopped containers auto-start with a one-line INFO log; missing
  containers fail-fast with a `PreflightError` directing at
  `xauditor coder init`. xauditor never auto-builds during `audit run`
  (build can take minutes; it belongs behind an explicit `coder init`).
- **`xauditor init` umbrella extended** — runs `coder init` after
  `graphdb init` + `reportdb init` when `coder.enabled: true`,
  `coder.transport: http`, and the endpoint is local. Otherwise emits
  a one-line skip status naming the reason.
- **New configuration keys**: `coder.container_image`,
  `coder.container_name`, `coder.runtime_socket_path`,
  `coder.repo_mount_path`. All exposed via matching `XAUDITOR_CODER_*`
  env vars. The socket path auto-derives from `coder.endpoint` when
  it starts with `unix://`.
- **`xauditor.integrations.coder/`** new sub-package: `helpers.py`
  (endpoint classification + docker-run argv builder), `package.py`
  (Dockerfile resolver), `runtime.py` (`CoderRuntimeManager` +
  `InMemoryCoderRuntimeManager` test double).

## [0.2.0] - 2026-04-28

### Added (`add-coder-agent`)

- **Repo-global coder verification stage** — opt-in fourth audit agent
  that reads the entire repository to verify whether each finding
  produced by the analyzer / validator / exploiter chain holds up
  against sibling-module evidence (sanitizers, capability checks,
  control-flow guards). The transport is the [Claude Code
  CLI](https://docs.claude.com/en/docs/claude-code), which the host
  must have installed when the stage is enabled.
- **Asynchronous dispatch** — the coder runs on a bounded
  `ThreadPoolExecutor` (`coder.concurrency`, default 2) so per-path
  audit latency is unchanged. The audit run waits for outstanding coder
  tasks before reaching its terminal status.
- **Pre-flight CLI check** — `xauditor audit run` and `audit resume`
  fail-fast before path planning when `coder.enabled: true` and the
  configured `cli_command` is missing from `PATH`. The error names the
  binary, the configuration key, and how to disable the stage. The CLI
  version captured at pre-flight is recorded on the run.
- **New configuration block** under `coder:` with keys `enabled`,
  `cli_command` (string or list), `concurrency`, `thinking_effort`
  (`low | medium | high | xhigh | max`), `model_url`, `model_name`,
  `model_api_key` (secret, redacted from logs and `repr`),
  `request_timeout_seconds`, `working_directory`. Matching env
  overrides are `XAUDITOR_CODER_*`.
- **Four new persisted fields** on every `Finding`: `coder_status`
  (`Verified | Not Verified | Inconclusive | Skipped | Pending`),
  `coder_analysis`, `coder_reason`, `coder_call_chain_evidence`. These
  are present on every run regardless of `coder.enabled` so the
  Markdown / DB / portal contracts stay uniform.
- **New Markdown artifact** `coder-results.md` alongside the existing
  per-stage outputs. Each finding card in `findings.md` /
  `false-positives.md` gains a `### Coder Verification` block when the
  stage was enabled.
- **Cancellation semantics** — `Ctrl+C` terminates outstanding coder
  subprocesses, marks affected findings as `Skipped` with
  `cancelled by user`, and emits one final snapshot before the existing
  `UserCancelledError` propagates. The subprocess env is scrubbed to a
  small allowlist (`PATH`, `HOME`, `USER`, `LANG`, `TERM`,
  `ANTHROPIC_API_KEY`, plus `CLAUDE_*`); xauditor never propagates its
  own credentials into the subprocess.

## [Unreleased]

### Added (post-`refine-portal-reports-and-settings`)

- **`xauditor audit resume`** picks up a failed or cancelled audit run
  and finishes only the paths that did not complete. Accepts an optional
  `--run-id` (on-disk timestamp form); when omitted, auto-selects the
  most recent failed / cancelled run on disk. Optional `--build` flag
  is honoured as a sanity check against the resumed run's recorded
  fingerprint. Idempotent: re-running against a completed run prints
  "run already completed" and exits 0.
- **Fresh-run sub-verb**: `xauditor audit run` replaces bare
  `xauditor audit` as the canonical way to start a new audit. Both the
  bare form and `xauditor audit --build FP` still work for one release
  via a deprecation shim that dispatches to `audit run` with a one-line
  stderr warning.
- Durable on-disk run state at
  `.xauditor/reports/<timestamp>/resume-state.json` (atomic tempfile +
  rename on every snapshot). Carries the build fingerprint, run meta,
  full `AuditRun.shared_state`, and the latest recorded terminal
  status. Capped at 16 MB by default via
  `XAUDITOR_RESUME_STATE_MAX_BYTES`.
- The audit workflow accepts `skip_paths` + `prior_shared_state` so
  resume can carry already-completed paths forward without any LLM
  call. Per-stage partial resume (skip analyzer only / re-run validator
  only) is deferred to a follow-up.
- Portal: resuming a run flips the existing `report.audit_runs.status`
  from `failed` / `cancelled` back to `in_progress`, clears
  `completed_at`, and emits a `progress_events` row with
  `heartbeat_kind="resumed"`. Child rows (findings, coverage, debates,
  subagents) are left untouched. The portal's Audit Log sub-tab renders
  the `resumed` event with a distinct warning tone.

### Added (post-`add-web-portal`)

- **Project → Graph Build → Audit Run hierarchy** in the Report tab with
  stable shareable URLs, server-side pagination at every level, and a
  user-configurable page size per list (`10 / 25 / 50 / 100`, persisted
  in `localStorage`). New endpoints: `GET /api/projects`,
  `/api/projects/{key}/builds`, `/api/projects/{key}/builds/{fp}/runs`.
- **Inline per-finding Debate** in team-mode runs — removes the standalone
  Debates sub-tab; each finding card shows a folded `<details>` transcript
  of the validator debate for that finding. Backed by
  `GET /api/runs/{id}/findings/{fid}/debate`.
- **Per-file collapsible source snippets** on finding cards — source
  references are grouped by `file_path` into independent `<details>` blocks
  (first file open, rest folded). A missing-snippets amber notice surfaces
  when the API returns an empty array despite Markdown on disk having one.
- **Coverage pagination** — `GET /api/runs/{id}/coverage/summary` +
  paginated `modules` / `files` / `functions` endpoints with status +
  substring filters; frontend renders three independently-paginated cards.
  Legacy `/coverage` remains as a 500-row deprecated wrapper for one
  release.
- **Live auto-refresh** every 3 seconds on the Graph Build list, Audit
  Run list, Findings sub-view, and Coverage sub-view while the underlying
  rows are still `in_progress`. Polling stops at terminal status.
- **Feedback overlay on the run header** — Valid findings and False
  positives cards now show `base + added − removed = net` so reviewers
  see both the validator-base count and the deltas introduced by human
  labelling. Saving a label invalidates the run query so terminal runs
  update within one second.
- **LLM configuration in the Settings tab** — one card per named
  provider, a default-provider selector, per-agent override cards, and a
  read-only effective-config preview table. Sampling ranges validated
  client-side and re-enforced server-side; `api_key` is masked and
  strictly yml-only (server returns HTTP 400 if a client tries to write
  it). New endpoint: `GET /api/config/effective/llm`.
- **Finer PostgresReportSink coverage** — `emit_snapshot` now mirrors
  `FindingSourceReference`, `ReferencedSymbol`, `CoverageModule`,
  `CoverageFile`, `CoverageFunction`, `ValidatorDebate`, `NoFindingPath`,
  all three subagent-record tables, and `PathRawOutput`. Previously only
  `AuditRun` + `Finding` + `ProgressEvent` were written, which is why the
  UI used to show empty Coverage / Debate panels even after a completed
  audit.
- New Alembic migrations: `0002_project_index` (composite index on
  `audit_runs` for project / build aggregations + `report_dir` index
  used by the sink); `0003_coverage_status_indexes` (composite
  `(run_id, status)` indexes on all three coverage tables).

### Fixed

- **Each audit invocation now gets its own `AuditRun` row**. The sink
  previously upserted by `build_fingerprint`, so cancelling a run and
  starting a new one against the same graph build collapsed both onto a
  single row. The sink now keys on the timestamped `report_dir` that the
  runtime stamps per invocation, so on-disk artifacts and DB rows agree.
- **`emit_snapshot` fallback path** no longer silently picks the most
  recent run with the same fingerprint when `_run_id` is not yet set —
  that was the same pre-existing bug in reverse. It now creates a
  clearly-labeled orphan row and logs a warning instead of clobbering
  another run's data.

### Added

- **Optional web portal** (`xauditor-portal`, separately installable)
  that mirrors every Markdown artifact into PostgreSQL and serves a
  Next.js 14 UI with login, filterable finding hierarchy, live
  progress, and human feedback annotations for RL post-training.
- **Report database CLI** — `xauditor reportdb init | start | stop |
  reset --yes` manages a PostgreSQL container with the same ergonomics
  as `xauditor graphdb`.
- **Umbrella init** — `xauditor init` brings up both the graph
  database and the report database in one shot and honors remote-
  endpoint bypass for either side.
- **Portal runtime CLI** — `xauditor portal build | start | stop |
  reset --yes | status` orchestrates a two-container topology
  (backend FastAPI + frontend Next.js) joined by a managed docker
  bridge network `xauditor-portal-net`.
- **Remote database support** in `xauditor.yml`:
  `reportdb.remote.url` and `graph.db.remote.url` point xauditor at
  external instances. When set, the managed container lifecycle is
  skipped and the ops commands refuse to run against that database.
- **Dual-write report sinks** (`xauditor/reporting/sinks.py`) —
  `ReportSinkBus` fans out every snapshot to a primary `MarkdownReportSink`
  and optional secondaries. Markdown generation stays authoritative;
  portal-side writes are best-effort.
- **Progress heartbeats** — `AuditWorkflow.run` accepts an
  `on_heartbeat` callback; services rate-limits `progress`-kind events
  to once per 2 seconds while letting `started` / `stage_completed` /
  `finished` / `failed` pass through.
- **ORM schema** (inside `xauditor-portal`) — 21 SQLAlchemy 2.0
  Declarative models across four logically-separated PostgreSQL
  schemas (`report`, `config`, `auth`, `feedback`) plus Alembic env
  + initial migration that seeds the default `auditor / auditor`
  user when `auth.users` is empty.
- **Human-feedback export** — `GET /api/runs/{id}/feedback-export?format=jsonl`
  streams every finding paired with its latest annotation for
  downstream RL pipelines, with a conservative secret-redaction pass.
- **End-to-end test** — `tests/cli/test_portal_e2e.py`, gated by
  `XAUDITOR_PORTAL_E2E=1`, drives the full portal lifecycle against
  real Docker.
- Example `xauditor.yml` showcasing portal + remote endpoints:
  `docs/examples/xauditor-with-portal.yml`.
- `docs/portal.md` — architecture, yml-over-DB precedence, feedback
  export format, remote-DB guidance, operational notes.

### Changed

- `src/xauditor/integrations/docker.py` now exposes a reusable
  `ContainerLifecycleManager` + `ContainerLifecycleSpec`; the
  existing `DockerManager` is preserved as a backwards-compatible thin
  wrapper. `PostgresContainerManager` is built on the same primitives.
- `ApplicationServices` gains `reportdb`, `portal`, and corresponding
  lifecycle methods plus `init_all`. `for_testing` accepts a
  `portal_package_installed` flag so CLI tests can exercise both the
  install-hint path and the full lifecycle.
- New environment variables: `XAUDITOR_REPORTDB_{IMAGE,PASSWORD,PORT,DATABASE,REMOTE_URL}`
  and `XAUDITOR_GRAPHDB_REMOTE_URL`.

### Backwards compatibility

- Existing `xauditor.yml` files remain valid — every new block is
  optional and defaults match the old behavior.
- `xauditor audit` behaves identically when `xauditor-portal` is not
  installed; no warning is emitted unless a `reportdb` yml block was
  explicitly configured.
- All pre-existing CLI verbs (`graphdb`, `graph`, `audit`) are
  unchanged. 240 main-package tests + 44 portal tests pass.

### Known follow-ups

- Image sizes land above initial targets (backend 211 MB vs ~200 MB
  target; frontend 221 MB vs ~150 MB target). Alpine-based rebases
  will shave ~20–80 MB per image.
- Vitest + Testing Library component tests for the frontend are not
  in this drop; scaffolding lands alongside the frontend CI build in a
  follow-up change.
