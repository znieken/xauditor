"""Synchronous agent-invocation orchestration.

Companion to ``worker.py`` (which serves async per-finding
verification jobs via /verifications). This module serves the
generic ``POST /agent_invocations`` endpoint introduced by the
``extend-coder-service-for-agent-invocations`` change — short-running,
synchronous, schema-validated invocations of the real ``claude`` CLI.

Wire shape:

  Request:  AgentInvocationRequest
            {system_prompt, user_payload, response_schema,
             timeout_seconds, project}
  Response: AgentInvocationResponse
            {final_answer, transcript, fell_back, fallback_reason,
             elapsed_seconds}

CLI invocation (real ``claude`` flags):

  claude -p \\
    --output-format json \\
    --json-schema '<response_schema>' \\
    --append-system-prompt '<system_prompt>' \\
    --add-dir '<project_dir>' \\
    < <(echo '<user_payload_json>')

The endpoint is a generic transport — it doesn't know about
analyzers / validators / exploiters / reconcilers. The xauditor side
(``CoderServiceAgentTransport``) decides which stage prompt to send
+ which response schema to validate against.

Cost control: caller-side ``timeout_seconds`` only. No
``max_tool_calls``, no ``max_budget_usd`` — the coder-service
container is the sandbox and operator-tunable wall-clock is the
bound. See the change's ``proposal.md`` for rationale.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import signal
from time import monotonic
from typing import Any, Mapping

import jsonschema
from pydantic import BaseModel, Field

from xauditor_coder_service._vendored import scrub_environment


log = logging.getLogger("xauditor_coder_service.agent_invocation")


_STDOUT_TRUNCATE_BYTES = 1024 * 1024  # 1 MiB; transcripts can be large
_STDERR_TRUNCATE_BYTES = 64 * 1024


# ---------------------------------------------------------------------------
# Wire-shape Pydantic models
# ---------------------------------------------------------------------------


class AgentInvocationRequest(BaseModel):
    """Generic agent-invocation request body."""

    system_prompt: str = Field(..., min_length=1)
    user_payload: dict[str, Any]
    response_schema: dict[str, Any]
    timeout_seconds: int = Field(..., ge=10, le=1800)
    # Empty / None routes to ``/workspace`` directly (legacy
    # single-repo container). Otherwise routes to
    # ``/workspace/<project>`` after the existing project-validation
    # rules in ``app.py``.
    project: str | None = None


class AgentInvocationResponse(BaseModel):
    """Generic agent-invocation response body."""

    final_answer: dict[str, Any] | None
    transcript: list[dict[str, Any]]
    fell_back: bool
    fallback_reason: str | None
    elapsed_seconds: float


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AgentInvocationConfig:
    """Per-call config supplied by the FastAPI handler."""

    cli_path: str
    """Resolved absolute path to the ``claude`` executable."""


async def run_agent_invocation(
    request: AgentInvocationRequest,
    *,
    config: AgentInvocationConfig,
    project_dir: str,
    home: str | None = None,
    cancel_event: asyncio.Event | None = None,
) -> AgentInvocationResponse:
    """Run one synchronous agent invocation. Returns the response.

    ``project_dir`` is the absolute path the FastAPI handler resolved
    via ``_validate_project_or_raise`` (empty string for the legacy
    single-repo container). ``home`` is the per-project HOME path
    (mirrors worker.py); ``None`` falls back to the inherited HOME.

    ``cancel_event`` is set by the FastAPI handler when the HTTP
    client disconnects. The orchestrator races it against
    ``communicate()`` and the wall-clock timeout.
    """

    started = monotonic()

    # Validate the response_schema itself before we spend a subprocess
    # slot. Callers passing a malformed JSON Schema get a 422 before
    # claude runs.
    try:
        jsonschema.Draft202012Validator.check_schema(request.response_schema)
    except jsonschema.SchemaError as exc:
        # Surface as a fallback rather than a 5xx — the request
        # reached us; claude just can't run with this input. The
        # FastAPI layer can choose to translate to 4xx if it wants,
        # but the orchestrator returns the structured shape.
        return AgentInvocationResponse(
            final_answer=None,
            transcript=[],
            fell_back=True,
            fallback_reason=f"invalid_response_schema: {exc.message}",
            elapsed_seconds=monotonic() - started,
        )

    argv = [
        config.cli_path,
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(request.response_schema),
        "--append-system-prompt",
        request.system_prompt,
    ]
    cwd_path: str | None = None
    if project_dir:
        argv.extend(["--add-dir", project_dir])
        cwd_path = project_dir

    env = scrub_environment(os.environ)
    if home:
        env["HOME"] = home

    encoded_payload = json.dumps(request.user_payload, default=str).encode("utf-8")

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd_path,
        )
    except FileNotFoundError as exc:
        return _fallback(
            "subprocess_spawn_failed",
            detail=f"cli not found ({exc})",
            started=started,
        )
    except OSError as exc:
        return _fallback(
            "subprocess_spawn_failed",
            detail=f"spawn failed ({exc})",
            started=started,
        )

    cancel_task: asyncio.Task | None = None
    if cancel_event is not None:
        cancel_task = asyncio.create_task(_wait_cancel_and_kill(process, cancel_event))
    communicate_task = asyncio.create_task(process.communicate(input=encoded_payload))
    timeout_task = asyncio.create_task(asyncio.sleep(request.timeout_seconds))

    try:
        watch_set: set[asyncio.Task] = {communicate_task, timeout_task}
        if cancel_task is not None:
            watch_set.add(cancel_task)
        done, _pending = await asyncio.wait(
            watch_set,
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Check cancel_event FIRST so we always see a cancellation
        # as a cancellation — even when the subprocess died from
        # SIGTERM (which causes communicate_task to complete in the
        # same wake-up). Without this priority, the race goes to
        # whichever task's "done" condition we check first and a
        # cancelled run looks like a subprocess crash.
        if cancel_event is not None and cancel_event.is_set():
            await _terminate(process, grace_seconds=2.0)
            await _drain_communicate(communicate_task)
            return _fallback(
                "cancelled",
                detail="client disconnected",
                started=started,
            )

        if timeout_task in done and not communicate_task.done():
            await _terminate(process, grace_seconds=2.0)
            await _drain_communicate(communicate_task)
            return _fallback(
                "timeout",
                detail=f"timed out after {request.timeout_seconds}s",
                started=started,
            )

        # Normal path: claude completed before timeout / cancellation.
        stdout_bytes, stderr_bytes = await communicate_task
        exit_code = process.returncode if process.returncode is not None else -1
        stdout_text = stdout_bytes[:_STDOUT_TRUNCATE_BYTES].decode(
            "utf-8", errors="replace"
        )
        stderr_text = stderr_bytes[:_STDERR_TRUNCATE_BYTES].decode(
            "utf-8", errors="replace"
        )

        if exit_code != 0:
            return _fallback(
                f"subprocess_exit_{exit_code}",
                detail=stderr_text[-512:] if stderr_text else "",
                started=started,
            )

        return _parse_and_validate(
            stdout_text,
            response_schema=request.response_schema,
            started=started,
        )

    except Exception as exc:  # noqa: BLE001 - last-resort safety net
        log.warning("agent_invocation crashed: %s", exc)
        return _fallback(
            "orchestrator_crash",
            detail=str(exc),
            started=started,
        )
    finally:
        for task in (timeout_task, cancel_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (timeout_task, cancel_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fallback(
    reason: str, *, detail: str = "", started: float
) -> AgentInvocationResponse:
    """Build a fallback response with elapsed timing populated."""

    elapsed = monotonic() - started
    full_reason = reason if not detail else f"{reason}: {detail}"
    return AgentInvocationResponse(
        final_answer=None,
        transcript=[],
        fell_back=True,
        fallback_reason=full_reason[:500],  # cap for the JSONB column
        elapsed_seconds=elapsed,
    )


def _parse_and_validate(
    stdout_text: str,
    *,
    response_schema: Mapping[str, Any],
    started: float,
) -> AgentInvocationResponse:
    """Parse claude's ``--output-format json`` envelope.

    Claude's JSON output envelope (per ``claude --help``) wraps the
    final answer alongside per-tool metadata. We extract the
    ``final_answer`` (the structured-output payload validated against
    the supplied schema), capture the per-tool transcript, and emit
    them in our wire shape.
    """

    stripped = stdout_text.strip()
    if not stripped:
        return _fallback("empty_stdout", started=started)

    try:
        envelope = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return _fallback(
            "malformed_response", detail=str(exc), started=started
        )

    final_answer, transcript = _extract_envelope(envelope)
    if final_answer is None:
        return _fallback("no_final_answer", started=started)

    try:
        jsonschema.validate(final_answer, response_schema)
    except jsonschema.ValidationError as exc:
        return AgentInvocationResponse(
            final_answer=final_answer,
            transcript=transcript,
            fell_back=True,
            fallback_reason=f"schema_validation_failed: {exc.message}"[:500],
            elapsed_seconds=monotonic() - started,
        )

    return AgentInvocationResponse(
        final_answer=final_answer,
        transcript=transcript,
        fell_back=False,
        fallback_reason=None,
        elapsed_seconds=monotonic() - started,
    )


def _extract_envelope(
    envelope: Any,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Extract ``(final_answer, transcript)`` from claude's JSON output.

    Claude's ``--output-format json`` emits a structured envelope. With
    ``--json-schema``, Claude Code 2.1.x puts the schema-conforming
    payload under ``structured_output`` and a conversational summary
    string under ``result``. Earlier versions inlined the dict at
    ``result`` directly. This extractor probes a few well-known keys
    in preference order (``structured_output``, ``final_answer``,
    ``result``, ``output``, ``response``) and falls back to treating
    the envelope itself as the final answer when no key matches.

    Tool transcript: a top-level ``messages`` / ``tool_calls`` /
    ``transcript`` array, when present.
    """

    if not isinstance(envelope, dict):
        # Top-level scalar / array — treat as the final answer when
        # the schema accepts it; otherwise schema validation will
        # catch the mismatch downstream.
        if isinstance(envelope, dict):  # pragma: no cover - unreachable
            return envelope, []
        return None, []

    final_answer: dict[str, Any] | None = None
    for key in ("structured_output", "final_answer", "result", "output", "response"):
        candidate = envelope.get(key)
        if isinstance(candidate, dict):
            final_answer = candidate
            break

    if final_answer is None:
        # Envelope itself IS the structured answer (when the schema
        # is matched against the whole stdout JSON object).
        # Drop CLI-known telemetry keys so the schema only sees the
        # caller-meaningful fields.
        telemetry_keys = {
            "messages", "tool_calls", "transcript",
            "usage", "model", "session_id", "session",
            "structured_output",
        }
        candidate = {k: v for k, v in envelope.items() if k not in telemetry_keys}
        if candidate:
            final_answer = candidate

    transcript: list[dict[str, Any]] = []
    for key in ("transcript", "tool_calls", "messages"):
        candidate = envelope.get(key)
        if isinstance(candidate, list):
            transcript = [
                item if isinstance(item, dict) else {"raw": item}
                for item in candidate
            ]
            break

    return final_answer, transcript


async def _wait_cancel_and_kill(process, cancel_event: asyncio.Event) -> None:
    """Wait for cancel signal then SIGTERM/SIGKILL the subprocess."""

    await cancel_event.wait()
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


__all__ = [
    "AgentInvocationConfig",
    "AgentInvocationRequest",
    "AgentInvocationResponse",
    "run_agent_invocation",
]
