"""FastAPI application factory for the coder microservice.

The six routes are described by the spec ``coder-http-microservice``:

  POST   /verifications        — submit; returns 201 (or 200 on idempotent hit)
  GET    /verifications/{id}   — poll; returns the wire-shape JobState
  DELETE /verifications/{id}   — cancel; SIGTERM/SIGKILL the inner subprocess
  POST   /agent_invocations    — synchronous; returns the structured
                                 AgentInvocationResponse body when claude
                                 finishes (or the wall-clock timer fires)
  GET    /projects             — list direct child subdirs of /workspace
  GET    /health               — unauth liveness probe

Concurrency is bounded by ``asyncio.Semaphore(max_concurrent_jobs)``
shared across BOTH ``/verifications`` and ``/agent_invocations``;
queueing happens here, not on the xauditor side.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from xauditor_coder_service._vendored import walk_projects
from xauditor_coder_service.agent_invocation import (
    AgentInvocationConfig,
    AgentInvocationRequest,
    AgentInvocationResponse,
    run_agent_invocation,
)
from xauditor_coder_service.auth import (
    AuthSettings,
    load_auth_settings,
    make_auth_dependency,
)
from xauditor_coder_service.job import (
    STATUS_PENDING,
    IdempotencyProjectConflict,
    JobStore,
)
from xauditor_coder_service.worker import (
    WorkerConfig,
    resolve_cli_path,
    run_verification,
)


log = logging.getLogger("xauditor_coder_service.app")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceSettings:
    auth: AuthSettings
    max_concurrent_jobs: int
    job_ttl_seconds: float
    cli_command: str
    cli_path: str
    cli_version: str

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        cli_version_probe: bool = True,
    ) -> "ServiceSettings":
        env = env if env is not None else os.environ
        auth = load_auth_settings(env)
        try:
            max_concurrent = int(
                (env.get("XAUDITOR_CODER_SERVICE_MAX_CONCURRENT") or "4").strip() or "4"
            )
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid XAUDITOR_CODER_SERVICE_MAX_CONCURRENT: {exc}"
            ) from exc
        if max_concurrent < 1:
            raise RuntimeError(
                "XAUDITOR_CODER_SERVICE_MAX_CONCURRENT must be >= 1."
            )
        try:
            ttl = float(
                (env.get("XAUDITOR_CODER_SERVICE_JOB_TTL_SECONDS") or "3600").strip() or "3600"
            )
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid XAUDITOR_CODER_SERVICE_JOB_TTL_SECONDS: {exc}"
            ) from exc
        cli_command = (env.get("XAUDITOR_CODER_SERVICE_CLI_COMMAND") or "claude").strip() or "claude"
        cli_path = resolve_cli_path(cli_command)
        cli_version = ""
        if cli_version_probe:
            cli_version = _probe_cli_version(cli_path)
        return cls(
            auth=auth,
            max_concurrent_jobs=max_concurrent,
            job_ttl_seconds=ttl,
            cli_command=cli_command,
            cli_path=cli_path,
            cli_version=cli_version,
        )


def _probe_cli_version(cli_path: str) -> str:
    """One-shot ``<cli_path> --version`` capture during startup.

    Best-effort: failures yield an empty string. The /health endpoint
    surfaces whatever we captured here so xauditor's pre-flight can
    reflect the real CLI version on the run.
    """

    import subprocess as _sp

    try:
        completed = _sp.run(
            [cli_path, "--version"],
            stdin=_sp.DEVNULL,
            stdout=_sp.PIPE,
            stderr=_sp.PIPE,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, OSError, _sp.TimeoutExpired):
        return ""
    if completed.returncode != 0:
        return ""
    return (completed.stdout or b"").decode("utf-8", errors="replace").strip()


# --------------------------------------------------------------------------
# Wire-shape pydantic models
# --------------------------------------------------------------------------


class ClaudeArgs(BaseModel):
    thinking_effort: str | None = None
    model_url: str | None = None
    model_name: str | None = None
    model_api_key: str | None = None


class SubmitRequest(BaseModel):
    idempotency_key: str = Field(..., min_length=1)
    # ``project`` routes the verification to ``/workspace/<project>/``.
    # Empty string means the legacy single-repo container is in use and
    # the service runs claude in ``/workspace`` directly. Validated
    # against ``_PROJECT_NAME_RE`` and the realpath child-of check;
    # see ``_validate_project_or_raise``.
    project: str = ""
    payload: dict[str, Any]
    claude_args: ClaudeArgs = Field(default_factory=ClaudeArgs)
    request_timeout_seconds: int = Field(..., ge=1)


_WORKSPACE_ROOT = Path("/workspace")
_HOME_ROOT = Path("/home/coder")
_PROJECT_COMPONENT_RE = re.compile(r"^[a-zA-Z0-9._-]+$")
_PROJECT_NAME_MAX_LEN = 512

# Rate-limit for the truncation warning (one entry per minute per process).
_TRUNCATION_WARN_INTERVAL = 60.0
_last_truncation_warn_at: float = 0.0


def _validate_project_format_or_raise(project: str) -> None:
    """Reject malformed nested project names with HTTP 400.

    Accepts a non-empty ``/``-joined sequence of one or more components,
    each matching ``^[a-zA-Z0-9._-]+$``. Rejects ``..`` components,
    leading / trailing slashes, double slashes, and oversized names.
    """

    if not project:
        return
    if len(project) > _PROJECT_NAME_MAX_LEN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "project name rejected", "project": project},
        )
    if project.startswith("/") or project.endswith("/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "project name rejected", "project": project},
        )
    components = project.split("/")
    for component in components:
        if not component or component == "..":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "project name rejected", "project": project},
            )
        if not _PROJECT_COMPONENT_RE.fullmatch(component):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": "project name rejected", "project": project},
            )


def _list_workspace_projects() -> list[str]:
    """Walk ``/workspace`` for audit-able projects.

    Calls the shared ``walk_projects`` helper. Logs a structured warning
    when either the entry-budget OR the wall-clock-budget cap kicks in
    (rate-limited to one entry per minute per process). Returns ``[]``
    when the workspace mount is missing (legacy single-repo containers).
    """

    if not os.path.isdir(_WORKSPACE_ROOT):
        return []
    names, info = walk_projects(str(_WORKSPACE_ROOT))
    if info.get("truncated"):
        global _last_truncation_warn_at
        now = time.monotonic()
        if now - _last_truncation_warn_at >= _TRUNCATION_WARN_INTERVAL:
            _last_truncation_warn_at = now
            log.warning(
                "GET /projects truncated while walking workspace_root=%s "
                "(entries_visited=%d, elapsed=%.2fs). Depth-1 listing is "
                "complete; nested projects below depth 1 may be missing. "
                "Move workspace_root to a directory that directly "
                "contains the audit-able projects, or run on a faster "
                "filesystem (vmhgfs/9p shared mounts are ~40 ms per "
                "scandir and exhaust the time budget quickly).",
                _WORKSPACE_ROOT,
                info["entries_visited"],
                info.get("elapsed_seconds", 0.0),
            )
    return names


def _validate_project_or_raise(project: str) -> str:
    """Validate ``project`` and return its resolved subdir path string.

    - Empty string is allowed and signals "legacy single-repo container";
      callers SHALL skip the realpath check and run claude in
      ``/workspace`` directly.
    - Anything else MUST match the path-component grammar AND resolve to
      a path *inside* ``/workspace`` (after realpath). Nested paths like
      ``team/repo`` are accepted at any depth.

    Raises :class:`HTTPException` with the spec'd 400/404 status codes.
    """

    if not project:
        return ""
    _validate_project_format_or_raise(project)
    candidate = _WORKSPACE_ROOT / project
    try:
        resolved = Path(os.path.realpath(candidate))
        workspace_real = Path(os.path.realpath(_WORKSPACE_ROOT))
    except OSError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "project not found", "project": project},
        )
    if not resolved.is_relative_to(workspace_real):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "project path traversal rejected", "project": project},
        )
    if not resolved.is_dir():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "project not found", "project": project},
        )
    return str(resolved)


# --------------------------------------------------------------------------
# Application factory
# --------------------------------------------------------------------------


def create_app(
    *,
    settings: ServiceSettings | None = None,
    job_store: JobStore | None = None,
) -> FastAPI:
    """Build the FastAPI app. Settings + JobStore are injectable for tests."""

    if settings is None:
        settings = ServiceSettings.from_env()
    if job_store is None:
        job_store = JobStore(terminal_ttl_seconds=settings.job_ttl_seconds)

    semaphore = asyncio.Semaphore(settings.max_concurrent_jobs)
    auth_dep = make_auth_dependency(settings.auth)
    worker_cfg = WorkerConfig(cli_path=settings.cli_path)
    agent_inv_cfg = AgentInvocationConfig(cli_path=settings.cli_path)

    @asynccontextmanager
    async def _lifespan(app: FastAPI):  # noqa: D401
        reaper = asyncio.create_task(_reaper_loop(job_store))
        try:
            yield
        finally:
            reaper.cancel()
            try:
                await reaper
            except asyncio.CancelledError:
                pass

    app = FastAPI(lifespan=_lifespan)
    app.state.settings = settings
    app.state.job_store = job_store

    # ----- /health -------------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "claude_cli_version": settings.cli_version,
            "max_concurrent_jobs": settings.max_concurrent_jobs,
            "in_flight": job_store.in_flight(),
        }

    # ----- /verifications ------------------------------------------------

    @app.post("/verifications", dependencies=[Depends(auth_dep)])
    async def submit(request: SubmitRequest) -> JSONResponse:
        # Project validation runs BEFORE worker-pool admission so a
        # malformed name is rejected without consuming a slot. Empty
        # project string means the legacy single-repo container — the
        # validator passes through and the worker spawns claude in
        # ``/workspace`` directly.
        project_dir = _validate_project_or_raise(request.project)
        try:
            job, created = job_store.submit(
                request.idempotency_key, project=request.project
            )
        except IdempotencyProjectConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "idempotency_key bound to a different project",
                    "existing_project": exc.existing_project,
                    "requested_project": exc.requested_project,
                },
            )
        if created:
            asyncio.create_task(
                _run_with_semaphore(
                    semaphore,
                    job,
                    payload=request.payload,
                    claude_args=request.claude_args.model_dump(),
                    request_timeout_seconds=request.request_timeout_seconds,
                    worker_config=worker_cfg,
                    job_store=job_store,
                    project=request.project,
                    project_dir=project_dir,
                )
            )
        return JSONResponse(
            status_code=(
                status.HTTP_201_CREATED if created else status.HTTP_200_OK
            ),
            content={
                "job_id": job.job_id,
                "status_url": f"/verifications/{job.job_id}",
            },
        )

    @app.get("/projects", dependencies=[Depends(auth_dep)])
    async def projects() -> dict[str, list[str]]:
        return {"projects": _list_workspace_projects()}

    @app.get("/verifications/{job_id}", dependencies=[Depends(auth_dep)])
    async def status_route(job_id: str) -> dict[str, Any]:
        job = job_store.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown job_id: {job_id}",
            )
        return job.to_response_dict()

    # ----- /agent_invocations -------------------------------------------

    @app.post(
        "/agent_invocations",
        dependencies=[Depends(auth_dep)],
        response_model=AgentInvocationResponse,
    )
    async def agent_invoke(
        body: AgentInvocationRequest, request: Request
    ) -> AgentInvocationResponse:
        # Project validation BEFORE worker-pool admission so a malformed
        # name is rejected without consuming a slot. Empty / None
        # `project` routes to the legacy single-repo container —
        # `_validate_project_or_raise` returns "" and the orchestrator
        # spawns claude in `/workspace` (or inherited cwd) directly.
        project_dir = _validate_project_or_raise(body.project or "")

        # Per-project HOME isolates claude session state across concurrent
        # invocations targeting different projects. Empty project gets
        # the shared default HOME (matches worker.py's behaviour).
        home_path: str | None = None
        if body.project:
            home_path = str(_HOME_ROOT / body.project)
            try:
                Path(home_path).mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                log.warning(
                    "Failed to mkdir per-project HOME %s: %s. "
                    "Falling back to shared /home/coder.",
                    home_path,
                    exc,
                )
                home_path = None

        # Cancellation: client disconnect → set the cancel event so the
        # orchestrator SIGTERMs the inner subprocess. Mirrors
        # `DELETE /verifications/{id}` semantics for the synchronous
        # shape.
        cancel_event = asyncio.Event()
        watcher = asyncio.create_task(
            _watch_disconnect(request, cancel_event)
        )
        try:
            async with semaphore:
                return await run_agent_invocation(
                    body,
                    config=agent_inv_cfg,
                    project_dir=project_dir,
                    home=home_path,
                    cancel_event=cancel_event,
                )
        finally:
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    @app.delete(
        "/verifications/{job_id}",
        dependencies=[Depends(auth_dep)],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def cancel(job_id: str) -> Response:
        job = job_store.request_cancel(job_id)
        if job is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown job_id: {job_id}",
            )
        # If the job is queued (still pending and not yet running, i.e.,
        # still waiting on the semaphore), flip it to cancelled now so the
        # client poll sees the right status before the worker even picks
        # it up.
        if job.status == STATUS_PENDING and job.process is None:
            from xauditor_coder_service.job import STATUS_CANCELLED

            job_store.mark_terminal(
                job_id, status=STATUS_CANCELLED, error="cancelled by client",
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


async def _run_with_semaphore(
    semaphore: asyncio.Semaphore,
    job,
    *,
    payload,
    claude_args,
    request_timeout_seconds: int,
    worker_config: WorkerConfig,
    job_store: JobStore,
    project: str = "",
    project_dir: str = "",
) -> None:
    """Acquire the concurrency slot, then run the verification."""

    async with semaphore:
        # Re-check cancellation after acquiring — the operator may have
        # DELETEd while we were queued.
        if job.cancel_event.is_set():
            from xauditor_coder_service.job import STATUS_CANCELLED

            job_store.mark_terminal(
                job.job_id,
                status=STATUS_CANCELLED,
                error="cancelled before worker started",
            )
            return
        # Per-project ``$HOME`` isolates claude session state across
        # concurrent verifications targeting different projects. Empty
        # ``project`` (legacy single-repo container) gets the default
        # ``/home/coder`` shared volume — same as the pre-multi-project
        # behavior.
        cwd_path: str | None = project_dir or None
        home_path: str | None = None
        if project:
            home_path = str(_HOME_ROOT / project)
            try:
                Path(home_path).mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as exc:
                log.warning(
                    "Failed to mkdir per-project HOME %s: %s. "
                    "Falling back to shared /home/coder.",
                    home_path,
                    exc,
                )
                home_path = None
        await run_verification(
            job,
            payload=payload,
            claude_args=claude_args,
            request_timeout_seconds=request_timeout_seconds,
            worker_config=worker_config,
            job_store=job_store,
            cwd=cwd_path,
            home=home_path,
        )


async def _watch_disconnect(request: Request, cancel_event: asyncio.Event) -> None:
    """Poll ``request.is_disconnected()`` and set ``cancel_event`` on close.

    FastAPI doesn't push disconnect notifications; the standard pattern
    is to poll. 0.5s cadence balances responsiveness against overhead.
    """

    try:
        while not cancel_event.is_set():
            if await request.is_disconnected():
                cancel_event.set()
                return
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - watcher must not propagate
        pass


async def _reaper_loop(job_store: JobStore) -> None:
    """Periodic background task that evicts terminal jobs older than the TTL."""

    interval = max(60.0, job_store.terminal_ttl_seconds / 10)
    while True:
        try:
            await asyncio.sleep(interval)
            reaped = job_store.reap_terminal()
            if reaped:
                log.debug("Reaped %d terminal job(s)", len(reaped))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reaper must not die silently
            log.warning("Reaper loop error: %s", exc)


__all__ = ["create_app", "ServiceSettings"]
