# Changelog — xauditor-portal

This file tracks the version of the `xauditor-portal` package specifically.
Main-package changes (`xauditor`) live in `../../CHANGELOG.md`.

## [1.0.0] — 2026-05-02

Coordinated 1.0.0 release with `xauditor` and `xauditor-coder-service`.
Detailed change log lives in the root `CHANGELOG.md` under `[1.0.0]`.

## [0.6.0] — 2026-04-30

### Fixed

- **Run detail tile staleness.** The Duplicates and Unlabeled
  tiles, plus the Valid / FP breakdown nets, now update in real
  time on every feedback PATCH. Previously they were sourced
  from `audit_runs.{*}_findings` columns written only at ingest
  by `postgres_sink._sync_snapshot`, so reviewers triaging a
  completed run never saw their duplicate-marks reflected in the
  header counts.
- **Per-bucket duplicate subtraction.** A finding whose human
  label is `duplicate` is now subtracted only from the bucket
  matching its `validation_status`: a `Valid` (or
  `Partial Valid` / `Inconclusive`) finding marked duplicate
  decrements the Valid net; a `False Positive` finding marked
  duplicate decrements the FP net. The `FeedbackBreakdown`
  payload gains a `duplicates_in_bucket` field surfacing the
  per-bucket count.

### Added

- **`valid_rate` metric on the run detail page.** New tile
  next to Total candidates / Valid / FP / Duplicates /
  Unlabeled, displaying
  `final_valid / (total_findings − duplicates_total)` as a
  percentage with one decimal place. Renders `—` when the
  denominator is zero (run with no findings, or every finding
  marked duplicate).
- **Inline GUI hint** under the Duplicate radio in the
  feedback control: "Only mark as duplicate after confirming
  this is a real (valid) finding." The dashboard math handles
  LLM-FP-marked-as-duplicate correctly, but the convention
  keeps the rate metric meaningful for normal triage flows.

### Changed (semantic shift — see callout below)

- **`unlabeled_findings` semantic** changed from
  "LLM-Inconclusive remainder" (legacy: `total − valid − fp −
  duplicates`) to "findings without a human annotation"
  (review queue). For most runs the legacy semantic was
  always 0 because every LLM verdict fell into Valid or FP;
  the new semantic reports the count of findings still
  awaiting human triage. **Consequence**: the four tiles
  Valid / FP / Duplicate / Unlabeled MAY NOT sum to total —
  they measure orthogonal axes (LLM-side bucket adjusted by
  feedback vs. human-side review status). Callers that
  relied on the four-bucket sum-to-total invariant must be
  updated.
- **API authority shift**: `RunSummary.{valid_findings,
  false_positives, duplicate_findings, unlabeled_findings}`
  on every endpoint (`/api/runs`, `/api/runs/{id}`,
  `/api/projects/.../runs`) are now derived from a live SQL
  pivot on `feedback.finding_annotations`, not from the
  cached `audit_runs.{*}_findings` columns. The columns
  continue to be written at ingest as a fallback. List
  endpoints use one batched GROUP BY per page so cost stays
  bounded.

## [0.5.0] — 2026-04-30

### Added (additive — backward compatible API)

- **`duplicate` feedback label.** A fourth feedback state
  alongside `true_positive` / `false_positive` /
  `unlabeled`. When a reviewer marks a finding `duplicate`,
  they SHALL pick a canonical target finding from the same
  audit run; the picker UI scopes to same-run candidates,
  excludes self, and excludes any finding already labeled
  `duplicate` (single-level rule — no chains).
- **Schema migration `0007_duplicate_feedback_label`.**
  - `feedback.finding_annotations.duplicate_of_finding_id`
    UUID column with FK to `report.findings(id)`
    `ON DELETE SET NULL`.
  - CHECK constraint
    `ck_finding_annotations_duplicate_pointer` enforcing
    `(label = 'duplicate') ↔ (duplicate_of_finding_id
    IS NOT NULL)`.
  - `feedback.annotation_history.previous_duplicate_of` /
    `new_duplicate_of` columns mirroring the canonical
    pointer for full audit trail.
  - `report.audit_runs.duplicate_findings` integer count
    populated by `_sync_snapshot`.
  - Backfill is implicit — existing annotations satisfy
    the biconditional with `duplicate_of_finding_id IS NULL`.
- **API endpoints**:
  - `POST` / `PATCH /api/findings/{id}/feedback` accept
    optional `duplicate_of_finding_id`. The handler
    enforces same-run rule, self-reference rejection, and
    the single-level rule with HTTP 400 on violation.
  - New `GET /api/findings/{finding_id}/duplicates`
    returns the paginated reverse-query list (findings
    pointing AT the given finding as canonical).
  - `GET /api/findings/{id}` response gains
    `duplicate_of_finding_id` and a `duplicate_of`
    summary `{id, finding_id, name}` when set.
  - `GET /api/runs/{id}` and the run-list endpoints'
    `RunSummary` payload gains `duplicate_findings: int`.
- **Sink dashboard math** (per design D6):
  duplicate-labeled findings subtract from the LLM-side
  `valid` / `false_positives` counts and surface in the
  new `duplicate_findings` bucket. Operator-visible
  formula:
  `unlabeled = total - valid - false_positives - duplicates`.
- **UI**:
  - `FeedbackControl` adds a 4th radio button
    "Duplicate" that opens a `FindingPicker` modal.
  - New `FindingPicker` component — searchable modal
    listing same-run findings, excludes self + chained
    duplicates client-side.
  - `FindingCard` collapsed header chip renders
    "Duplicate of `<finding_id>`" when the label is
    `duplicate`; expanded body lazy-fetches
    `/duplicates` and renders a "Marked as duplicate by"
    sub-section listing incoming duplicates.
  - Filter bar adds `Duplicate` to the feedback-label
    `<select>` options.
  - Run detail page adds a `Duplicates` metric
    alongside Valid / FP / Unlabeled.

### Tests

- `tests/sinks/test_feedback_duplicate_label.py` (new, 5
  cases) — `LABELS` registry, `LABEL_DUPLICATE` constant
  value, `FeedbackIn` Pydantic model accepts the new
  optional `duplicate_of_finding_id` field. Live-DB
  integration tests for the CHECK constraint round-trip
  and the full PATCH guard suite are gated on a CI
  Postgres fixture (deferred).
- All 87 portal tests pass (was 82; +5 new).

### Operator-action smoke tests (deferred)

- Run alembic upgrade against an existing 0.4.x portal DB;
  verify the new column / CHECK / FK exists; verify
  existing annotations still pass the constraint.
- UI smoke: triage a 200-finding run, mark 5 as
  duplicates of one canonical, verify the dashboard
  `Duplicates` count = 5 and the canonical's expanded
  card lists all 5.
- D2 single-level guard: try to mark a canonical finding
  as a duplicate; verify the 400 surfaces inline.

### Out of scope (intentional)

- Cross-run duplicate detection (same-run only for this
  release; phase 2 follow-up if requested).
- Bulk duplicate marking (select N, mark all in one PATCH).
- Markdown export changes — the on-demand `audit export`
  verb continues to use validation_status; duplicate
  labels are a portal-side overlay.

## [0.4.1] — 2026-04-29

### Fixed

- **Run dashboard's "Unlabeled" metric was permanently 0.** The
  `_finding_is_valid` predicate in
  `xauditor_portal.sinks.postgres_sink` lumped
  `ValidationStatus.INCONCLUSIVE` into the valid bucket alongside
  `Valid` / `Partial Valid`. Since the `false_positives` bucket
  picked up `False Positive` and the LLM only produces those four
  statuses, `valid + false_positives == total` for every run, so
  the subtraction `unlabeled = total - valid - false_positives`
  was permanently zero. The `audit_runs.unlabeled_findings` row
  the FE reads was always 0; the run detail page's
  "Unlabeled" metric was visibly stuck.

  Fix: `_finding_is_valid` no longer includes `Inconclusive`.
  An LLM-inconclusive finding falls through subtraction into the
  `unlabeled` bucket, which matches the operator's mental model
  ("Unlabeled = the LLM couldn't decide → I need to look at it"
  — the actionable backlog).

### Tests

- `tests/sinks/test_finding_tally.py` (new, 7 cases) — predicates
  bucket every `ValidationStatus` enum value correctly; raw-string
  inputs work; the full-tally math leaves `unlabeled == 0` only
  when no path went `Inconclusive`.

## [0.4.0] — 2026-04-29 — breaking (paired with xauditor 0.5.0)

> **Breaking release** — paired with xauditor 0.5.0. See the main
> package CHANGELOG for the architectural rationale and the
> migration runbook. Highlights below cover only the portal-side
> impact.

### Added

- **`audit_runs.resume_state JSONB`** column (alembic migration
  `0006_audit_runs_resume_state`). The portal can READ this column
  for "this run is resumable" UI badges; the audit runtime owns
  WRITES.
- **`PostgresReportSink.write_resume_state(payload)`** — UPDATEs
  the new JSONB column from the run-time per-checkpoint hook.
- **`fetch_resume_target(config, run_label=...)`** module-level
  helper + `ResumeTarget` dataclass — used by ``xauditor 0.5.0``'s
  ``audit resume`` to resolve which run to resume (most recent
  failed / cancelled / in-progress, or a specific label).

### Changed (breaking)

- **`RunMeta.report_dir` is removed; `RunMeta.run_label` (str)
  replaces it.** The ``audit_runs.report_dir`` *column* keeps its
  name to avoid an unrelated rename migration but now stores the
  run-label string (the ``YYYYMMDD-HHMMSS`` slug) rather than a
  filesystem path. Third-party sinks that subclassed
  ``PostgresReportSink`` need to update field names.
- **The ``ReportSink`` Protocol gains
  ``write_resume_state(payload)``** (Phase 2 of
  make-postgres-the-canonical-sink). Third-party sinks must add
  the matching method to type-check against the updated Protocol.

### Tests

- `tests/db/test_migrations_structural.py` adds the linkage check
  for ``0006_audit_runs_resume_state`` → ``0005_coder_findings``.

## [0.3.2] — 2026-04-29

### Changed (per-row Postgres UPSERTs during coder streaming)

- **`PostgresReportSink.upsert_finding(run_id, finding)`** added so the
  audit runtime can write one Finding row at a time as coder verdicts
  settle, instead of rewriting every Finding + children on every
  settle (`emit_snapshot`). Pairs with `xauditor` 0.4.11 — see the
  main package CHANGELOG for the architectural rationale and the
  O(N²) → O(N) write-count reduction. The `emit_snapshot` path is
  still used at path boundaries and at run end to keep the
  ``coverage`` / ``validator_debates`` / ``no_finding_paths`` /
  subagent / ``path_raw_outputs`` mirror eventually consistent.
- The new method is part of the `ReportSink` Protocol so this version
  also bumps the structural protocol contract — third-party sinks
  must add a matching `upsert_finding` method to type-check against
  the updated Protocol.

### Tests

- `tests/sinks/test_postgres_sink_integration.py` gains two live-DB
  tests:
  - `test_upsert_finding_writes_one_row_without_full_snapshot` —
    verifies the streaming path can run before any
    ``emit_snapshot`` and lands a single Finding row, leaving
    sibling tables untouched.
  - `test_upsert_finding_does_not_touch_sibling_findings` —
    regression guard against the O(N²) write storm: after
    ``emit_snapshot`` seeds two findings, mutating only one via
    ``upsert_finding`` MUST leave the other Finding row + its
    source-reference rows byte-identical.

## [0.3.1] — 2026-04-28

### Fixed

- **Run-detail page's "LLM providers" header rendered
  `coder: [object Object]`** when the run had a coder verification
  stage. Root cause: backend used to write the coder entry as a
  structured dict; the page's previous `String(v)` rendering only
  worked for strings. Fixed in tandem with `xauditor` 0.4.8 which
  changes the backend to write a flat string. The frontend ALSO
  gains a defensive `formatProviderValue` helper so any future
  structured provider entry renders sensibly (preferring
  ``version`` / ``model`` / ``name`` fields, falling back to
  `JSON.stringify`) instead of `[object Object]`.

## [0.3.0] — 2026-04-28

### Added (`add-coder-agent`)

- **`report.coder_findings`** table — one row per `(run_id, finding_ref)`
  carrying the coder verdict (`status`, `analysis`, `reason`) plus
  operator-debug columns (`dispatched_at`, `completed_at`,
  `duration_ms`, `cli_exit_code`, `cli_stderr`). Unique index on
  `(run_id, finding_ref)`; supplementary index on `(run_id, status)`.
- **`report.coder_finding_evidence`** table — one row per call-chain
  evidence item (file path, function name, snippet, language, role,
  ordinal). Cascade-deletes with the parent `coder_findings` row.
- **Alembic migration `0005_coder_findings`** introduces both tables
  with idempotent `CREATE TABLE IF NOT EXISTS` so a partial manual
  migrate does not crash subsequent `xauditor reportdb init` calls.
- **PostgresReportSink** mirrors the coder verdict + evidence for every
  finding in the same transaction as the parent `report.findings` row.
  Evidence is delete-and-reinserted on every snapshot so a `Pending →
  Verdict` transition cannot leave stale rows. When `coder.enabled` is
  false, every finding still gets a `Skipped` row so the schema is
  uniform across runs.
- **API** — `GET /api/runs/{id}/findings` summary payload includes
  `coder_status` so the collapsed-card chip renders without a per-card
  detail fetch. `GET /api/findings/{id}` detail payload includes
  `coder_status`, `coder_analysis`, `coder_reason`, and the
  `coder_call_chain_evidence` array.
- **Frontend** — finding card renders a `Coder Verification`
  `<details open={false}>` block in the expanded body (after Source
  references, before the team-mode Debate section), plus a `Coder:
  <status>` chip on the collapsed-card header when the verdict is
  non-Skipped. Pending verdicts show a "Verification in progress"
  placeholder; the existing 3-second poll picks up the verdict
  transition without extra wiring.

## [0.2.4] — 2026-04-20

### Fixed

- **`0004_findings_path_fingerprint` migration is now idempotent.**
  The upgrade body switched from `op.add_column(...)` to `op.execute(...)`
  with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` (and the mirror
  `DROP COLUMN IF EXISTS` on downgrade). The previous non-idempotent
  form would crash the backend container with
  `psycopg.errors.DuplicateColumn` when the live schema already had
  the column but `alembic_version` still pointed at a pre-0004
  revision (for example after a partial manual migrate or a volume
  restored from an earlier snapshot). Because the backend's
  migrate-on-startup path was the first thing to run, the crash
  looped the container forever. The idempotent form heals the drift
  silently and lets startup proceed.

## [0.2.3] — 2026-04-19

### Changed

- **Backend image now runs Alembic migrations on container start.**
  `backend.Dockerfile`'s `CMD` runs `xauditor-portal migrate` before
  `exec uvicorn ...`, so a freshly-built image always advances the
  report DB to `head` before serving API traffic. Previously the
  portal backend only auto-migrated when the user explicitly re-ran
  `xauditor reportdb init` — any pending migration (e.g. newly
  shipped `0004_findings_path_fingerprint`) left the schema stale and
  `/api/runs/{run_id}/findings` returned 500 with the frontend
  surfacing "Failed to load findings." `XAUDITOR_PORTAL_DATABASE_URL`
  (already injected by the managed portal runtime) is the single
  source of truth for the migration target; callers running the image
  outside the managed runtime must set that env var. The managed
  runtime is single-instance, so no distributed lock is required;
  horizontally scaled deployments should serialize migrations
  externally.

## [0.2.2] — 2026-04-19

`add-audit-resume` · `fix-portal-finding-debate-linkage`

### Fixed

- **Validator-debate section now renders in the finding card.**
  Team-mode runs recorded validator debates under
  `shared_state["validator_debates"]` keyed by
  `<path_fingerprint>::f<idx>`, but `report.validator_debates.finding_ref`
  persisted that raw key while the portal compared it to
  `findings.finding_id = F-NNNN` — so `FindingSummary.has_debate` was
  always `false` and `GET /api/runs/{run_id}/findings/{finding_id}/debate`
  always returned 404. The sink now translates the per-path key to the
  owning `Finding.finding_id` before persisting, so the existing
  collapsible Debate section in the finding card renders for every
  finding that actually has a debate. The same translation applies to
  `validator_subagent_records.finding_ref` and
  `exploiter_subagent_records.finding_ref`.

### Changed

- `PostgresReportSink` honors a new `meta.resumed` flag on
  `RunMeta`: when a resume reuses an existing row, the sink sets
  `status="in_progress"`, clears `completed_at`, and emits a
  `progress_events` row with `heartbeat_kind="resumed"`. Child rows
  (findings, coverage, debates, subagents) are left untouched so the
  portal's Findings / Coverage / Debate views keep showing the partial
  results captured before interruption.
- The Audit Log sub-tab recognises the new `resumed` heartbeat kind
  and renders it with a distinct warning tone.
- `report.findings` now carries a nullable `path_fingerprint` column
  (migration `0004_findings_path_fingerprint`) so the sink can build
  the `{finding_fingerprint → finding_id}` translation map and so
  future queries have a durable anchor for per-path groupings. Legacy
  rows remain valid with `path_fingerprint = NULL`.

## [0.2.0] — 2026-04-19

`refine-portal-reports-and-settings`

### Added

- **Project → Graph Build → Audit Run navigation** in the Report tab —
  `GET /api/projects`, `/api/projects/{key}/builds`,
  `/api/projects/{key}/builds/{fp}/runs`; all four levels share a
  paginated-list shell with a per-list page-size selector backed by
  `localStorage`.
- **Per-finding validator debate** — `GET /api/runs/{id}/findings/{fid}/debate`
  returns the single debate for a finding; rendered inline as a folded
  transcript inside the finding card on team-mode runs (no separate
  Debates sub-tab anymore).
- **Per-file collapsible source code** — finding cards group source
  references by `file_path` into independent `<details>` blocks (first
  file open, rest folded). Amber regression notice when the API returns
  empty snippets for a finding that produced Markdown snippets on disk.
- **Coverage pagination** — `GET /api/runs/{id}/coverage/summary` plus
  paginated `modules` / `files` / `functions` endpoints; frontend renders
  three independently-paginated cards with status filter, substring
  search, and their own `localStorage`-backed page size. Legacy
  `/coverage` endpoint caps at 500 rows per category and sets
  `Deprecation: true`.
- **Live auto-refresh** (3s) on the Graph Build list, Audit Run list,
  Findings sub-view, and Coverage sub-view while underlying rows are
  still `in_progress`. Stops at terminal status.
- **Feedback overlay on the run header** — `RunDetail` response adds
  `valid_findings_breakdown` and `false_positives_breakdown` with
  `{ base, added_by_feedback, removed_by_feedback, net }` computed via a
  single LEFT JOIN over `feedback.finding_annotations`. UI renders a
  `MetricWithFeedback` card with explicit `base + N − M = net`
  secondary line.
- **LLM configuration in the Settings tab** — one card per named
  provider (`llm.providers.*`), `llm.default_provider` selector, four
  per-agent override cards, read-only effective-config preview table.
  `api_key` is masked and strictly yml-only; server rejects any PATCH
  targeting it with HTTP 400. Sampling-range validation on both ends.
- **Full sink coverage** — `PostgresReportSink.emit_snapshot` now
  mirrors `FindingSourceReference`, `ReferencedSymbol`, coverage
  (modules / files / functions), `ValidatorDebate`, `NoFindingPath`,
  all three subagent record tables, and `PathRawOutput`. Previously only
  runs, findings, and progress events were written, which is why the
  UI used to show empty Coverage / Debate panels after completed audits.
- Alembic migrations `0002_project_index` (project + report_dir
  indexes) and `0003_coverage_status_indexes` (`(run_id, status)`
  composite indexes on coverage tables).

### Fixed

- **One run per `xauditor audit` invocation.** The sink previously
  upserted by `build_fingerprint`, so cancelling a run and starting
  a new one against the same graph build reused the same DB row. The
  natural key is now `report_dir` (the timestamped directory the
  runtime creates per invocation), matching on-disk layout.
- The `emit_snapshot` fallback path no longer silently picks the most
  recent row with the same fingerprint; it creates a clearly-labeled
  orphan row and logs a warning instead of clobbering another run.

### Changed

- Run detail page slimmed to three sub-tabs: `Findings`, `Coverage`,
  `Audit Log`. The `analyzer` / `validator` / `exploitation` stage
  banners are retired (the same fields live inside each Finding card
  already). `debates-view.tsx` and `subagents-view.tsx` are removed.
- Overall audit progress percent is now shown on the run header for
  every status, not only `in_progress` — failed or cancelled runs
  keep the percent they reached.
- Legacy `/reports/{runId}` URL redirects to `/reports/runs/{runId}`
  to preserve external bookmarks.

### Backwards compatibility

- Existing `xauditor.yml` remains valid; no new required fields.
- `GET /api/runs/{id}/coverage` still works but is marked deprecated
  and capped at 500 rows per category.
- No DB schema change — only new indexes. Historical runs completed
  before this release will continue to show empty Coverage / Debates
  because their data was never mirrored; this is documented in the
  Settings tab About card.

## [0.1.0] — 2026-04-18

Initial release shipped by the `add-web-portal` change.
