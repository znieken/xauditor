"""Pre-flight checks that run before the audit pipeline starts.

Today this module hosts only the coder-CLI check, but it is the natural
home for any future "before path planning" verification (e.g. a future
GPU-availability probe or a license check).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from xauditor.audit.coder import build_cli_argv, scrub_environment
from xauditor.config import CoderConfig
from xauditor.errors import PreflightError

try:
    import httpx  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised when httpx absent
    httpx = None  # type: ignore[assignment]


_VERSION_PROBE_TIMEOUT_SECONDS = 10
_VERSION_OUTPUT_TRUNCATE_BYTES = 1024


@dataclass(frozen=True)
class CoderPreflightResult:
    """Outcome of the coder pre-flight check.

    ``cli_command`` is preserved as a tuple so the workflow's
    ``RunMeta.llm_providers_used["coder"]`` payload is JSON-serialisable
    by callers that re-use the same shape.
    """

    cli_command: tuple[str, ...]
    resolved_path: str
    version: str

    def as_provider_manifest(self) -> dict[str, object]:
        return {
            "cli_command": list(self.cli_command),
            "resolved_path": self.resolved_path,
            "version": self.version,
        }

    def as_provider_summary(self, *, model_name: str | None = None) -> str:
        """One-line display string for ``RunMeta.llm_providers_used``.

        Format: ``"claude-code <version>, <model_name>"`` when both
        pieces are known. The ``claude --version`` output looks like
        ``"2.1.123 (Claude Code)"``; the ``" (Claude Code)"`` suffix
        is stripped because it duplicates the ``claude-code`` prefix
        we add ourselves. The model name (from ``coder.model_name``
        in xauditor config) appears after a comma so operators can
        distinguish the runtime (claude-code) from the model the
        runtime is talking to (e.g. claude-sonnet-4-7,
        fortiai180-multimodal, etc.).

        Falls back gracefully:
          - no version, no model      → "claude-code"
          - version only               → "claude-code 2.1.123"
          - model only                 → "claude-code, fortiai180-multimodal"
          - both                       → "claude-code 2.1.123, fortiai180-multimodal"
        """

        # Strip the "(Claude Code)" suffix — it's redundant with our
        # "claude-code" prefix. Preserve any other parens that might
        # appear in non-standard claude builds by being conservative
        # about what we strip.
        version_raw = (self.version or "").strip()
        version_clean = version_raw
        paren = version_clean.find("(")
        if paren >= 0:
            version_clean = version_clean[:paren].strip()

        head = f"claude-code {version_clean}".strip() if version_clean else "claude-code"
        model = (model_name or "").strip()
        if model:
            return f"{head}, {model}"
        return head


def check_coder_cli(
    coder_cfg: CoderConfig,
    *,
    env: Mapping[str, str] | None = None,
) -> CoderPreflightResult | None:
    """Verify that the configured Claude Code CLI is executable.

    Returns ``None`` when ``coder.enabled`` is false (no probing is
    performed in that case). Raises :class:`PreflightError` when the CLI
    cannot be found or is not executable; the error message names the
    configured ``cli_command`` and the configuration key, and tells the
    operator either to install Claude Code or to set
    ``coder.enabled: false``.
    """

    if not coder_cfg.enabled:
        return None

    cli_command = tuple(coder_cfg.cli_command)
    if not cli_command:
        raise PreflightError(
            "coder.cli_command is empty. Configure a Claude Code CLI binary "
            "or set coder.enabled: false."
        )

    head = cli_command[0]
    search_path = None
    if env is not None and "PATH" in env:
        search_path = env["PATH"]
    resolved = _resolve_executable(head, search_path=search_path)
    if resolved is None:
        raise PreflightError(
            f"Configured Claude Code CLI `{head}` (coder.cli_command) was not "
            "found on PATH or is not executable. Install Claude Code "
            "(https://docs.claude.com/en/docs/claude-code) or set "
            "coder.enabled: false to disable the coder verification stage."
        )

    version = _probe_version(cli_command, resolved=resolved, env=env)
    return CoderPreflightResult(
        cli_command=cli_command,
        resolved_path=resolved,
        version=version,
    )


def _resolve_executable(head: str, *, search_path: str | None = None) -> str | None:
    if not head:
        return None
    if os.path.isabs(head):
        return head if os.path.isfile(head) and os.access(head, os.X_OK) else None
    return shutil.which(head, path=search_path)


def _probe_version(
    cli_command: Sequence[str],
    *,
    resolved: str,
    env: Mapping[str, str] | None,
) -> str:
    """Run ``<cli> --version`` and return the captured output.

    Failures (non-zero exit, missing binary, timeout) yield an empty
    string instead of raising — the version is informational metadata,
    not a gating signal.
    """

    argv = list(cli_command)
    # Replace the head with the absolute resolved path so we don't depend
    # on PATH lookup happening twice.
    argv[0] = resolved
    argv = build_cli_argv(argv) + ["--version"]
    scrubbed = scrub_environment(env)
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=scrubbed,
            timeout=_VERSION_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    raw = (completed.stdout or b"").strip()
    if len(raw) > _VERSION_OUTPUT_TRUNCATE_BYTES:
        raw = raw[:_VERSION_OUTPUT_TRUNCATE_BYTES]
    return raw.decode("utf-8", errors="replace")


def check_coder_http(
    coder_cfg: CoderConfig,
    *,
    env: Mapping[str, str] | None = None,
) -> CoderPreflightResult | None:
    """Probe the configured `coder.endpoint` `/health` endpoint.

    Returns ``None`` when ``coder.enabled`` is false. Raises
    :class:`PreflightError` on connect-refused / non-200 / timeout. On
    success captures the service-reported ``claude_cli_version`` and
    returns a ``CoderPreflightResult`` whose ``cli_command`` is
    ``("http",)`` and ``resolved_path`` is the configured endpoint —
    keeps the wire shape compatible with the subprocess path so
    downstream sinks (``RunMeta.llm_providers_used["coder"]``) get the
    same key set regardless of transport.
    """

    if not coder_cfg.enabled:
        return None
    if httpx is None:  # pragma: no cover
        raise PreflightError(
            "coder.transport: http requires the `httpx` package; "
            "install xauditor with its standard dependencies."
        )
    endpoint = (coder_cfg.endpoint or "").strip()
    if not endpoint:
        raise PreflightError(
            "coder.transport: http requires coder.endpoint to be set "
            "(e.g. 'http://127.0.0.1:8090', 'https://coder-pool.internal/', "
            "or 'unix:///var/run/xauditor-coder.sock')."
        )

    timeout = httpx.Timeout(
        connect=float(coder_cfg.preflight_timeout_seconds),
        read=float(coder_cfg.preflight_timeout_seconds),
        write=float(coder_cfg.preflight_timeout_seconds),
        pool=float(coder_cfg.preflight_timeout_seconds),
    )
    headers: dict[str, str] = {"Accept": "application/json"}
    # /health is unauth per spec; do NOT send the bearer token even if
    # one is configured. Accidentally sending it during a probe would
    # surface in service logs and provide no value.

    if endpoint.startswith("unix://"):
        socket_path = endpoint[len("unix://"):]
        client_kwargs: dict[str, Any] = {
            "base_url": "http://localhost",
            "transport": httpx.HTTPTransport(uds=socket_path),
        }
    else:
        client_kwargs = {"base_url": endpoint.rstrip("/")}

    try:
        with httpx.Client(headers=headers, timeout=timeout, **client_kwargs) as client:
            resp = client.get("/health")
    except httpx.ConnectError as exc:
        raise PreflightError(
            f"Coder service at `{endpoint}` (coder.endpoint) is not reachable: "
            f"{exc.__class__.__name__}: {exc}. "
            "Start the sidecar (`docker compose -f deploy/coder-service/docker-compose.coder.yml up`), "
            "check firewall / network policy, or set coder.transport: subprocess."
        ) from exc
    except httpx.TimeoutException as exc:
        raise PreflightError(
            f"Coder service at `{endpoint}` (coder.endpoint) did not respond "
            f"within {coder_cfg.preflight_timeout_seconds}s. "
            "Increase coder.preflight_timeout_seconds, check the service health, "
            "or set coder.transport: subprocess."
        ) from exc
    except httpx.HTTPError as exc:  # noqa: BLE001
        raise PreflightError(
            f"Coder service health probe at `{endpoint}` failed: "
            f"{exc.__class__.__name__}: {exc}."
        ) from exc

    if resp.status_code != 200:
        raise PreflightError(
            f"Coder service health probe at `{endpoint}` returned HTTP "
            f"{resp.status_code}: {resp.text[:200]}"
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise PreflightError(
            f"Coder service health response at `{endpoint}` was not JSON: "
            f"{resp.text[:200]}"
        ) from exc

    version = str(body.get("claude_cli_version", ""))
    return CoderPreflightResult(
        cli_command=("http",),
        resolved_path=endpoint,
        version=version,
    )


def resolve_coder_project(
    coder_cfg: CoderConfig, *, repo_root: Path
) -> str | None:
    """Derive the multi-project ``project`` name for an audit run.

    Returns ``None`` for the subprocess transport (which runs claude
    with ``cwd=repo_root`` directly and has no project namespace).
    Otherwise returns:

    1. ``coder.project_name`` when explicitly set (operator override
       always wins).
    2. The ``/``-joined path of ``repo_root`` relative to
       ``coder.workspace_root`` when both resolve cleanly and
       ``repo_root`` lies inside ``workspace_root``. With the
       every-directory-is-a-project discovery walk this matches the
       project name that ``GET /projects`` emits (e.g. ``team/repo``
       for ``workspace_root=/ws`` and ``repo_root=/ws/team/repo``).
    3. ``basename(realpath(repo_root))`` as the legacy fallback —
       used when ``workspace_root`` is unset (single-repo container)
       or when ``repo_root`` is not relative to ``workspace_root``
       (mis-configured layout; the error path in
       ``check_coder_projects`` then nudges the operator).

    The deprecation shim populates ``effective_project_name`` from the
    legacy ``repo_mount_path`` so case 1 still applies for old configs.
    """

    if not coder_cfg.enabled:
        return None
    if coder_cfg.transport != "http":
        return None
    explicit = (coder_cfg.effective_project_name or "").strip()
    if explicit:
        return explicit

    try:
        repo_real = Path(os.path.realpath(repo_root))
    except OSError:
        return Path(repo_root).name

    workspace_root = (coder_cfg.effective_workspace_root or "").strip()
    if workspace_root:
        try:
            workspace_real = Path(os.path.realpath(workspace_root))
        except OSError:
            workspace_real = None
        if workspace_real is not None and repo_real != workspace_real:
            try:
                rel = repo_real.relative_to(workspace_real)
            except ValueError:
                rel = None
            if rel is not None:
                return rel.as_posix()

    return repo_real.name


def check_coder_projects(
    coder_cfg: CoderConfig,
    *,
    project: str,
) -> None:
    """Probe ``GET /projects`` and validate membership of ``project``.

    Skipped silently when ``coder.workspace_root`` is unset (the
    legacy single-repo container has no project list to enumerate).
    Skipped silently when the project's path can be confirmed locally
    (i.e. ``<workspace_root>/<project>`` resolves to a real directory
    under ``workspace_root`` on the audit host's filesystem). Rationale:
    the ``/projects`` listing probe can give false negatives on slow
    filesystems (vmhgfs/9p shared mounts) where the service-side walk
    exhausts its time budget before reaching deep paths. The service's
    per-request validator (``_validate_project_or_raise``) catches any
    mismatch at dispatch time, and the workspace-mount mismatch case is
    already caught upstream by ``_verify_workspace_mount_or_raise``.
    Raises :class:`PreflightError` on any non-200, malformed body, or
    missing-project outcome with an actionable message naming the
    available projects and the two repair options.
    """

    if not coder_cfg.enabled:
        return
    if coder_cfg.transport != "http":
        return
    workspace_root = (coder_cfg.effective_workspace_root or "").strip()
    if not workspace_root:
        return

    # Local-FS trust check: skip the listing probe when the project
    # path is verifiable locally. See docstring.
    try:
        ws_real = Path(os.path.realpath(workspace_root))
        candidate_real = Path(os.path.realpath(ws_real / project))
        if (
            candidate_real != ws_real
            and candidate_real.is_relative_to(ws_real)
            and candidate_real.is_dir()
        ):
            return
    except (OSError, ValueError):
        pass

    if httpx is None:  # pragma: no cover
        raise PreflightError(
            "coder.transport: http requires the `httpx` package; "
            "install xauditor with its standard dependencies."
        )
    endpoint = (coder_cfg.endpoint or "").strip()
    if not endpoint:
        return  # check_coder_http already raised; defensive

    timeout = httpx.Timeout(
        connect=float(coder_cfg.preflight_timeout_seconds),
        read=float(coder_cfg.preflight_timeout_seconds),
        write=float(coder_cfg.preflight_timeout_seconds),
        pool=float(coder_cfg.preflight_timeout_seconds),
    )
    headers: dict[str, str] = {"Accept": "application/json"}
    if coder_cfg.enable_auth and coder_cfg.endpoint_token:
        headers["Authorization"] = f"Bearer {coder_cfg.endpoint_token}"

    if endpoint.startswith("unix://"):
        socket_path = endpoint[len("unix://"):]
        client_kwargs: dict[str, Any] = {
            "base_url": "http://localhost",
            "transport": httpx.HTTPTransport(uds=socket_path),
        }
    else:
        client_kwargs = {"base_url": endpoint.rstrip("/")}

    try:
        with httpx.Client(headers=headers, timeout=timeout, **client_kwargs) as client:
            resp = client.get("/projects")
    except httpx.HTTPError as exc:
        raise PreflightError(
            f"Coder service /projects probe at `{endpoint}` failed: "
            f"{exc.__class__.__name__}: {exc}. "
            "Confirm the service is up-to-date and that "
            "coder.endpoint_token matches the service's auth token."
        ) from exc

    if resp.status_code == 401 or resp.status_code == 403:
        raise PreflightError(
            f"Coder service /projects probe at `{endpoint}` returned HTTP "
            f"{resp.status_code}. The endpoint is auth-gated; set "
            "coder.endpoint_token in xauditor.yml to the matching service "
            "bearer token, or set coder.enable_auth: false on both ends."
        )
    if resp.status_code != 200:
        raise PreflightError(
            f"Coder service /projects probe at `{endpoint}` returned HTTP "
            f"{resp.status_code}: {resp.text[:200]}"
        )
    try:
        body = resp.json()
    except ValueError as exc:
        raise PreflightError(
            f"Coder service /projects response at `{endpoint}` was not JSON: "
            f"{resp.text[:200]}"
        ) from exc

    available = body.get("projects") if isinstance(body, dict) else None
    if not isinstance(available, list):
        raise PreflightError(
            f"Coder service /projects response at `{endpoint}` did not "
            f"contain a `projects` array: {body!r}"
        )

    if project not in available:
        raise PreflightError(
            f"Coder service has no project named \"{project}\". "
            f"Available projects: [{', '.join(str(p) for p in available)}]. "
            "The project name is derived from `repo_root` relative to "
            "`coder.workspace_root` (or `basename(repo_root)` when the "
            "two don't overlap). Either point `audit.repo_root` at a "
            f"subdirectory under `coder.workspace_root` ({workspace_root}), "
            "set `coder.project_name` in xauditor.yml to override the "
            f"derivation, or `mkdir {workspace_root}/{project}` on the "
            "host where the coder container runs."
        )


def check_coder_runtime(
    coder_cfg: CoderConfig,
    *,
    env: Mapping[str, str] | None = None,
    coder_runtime: object | None = None,
    runtime_logger: object | None = None,
    repo_root: Path | None = None,
) -> CoderPreflightResult | None:
    """Single entry point that picks the right pre-flight per transport.

    Used by ``services.run_audit`` and ``services.resume_audit`` so the
    rest of the codebase doesn't have to branch on ``coder.transport``.

    When ``coder_runtime`` is provided AND the transport is HTTP AND the
    endpoint resolves to a local target, this checks the container state
    BEFORE the ``/health`` probe:

    - Container running → proceed straight to the probe.
    - Exists but stopped → ``docker start`` it, emit one INFO log line,
      proceed.
    - Doesn't exist → raise :class:`PreflightError` directing at
      ``xauditor coder init``.

    Remote endpoints skip the container check entirely.

    When ``coder.workspace_root`` is set AND ``repo_root`` is provided,
    this also probes ``GET /projects`` after a successful ``/health`` and
    validates that the resolved project name appears in the response.
    """

    if not coder_cfg.enabled:
        return None
    if coder_cfg.transport == "http":
        if coder_runtime is not None:
            _maybe_inspect_container(coder_cfg, coder_runtime, runtime_logger)
        result = check_coder_http(coder_cfg, env=env)
        if repo_root is not None:
            project = resolve_coder_project(coder_cfg, repo_root=repo_root)
            if project:
                check_coder_projects(coder_cfg, project=project)
        return result
    return check_coder_cli(coder_cfg, env=env)


def _verify_workspace_mount_or_raise(
    coder_cfg: CoderConfig, coder_runtime: object
) -> None:
    """Refuse audit start when the running container's /workspace mount
    source differs from ``coder.effective_workspace_root``.

    This catches the foot-gun where the operator changes
    ``coder.workspace_root`` in xauditor.yml but doesn't re-run
    ``xauditor coder reset --yes && xauditor coder init`` — the
    existing container's bind-mount is baked at ``docker run`` time
    and cannot be updated by ``docker start``. Without this check the
    audit silently runs claude against the wrong tree.
    """

    configured = (
        coder_cfg.effective_workspace_root
        or coder_cfg.repo_mount_path
        or ""
    )
    if not configured:
        return
    inspect_mount = getattr(coder_runtime, "inspect_workspace_mount_source", None)
    if inspect_mount is None:
        return
    try:
        running_source = inspect_mount() or ""
    except Exception:  # noqa: BLE001 - best-effort diagnostic
        return
    if not running_source:
        return
    if Path(running_source).resolve() == Path(configured).resolve():
        return
    raise PreflightError(
        f"Coder container `{coder_cfg.container_name}` was created "
        f"with /workspace bind-mounted from `{running_source}`, but "
        f"config now has coder.workspace_root=`{configured}`. "
        "Bind-mounts cannot be changed without re-creating the "
        "container. Run `xauditor coder reset --yes` then "
        "`xauditor coder init` to apply the new workspace_root."
    )


def _maybe_inspect_container(
    coder_cfg: CoderConfig,
    coder_runtime: object,
    runtime_logger: object | None,
) -> None:
    """Container-state inspection branch of the HTTP preflight.

    Skipped silently for remote endpoints (the existing /health probe is
    sufficient there). Imports the helper lazily so this module doesn't
    cycle through ``xauditor.integrations.coder``.
    """

    from xauditor.integrations.coder import endpoint_is_local

    if not endpoint_is_local(coder_cfg):
        return
    inspect = getattr(coder_runtime, "inspect_for_preflight", None)
    if inspect is None:
        return
    try:
        state = inspect()
    except PreflightError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PreflightError(
            f"Coder container state could not be inspected: "
            f"{exc.__class__.__name__}: {exc}"
        ) from exc
    if state == "running":
        _verify_workspace_mount_or_raise(coder_cfg, coder_runtime)
        return
    if state == "started":
        _verify_workspace_mount_or_raise(coder_cfg, coder_runtime)
        if runtime_logger is not None:
            getattr(runtime_logger, "info", lambda _msg: None)(
                f"Coder container `{coder_cfg.container_name}` was stopped; "
                "starting it before audit"
            )
        return
    if state == "missing":
        raise PreflightError(
            f"Coder container `{coder_cfg.container_name}` does not exist. "
            "Run `xauditor coder init` to build and start it before running "
            "an audit, or set `coder.transport: subprocess` to use a "
            "host-installed Claude Code instead."
        )
    # Unknown state — surface it explicitly rather than silently proceed.
    raise PreflightError(
        f"Coder container state inspection returned unexpected value `{state}`."
    )


def check_report_database(reportdb_cfg, *, runtime_logger=None) -> None:
    """Pre-flight: refuse to start an audit when the report DB is unreachable.

    Phase 2 of make-postgres-the-canonical-sink: Postgres is mandatory
    in xauditor 0.5.0+. The audit kernel writes per-finding rows
    directly into ``audit_runs`` / ``findings`` / etc. as the run
    progresses, so a DB outage at audit-time is not recoverable —
    ``PostgresReportSink`` would log warnings while the in-memory
    state silently grew without persistence. We probe connectivity
    here, before the first LLM call, so operators see a clear error
    pointing at ``xauditor reportdb start`` / ``init`` instead of
    discovering halfway through that nothing was saved.

    The probe also confirms the schema is at least at the migration
    revision required by this xauditor version. A DB that exists but
    has not been migrated produces a "schema below minimum" failure
    with the remediation pointing at the portal's migration command.

    Errors are wrapped as :class:`PreflightError` with three branches:

    - **Connection refused / unreachable**: recommend
      ``xauditor reportdb start`` (or ``init`` for first-time setup).
    - **Authentication failed**: recommend reviewing
      ``reportdb.connection.username`` / ``password`` in
      ``xauditor.yml``.
    - **Schema below minimum**: recommend the migration command.
    """

    try:
        from sqlalchemy import create_engine, text  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - SQLAlchemy is a hard dep
        raise PreflightError(
            "SQLAlchemy is required for the report-database pre-flight; "
            "ensure xauditor was installed with all dependencies."
        ) from exc

    try:
        from xauditor_portal.sinks.postgres_sink import _sync_url_for
    except ImportError as exc:
        raise PreflightError(
            "xauditor-portal is not installed but is required by xauditor "
            "0.5.0+ (it owns the report database schema and sink). Install "
            "xauditor-portal>=0.4 alongside xauditor."
        ) from exc

    sync_url = _sync_url_for(reportdb_cfg)
    # Strip the password from the displayed endpoint so log lines / CLI
    # error output never echo it back.
    safe_endpoint = _safe_endpoint(sync_url)

    try:
        engine = create_engine(sync_url, pool_pre_ping=True)
    except Exception as exc:  # noqa: BLE001 - URL parse can throw odd shapes
        raise PreflightError(
            f"Report database URL is invalid for {safe_endpoint}: "
            f"{exc.__class__.__name__}: {exc}\n"
            "Review reportdb.connection in xauditor.yml."
        ) from exc

    try:
        try:
            connect_cm = engine.connect()
        except Exception as exc:  # noqa: BLE001 - connect() may fail eagerly
            _raise_authn_or_connect_error(safe_endpoint, exc)
            return  # unreachable; satisfies type checker
        try:
            with connect_cm as conn:
                try:
                    conn.execute(text("SELECT 1"))
                except Exception as exc:  # noqa: BLE001
                    _raise_authn_or_connect_error(safe_endpoint, exc)
                current_rev = _alembic_current_revision(conn)
        except PreflightError:
            raise
        except Exception as exc:  # noqa: BLE001 - context-manager enter / exit
            _raise_authn_or_connect_error(safe_endpoint, exc)
            return
        _ensure_minimum_revision(safe_endpoint, current_rev)
    finally:
        engine.dispose()

    if runtime_logger is not None:
        getattr(runtime_logger, "info", lambda _msg: None)(
            f"Report database reachable at {safe_endpoint} "
            f"(alembic revision: {current_rev or 'unknown'})"
        )


# Bumped whenever a migration adds something the audit code path
# strictly requires. xauditor 0.5.0 needs ``audit_runs.resume_state``
# (added in 0006_audit_runs_resume_state) for in-DB resume.
_REPORT_DB_MINIMUM_REVISION = "0006_audit_runs_resume_state"


def _alembic_current_revision(conn) -> str | None:
    """Return the alembic revision the report DB is currently stamped at.

    Returns ``None`` when no ``alembic_version`` table exists (a fresh
    DB that has not been migrated at all). The caller raises a
    ``schema-below-minimum`` PreflightError in that case.
    """

    from sqlalchemy import text

    try:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    except Exception:  # noqa: BLE001 - missing table / mid-migration
        return None
    if row is None:
        return None
    return str(row[0])


def _ensure_minimum_revision(endpoint: str, current_rev: str | None) -> None:
    if current_rev is None:
        raise PreflightError(
            f"Report database at {endpoint} has no alembic schema. "
            "Run `xauditor reportdb init` (which applies migrations as "
            "part of first-time setup) or `xauditor-portal migrate` "
            f"to bring the schema up to {_REPORT_DB_MINIMUM_REVISION}."
        )
    # We compare by revision id rather than monotonic ordering. The
    # migrations form a linear chain via ``down_revision`` so any rev
    # different from the head is implicitly older OR the schema is
    # ahead of this xauditor version (in which case we accept it —
    # forward-compatible additions are allowed).
    if current_rev == _REPORT_DB_MINIMUM_REVISION:
        return
    # Best-effort linearity check: try importing the migrations env to
    # see whether ``current_rev`` is an ancestor of the required rev.
    if _revision_is_ancestor(current_rev, _REPORT_DB_MINIMUM_REVISION):
        raise PreflightError(
            f"Report database at {endpoint} is at alembic revision "
            f"{current_rev}, below the minimum required by xauditor "
            f"({_REPORT_DB_MINIMUM_REVISION}). Run `xauditor-portal "
            "migrate` (or `xauditor reportdb init` for a clean setup) "
            "to apply the missing migrations."
        )


def _revision_is_ancestor(candidate: str, target: str) -> bool:
    """Return True iff *candidate* is an ancestor of *target* in the chain.

    Walks ``packages/xauditor-portal/.../migrations/versions/*.py`` files
    and follows ``down_revision`` pointers from *target* backwards.
    Defaults to True (i.e. assume the user needs to migrate) when the
    chain cannot be resolved — better to err toward "needs migrate"
    than to silently let a broken setup proceed.
    """

    try:
        import importlib
        import importlib.util
        from pathlib import Path

        spec = importlib.util.find_spec("xauditor_portal.db.migrations.versions")
        if spec is None or spec.submodule_search_locations is None:
            return True
        versions_dir = Path(next(iter(spec.submodule_search_locations)))
        chain: dict[str, str | None] = {}
        for migration in versions_dir.glob("*.py"):
            text = migration.read_text(encoding="utf-8")
            revision = _extract_revision_string(text, "revision")
            down = _extract_revision_string(text, "down_revision")
            if revision:
                chain[revision] = down
        cursor: str | None = target
        seen: set[str] = set()
        while cursor and cursor in chain and cursor not in seen:
            seen.add(cursor)
            if chain[cursor] == candidate:
                return True
            cursor = chain[cursor]
        return False
    except Exception:  # noqa: BLE001 - best-effort
        return True


def _extract_revision_string(text: str, key: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(key):
            continue
        # Format ``revision: str = "..."`` or ``down_revision: str | None = "..."``.
        _, _, rhs = stripped.partition("=")
        rhs = rhs.strip().rstrip(",")
        if rhs in ("None", "''", '""'):
            return None
        return rhs.strip("'\"")
    return None


def _raise_authn_or_connect_error(endpoint: str, exc: Exception) -> None:
    text = str(exc).lower()
    if "password authentication failed" in text or "role" in text and "does not exist" in text:
        raise PreflightError(
            f"Report database authentication failed for {endpoint}: {exc}\n"
            "Review reportdb.connection.username / password in xauditor.yml "
            "(or the equivalent secret-store entry)."
        ) from exc
    if "connection refused" in text or "could not connect" in text or "no such file" in text:
        raise PreflightError(
            f"Report database is unreachable at {endpoint}: {exc}\n"
            "Run `xauditor reportdb start` to bring it up "
            "(or `xauditor reportdb init` if no managed container exists yet)."
        ) from exc
    raise PreflightError(
        f"Report database probe failed for {endpoint}: "
        f"{exc.__class__.__name__}: {exc}\n"
        "Run `xauditor reportdb start` to verify the container is up."
    ) from exc


def _safe_endpoint(sync_url: str) -> str:
    """Strip credentials from the SQLAlchemy URL for log / error display."""

    # postgresql+psycopg://<user>:<password>@host:port/db
    head, sep, tail = sync_url.partition("://")
    if not sep:
        return sync_url
    if "@" not in tail:
        return sync_url
    creds_and_host = tail
    _, _, host_part = creds_and_host.partition("@")
    return f"{head}://{host_part}"


__all__ = [
    "CoderPreflightResult",
    "check_coder_cli",
    "check_coder_http",
    "check_coder_runtime",
    "check_report_database",
]
