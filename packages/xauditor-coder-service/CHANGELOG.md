# Changelog

All notable changes to `xauditor-coder-service` are logged here. The
xauditor CLI's own version lives in the repo-root `pyproject.toml`.

## [1.0.0] - 2026-05-02

### Changed (`support-nested-coder-projects` follow-up #3)

- **Project markers removed; every directory at every depth is a
  project.** The 0.4.0–0.4.2 implementation required a marker file
  (`.git`, `xauditor.yml`, or `.xauditor/`) at depth `> 1` for a
  directory to count as a project. Operators wanted predictable
  any-folder-is-a-project semantics: drop a tree under
  `coder.workspace_root` and address any subdirectory by its
  `/`-joined path relative to the root. The discovery walk now yields
  every directory at every depth (subject to the unchanged dotfile /
  `__`-prefix filter, dir-only check, symlink-loop break, and entry +
  time budgets). The `(marker-anchored)` annotation in `xauditor
  coder status` is removed — there is no marker / non-marker
  distinction to surface. **Operator-visible delta**: workspaces
  whose nested folders lacked markers (e.g. a tarball-extracted tree
  with no `.git`) now surface in `/projects` and become directly
  addressable by `coder.project_name`. Workspaces that were
  already marker-anchored continue to work without change. Plain
  build / vendor directories (`node_modules/`, `dist/`, `target/`)
  also surface in the listing — operators who find this noisy can
  prune those subtrees on the host or split the workspace.

## [0.4.2] - 2026-05-02

### Fixed (`support-nested-coder-projects` follow-up #2)

- **`GET /projects` no longer times out on slow filesystems.** On
  vmhgfs, 9p, NFS, and other high-latency mounts a single
  `os.scandir` can cost ~40 ms; even with the 0.4.1 marker-leaf fix,
  walking a workspace whose top-level dirs lack markers (e.g. a
  workspace that *is* a single repo whose top is `src/`, `tests/`,
  …) blew past the 5 s preflight timeout. The walk is now bounded by
  a wall-clock budget (default 3.0 s) in addition to the entry
  budget. Two-pass design guarantees that all depth-1 projects appear
  even when the budget is exhausted — the legacy flat-layout listing
  never regresses. When the budget is hit, the service logs a
  structured WARN naming the elapsed time and the workspace path,
  and the response includes whatever was found before the cut-off.
  Operators can either point `coder.workspace_root` directly at the
  parent of audit-able projects, run on a faster filesystem, or
  raise `coder.preflight_timeout_seconds` to give the walk more room.
- **Realpath called once per entry instead of multiple times.** The
  visited-set is now seeded with the resolved root so the root's
  realpath isn't recomputed for each yielded directory. Combined
  with the budget, this halves typical-case syscall pressure on
  slow filesystems.

## [0.4.1] - 2026-05-02

### Fixed (`support-nested-coder-projects` follow-up)

- **`GET /projects` no longer times out on workspaces with project
  internals.** The 0.4.0 walk recursed into yielded projects and
  burned the 10 000-entry budget on `src/`, `lib/`, `target/`, and
  similar directories inside a `.git`-marked repo, causing the
  `xauditor audit run` preflight to fail with a 5s read-timeout on
  any non-trivial layout. A directory carrying one of the project
  markers (`.git`, `xauditor.yml`, `.xauditor/`) is now treated as a
  **leaf**: its name is added to the listing and the walk does not
  descend into it. A depth-1 directory **without** a marker is still
  yielded (legacy flat-layout compat) AND descended (so a `legacy/`
  container can hold nested marker-anchored projects). The drift
  test in the xauditor repo asserts the upstream and vendored copies
  produce identical results on the same layouts.

## [0.4.0] - 2026-05-02

### Added (`support-nested-coder-projects`)

- **Nested project layouts.** `POST /verifications` and `GET /projects`
  now accept slash-separated project names (e.g. `team/repo`,
  `org/team/sub/repo`). The path-component validator accepts any
  `/`-joined sequence of components matching `^[a-zA-Z0-9._-]+$`,
  rejects `..` segments, leading/trailing `/`, double slashes, and
  oversized names (> 512 chars) with HTTP 400.
- **Workspace discovery walks without a depth cap.** A directory at
  depth 1 is yielded unconditionally (preserves legacy flat layouts);
  a directory below depth 1 is yielded only when it contains one of
  `.git`, `xauditor.yml`, or a `.xauditor/` directory. Symlinks are
  followed once via realpath; symlink loops are pruned. The 10 000
  per-call entry budget is the only stop condition.
- **`_validate_project_or_raise` switched to `is_relative_to`.** Any
  realpath under `/workspace` is accepted at any depth; traversal
  remains rejected at the format-validator stage (and again as
  defence-in-depth at the realpath stage).
- **Per-project `HOME` mirrors the nested name.**
  `HOME=/home/coder/team/repo` for nested project `team/repo`; the
  service `mkdir -p`s the path with mode `0700` on first use.
- **`walk_projects` vendored into `_vendored.py`.** Mirrors
  `xauditor.integrations.coder.projects_walk.walk_projects`; the drift
  test in the xauditor repo asserts behavioural parity.

### Operator notes

Existing flat layouts continue to work unchanged. To use nested
layouts, drop a `.git` (or `xauditor.yml` / `.xauditor/`) marker in
each nested project directory you want surfaced. No config change is
needed — the service-side walk is fully driven by the on-disk shape
under `coder.workspace_root`.

After upgrading both wheels run `xauditor coder reset --yes &&
xauditor coder init` so the worker container picks up the new
`/projects` walk and validator.

### Post-merge smoke checklist (recommended)

1. `xauditor coder reset --yes && xauditor coder init` to rebuild
   against the 0.4.0 wheel.
2. Drop a marker into a nested directory inside `coder.workspace_root`,
   e.g. `mkdir -p $WORKSPACE_ROOT/team/repo/.git` plus a flat
   `mkdir -p $WORKSPACE_ROOT/legacy/`.
3. `xauditor coder status` — projects line SHOULD list both
   `legacy` and `team/repo` and carry a `(marker-anchored)` annotation
   (the listing contains a nested entry).
4. `curl <endpoint>/projects` (with bearer token if auth is enabled) —
   SHOULD return both names sorted lexicographically.
5. Dispatch a verification with `project: "team/repo"` and confirm
   the worker runs with `cwd=/workspace/team/repo` and
   `HOME=/home/coder/team/repo`.

## [0.2.3] - 2026-04-28

### Fixed (paired with xauditor 0.4.9)

- **Vendored `parse_coder_response` no longer flags markdown-fenced
  JSON as a transport error.** Mirrors the new `_extract_json_object`
  helper from `xauditor.audit.coder`: strips ```` ```json ```` fences,
  walks balanced braces to skip preamble / postamble, distinguishes
  "no JSON object in stdout" from "malformed JSON" in the failure
  reason.
- The drift test in the xauditor repo
  (`tests/test_coder_service_vendor_drift.py`) asserts both copies
  produce identical results on the full input matrix.

### Operator action required

`xauditor coder reset --yes && xauditor coder init` after
upgrading both wheels — the worker container needs the 0.2.3
parser to actually use it.

## [0.2.2] - 2026-04-28

### Changed (architecture — paired with xauditor 0.4.4)

- **Worker no longer reads claude config from the HTTP request body.**
  `claude_args` shrank to just `{"thinking_effort": ...}`. The three
  driving values (`ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`,
  `ANTHROPIC_MODEL`) come from the worker container's env, baked at
  `xauditor coder init` time by xauditor's `build_run_argv`.
- **`build_cli_argv` reduces to `-p` + `--effort`**. Mirrors xauditor
  side; drift test confirmed.
- **`ENV_ALLOWLIST` adds `ANTHROPIC_MODEL`** so the container's
  baked env passes through `scrub_environment` to the spawned
  claude subprocess.

### Added

- **Dockerfile build-time guard** (`docker/Dockerfile`): after `npm
  install -g @anthropic-ai/claude-code`, asserts that `-p`,
  `--effort` flags and the three env-var names exist in the
  installed claude binary. A future claude-code release that
  renames any of them fails the build immediately rather than
  shipping silently-broken images.

## [0.2.1] - 2026-04-28

### Fixed (CRITICAL — coder verifications were silently failing)

- **Worker was passing flags Claude Code 2.x does not accept.**
  `build_cli_argv` (in the vendored `_vendored.py`) emitted
  `--thinking-effort` / `--model-name` / `--model-url` — none of
  which exist on claude-code 2.1.122. Verifications consistently
  failed with `error: unknown option '...'`, exit non-zero, and
  surfaced as `coder_status: "Inconclusive"` to xauditor.

  Fixed argv shape:
  - `--thinking-effort` → `--effort` (claude 2.x flag name)
  - `--model-name` → `--model`
  - `--model-url` removed entirely; the API base URL goes through
    `ANTHROPIC_BASE_URL` env var (claude 2.x has no flag for it).
    The worker now writes `env["ANTHROPIC_BASE_URL"] =
    claude_args["model_url"]` per request when the request body
    carries one.
  - `-p` (`--print`) is always added so claude runs in
    non-interactive print-and-exit mode, matching the worker's
    pipe-stdin / read-stdout contract.

- **`ENV_ALLOWLIST` (vendored) adds `ANTHROPIC_BASE_URL`** so the
  base URL also passes through `scrub_environment` from the
  worker's own env when set there (e.g. for operators preferring
  to bake it into the container instead of sending per-request).

### Operator action required

After upgrading: rebuild the coder container so the running
service has the fixed worker code:

```bash
xauditor coder reset --yes
xauditor coder init     # picks up the 0.2.1 wheel
```

### Tests

The xauditor-side `tests/test_coder_service_vendor_drift.py`
asserts behavioural parity between the vendored `build_cli_argv`
and `xauditor.audit.coder.build_cli_argv`. Both copies now emit
the corrected argv; drift test passes.

## [0.2.0] - 2026-04-28

### Changed (`align-coder-service-with-portal`)

- **Dropped the runtime dependency on `xauditor`.** The four imports
  the worker used (`build_cli_argv`, `parse_coder_response`,
  `scrub_environment`, `CODER_STATUS_INCONCLUSIVE`) are now vendored
  under `xauditor_coder_service/_vendored.py`. The container no longer
  needs xauditor to be installable — the build context can be the bare
  package source tree.
- **The Dockerfile now ships inside the package** at
  `xauditor_coder_service/docker/Dockerfile` plus a
  `docker/requirements.txt` mirror of the project deps. The build
  context is the installed package directory; `xauditor coder build`
  resolves it through `importlib.metadata` exactly the way
  `xauditor portal build` resolves `xauditor-portal`. There is no
  longer any `wheels/` subdirectory or external Dockerfile staging
  step.
- **`pyproject.toml` adds `[tool.setuptools.package-data]` for the
  Dockerfile, requirements, and `.dockerignore`.** The wheel ships
  these alongside the Python source so editable and wheel installs
  both expose the build context.

### Operator notes

- Drift between the vendored helpers and their xauditor source-of-truth
  is asserted by `tests/test_coder_service_vendor_drift.py` in the
  xauditor repo. If you change behaviour on either side, update both.
- Backward compatibility: existing service deployments don't change
  shape (HTTP endpoints, request/response payloads, container env vars
  are all identical). Operators rebuild the image once after upgrading
  xauditor to 0.3.3.

## [0.1.0] - 2026-04-28

### Added (`add-coder-http-microservice`)

- Initial release. FastAPI app with `POST /verifications`,
  `GET /verifications/{job_id}`, `DELETE /verifications/{job_id}`,
  `GET /health`. `asyncio.Semaphore` worker pool, in-memory job store
  with TTL reaper, optional bearer-token auth.
