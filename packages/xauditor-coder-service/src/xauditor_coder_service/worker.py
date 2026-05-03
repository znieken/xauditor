"""Async worker that spawns Claude Code subprocesses.

Mirrors ``xauditor.audit.coder.ClaudeCodeCliTransport.invoke`` in shape
but uses ``asyncio.create_subprocess_exec`` instead of blocking
``subprocess.Popen`` so a single event loop can host many concurrent
worker tasks under an ``asyncio.Semaphore``.

The output JSON shape MUST match what ``parse_coder_response`` already
produces; the xauditor-side ``HttpCoderTransport`` reconstructs a
``CoderResult`` from that exact shape.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import shutil
import signal
from time import monotonic
from typing import Any, Mapping

from xauditor_coder_service._vendored import (
    CODER_STATUS_INCONCLUSIVE,
    build_cli_argv,
    parse_coder_response,
    scrub_environment,
)
from xauditor_coder_service.job import (
    STATUS_CANCELLED,
    STATUS_DONE,
    JobState,
    JobStore,
)


log = logging.getLogger("xauditor_coder_service.worker")


_STDOUT_TRUNCATE_BYTES = 256 * 1024
_STDERR_TRUNCATE_BYTES = 32 * 1024


@dataclasses.dataclass(frozen=True)
class WorkerConfig:
    """Service-side worker config. NOT to be confused with xauditor's CoderConfig."""

    cli_path: str
    """Resolved absolute path to the `claude` executable inside the container."""


def resolve_cli_path(cli_command: str = "claude") -> str:
    """Resolve the CLI executable once at startup so workers don't re-resolve.

    Falls back to the bare command when ``shutil.which`` returns nothing —
    the worker will surface the failure as `transport error: cli not found`.
    """

    resolved = shutil.which(cli_command)
    return resolved or cli_command


async def run_verification(
    job: JobState,
    *,
    payload: Mapping[str, Any],
    claude_args: Mapping[str, Any],
    request_timeout_seconds: int,
    worker_config: WorkerConfig,
    job_store: JobStore,
    cwd: str | None = None,
    home: str | None = None,
) -> None:
    """Execute one verification, mutating the job state when done.

    Always flips the job to a terminal status before returning, so the
    GET /verifications/{id} handler always sees a settled state once we
    leave this coroutine. Cancellation (via ``job.cancel_event``) flips
    the job to ``cancelled`` after best-effort SIGTERM/SIGKILL of the
    inner subprocess.

    ``cwd`` and ``home`` come from the multi-project caller. ``cwd`` is
    set to ``/workspace/<project>`` so claude's working directory is
    the audited repo. ``home`` is set to ``/home/coder/<project>`` so
    concurrent verifications across projects do not race on session
    caches under ``$HOME/.claude/``. Both are ``None`` for the legacy
    single-repo container path; the inherited env / cwd applies.
    """

    started = monotonic()
    argv = build_cli_argv(
        (worker_config.cli_path,),
        thinking_effort=claude_args.get("thinking_effort"),
    )

    # Claude's three driving env vars (ANTHROPIC_API_KEY,
    # ANTHROPIC_BASE_URL, ANTHROPIC_MODEL) are baked into the worker
    # container's env at ``xauditor coder init`` time (see
    # ``xauditor.integrations.coder.helpers.build_run_argv``). The
    # worker just inherits them via ``scrub_environment(os.environ)``;
    # there is no per-request override path. The HTTP request body's
    # ``claude_args`` only carries ``thinking_effort`` going forward.
    env = scrub_environment(os.environ)
    if home:
        # Per-project HOME scopes claude's session cache /
        # ``~/.claude/`` state under the named volume so concurrent
        # verifications don't race.
        env["HOME"] = home

    encoded_payload = json.dumps(dict(payload), default=str).encode("utf-8")

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        _finish_inconclusive(
            job_store, job, reason=f"transport error: cli not found ({exc})",
            duration_ms=int((monotonic() - started) * 1000),
        )
        return
    except OSError as exc:
        _finish_inconclusive(
            job_store, job, reason=f"transport error: spawn failed ({exc})",
            duration_ms=int((monotonic() - started) * 1000),
        )
        return

    job.process = process

    cancel_task = asyncio.create_task(_wait_cancel_and_kill(job))
    communicate_task = asyncio.create_task(
        process.communicate(input=encoded_payload)
    )
    timeout_task = asyncio.create_task(asyncio.sleep(request_timeout_seconds))

    try:
        done, _pending = await asyncio.wait(
            {communicate_task, timeout_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done and not communicate_task.done():
            # Cancellation already started killing the process; make sure
            # we wait for it to drain stdout / stderr.
            await _terminate(process, grace_seconds=2.0)
            stdout_bytes, stderr_bytes = await _drain_communicate(communicate_task)
            duration_ms = int((monotonic() - started) * 1000)
            _finish_terminal(
                job_store,
                job,
                status=STATUS_CANCELLED,
                error="cancelled by client",
                result=None,
                duration_ms=duration_ms,
            )
            return
        if timeout_task in done and not communicate_task.done():
            await _terminate(process, grace_seconds=2.0)
            stdout_bytes, stderr_bytes = await _drain_communicate(communicate_task)
            duration_ms = int((monotonic() - started) * 1000)
            timeout_result = parse_coder_response(
                stdout="",
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                exit_code=process.returncode if process.returncode is not None else -1,
                duration_ms=duration_ms,
            )
            # Replace the parser's "exit -N" reason with the timeout reason.
            timeout_result_dict = dataclasses.asdict(timeout_result)
            timeout_result_dict.update(
                {
                    "status": CODER_STATUS_INCONCLUSIVE,
                    "reason": f"timed out after {request_timeout_seconds}s",
                }
            )
            _finish_terminal(
                job_store,
                job,
                status=STATUS_DONE,
                error=None,
                result=_serializable_result(timeout_result_dict),
                duration_ms=duration_ms,
            )
            return
        # communicate_task completed first (the normal path)
        stdout_bytes, stderr_bytes = await communicate_task
        exit_code = process.returncode if process.returncode is not None else -1
        duration_ms = int((monotonic() - started) * 1000)
        stdout_text = stdout_bytes[:_STDOUT_TRUNCATE_BYTES].decode("utf-8", errors="replace")
        stderr_text = stderr_bytes[:_STDERR_TRUNCATE_BYTES].decode("utf-8", errors="replace")
        result = parse_coder_response(
            stdout=stdout_text,
            stderr=stderr_text,
            exit_code=exit_code,
            duration_ms=duration_ms,
        )
        _finish_terminal(
            job_store,
            job,
            status=STATUS_DONE,
            error=None,
            result=_serializable_result(dataclasses.asdict(result)),
            duration_ms=duration_ms,
        )
    except Exception as exc:  # noqa: BLE001 - last-resort safety net
        log.warning("worker crashed for job %s: %s", job.job_id, exc)
        _finish_inconclusive(
            job_store,
            job,
            reason=f"transport error: worker crash ({exc})",
            duration_ms=int((monotonic() - started) * 1000),
        )
    finally:
        for task in (cancel_task, timeout_task):
            if not task.done():
                task.cancel()
        # Drain so the asyncio bookkeeping doesn't complain.
        for task in (cancel_task, timeout_task):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _wait_cancel_and_kill(job: JobState) -> None:
    """Wait for ``job.cancel_event`` then terminate the inner subprocess."""

    await job.cancel_event.wait()
    process = job.process
    if process is None:
        return
    await _terminate(process, grace_seconds=2.0)


async def _terminate(process, *, grace_seconds: float) -> None:
    """SIGTERM, then SIGKILL after ``grace_seconds`` if still alive."""

    if process.returncode is not None:
        return
    try:
        process.terminate()
    except (ProcessLookupError, OSError):
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except asyncio.TimeoutError:
        pass
    if process.returncode is None:
        try:
            if hasattr(signal, "SIGKILL"):
                process.kill()
            else:  # pragma: no cover - non-POSIX
                process.terminate()
        except (ProcessLookupError, OSError):
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            pass


async def _drain_communicate(task: asyncio.Task) -> tuple[bytes, bytes]:
    try:
        return await asyncio.wait_for(task, timeout=2.0)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        return b"", b""


def _finish_inconclusive(
    store: JobStore,
    job: JobState,
    *,
    reason: str,
    duration_ms: int,
) -> None:
    """Helper: stamp a transport-level inconclusive verdict and flip to ``done``.

    The wire shape stays "done with an Inconclusive result" so the
    xauditor-side transport doesn't have to special-case error-vs-done.
    """

    result = {
        "status": CODER_STATUS_INCONCLUSIVE,
        "analysis": "",
        "reason": reason,
        "evidence": (),
        "cli_exit_code": None,
        "cli_stderr": None,
        "duration_ms": duration_ms,
    }
    _finish_terminal(
        store, job, status=STATUS_DONE, error=None,
        result=_serializable_result(result), duration_ms=duration_ms,
    )


def _finish_terminal(
    store: JobStore,
    job: JobState,
    *,
    status: str,
    error: str | None,
    result: dict[str, Any] | None,
    duration_ms: int,
) -> None:
    if status == STATUS_DONE and result is None:
        result = {
            "status": CODER_STATUS_INCONCLUSIVE,
            "analysis": "",
            "reason": "transport error: empty result",
            "evidence": [],
            "cli_exit_code": None,
            "cli_stderr": None,
            "duration_ms": duration_ms,
        }
    store.mark_terminal(job.job_id, status=status, result=result, error=error)


def _serializable_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Turn the dataclass dict into wire-shape JSON.

    `dataclasses.asdict` already converts nested dataclasses (e.g.
    ``CoderEvidence``); we just need to coerce the tuple of evidence into
    a list and ensure no leftover non-JSON types.
    """

    out: dict[str, Any] = dict(raw)
    evidence = out.get("evidence") or ()
    if isinstance(evidence, (tuple, list)):
        out["evidence"] = [dict(item) if not isinstance(item, dict) else item for item in evidence]
    else:
        out["evidence"] = []
    return out


__all__ = [
    "WorkerConfig",
    "resolve_cli_path",
    "run_verification",
]
