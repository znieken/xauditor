"""AgentTransport Protocol + CoderServiceAgentTransport.

`agentic-stage-runner-real` Phase 1 introduced the
`AgentTransport` seam so the audit's agentic stage runner +
agentic reconciler could swap between transport
implementations without touching audit-side orchestration.

`wire-agentic-into-workflow` slimmed the Protocol shape (drop
`tool_grants` + `max_tool_calls`) to match the real `claude`
CLI surface and replaced `SubprocessAgentTransport` with
`CoderServiceAgentTransport`. The original subprocess
transport assumed CLI flags (`--tool-grants`,
`--max-tool-calls`, `--timeout`, `--response-schema`) that
don't exist on the real `claude` binary; it was never
exercised against a live install (only against
`MockAgentTransport`). Removing it cleans up dead code and
focuses the transport surface on the production path:
HTTP POST to `xauditor-coder-service`'s
`/agent_invocations` endpoint.

This module ships:

- `AgentTransport` Protocol — `invoke(...)` returning an
  `AgentResult`. Slim signature: `system_prompt`,
  `user_payload`, `response_model`, `timeout_seconds`,
  `project`. No tool grants (the coder-service container
  is the security sandbox); no max-tool-calls (the real
  `claude` CLI has no such flag; wall-clock timeout is
  the only enforceable cap).
- `AgentResult` dataclass — final answer + structured
  tool-call transcript + fallback metadata.
- `CoderServiceAgentTransport` — HTTP POST to
  `xauditor-coder-service`'s `/agent_invocations`
  endpoint. Production transport.
- `MockAgentTransport` — records calls and returns
  scripted results. Used by audit-side tests so the
  workflow's stage execution can be exercised without a
  live coder-service.

The transport DOES NOT know anything about the audit's
unit shape or stage semantics — it just takes a
system prompt + user payload + Pydantic response model
and returns the parsed `final_answer`. Audit-side
orchestration (persona injection, prompt-family selection,
fallback handling) lives in `stage_runner.py` and
`reconciler.py`.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Protocol

try:
    import httpx  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - httpx is a hard runtime dep
    httpx = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentResult:
    """One agent invocation's complete record.

    `final_answer` is the parsed Pydantic instance returned
    by the agent's `final_answer` tool call. `transcript`
    is the structured tool-call log persisted onto
    `findings.agentic_transcript` (alembic 0013) so PSIRT
    can replay the agent's evidence-collection trail.

    `fell_back` indicates the heuristic-fallback path fired —
    the agent failed to produce a valid `final_answer`
    within the budget. The `final_answer` field still holds
    a usable response_model instance (with default values)
    so downstream code consumes a stable shape.
    """

    final_answer: object
    transcript: list[dict[str, object]] = field(default_factory=list)
    tool_call_count: int = 0
    fell_back: bool = False
    fallback_reason: str = ""

    def transcript_as_payload(self) -> list[dict[str, object]]:
        """Project the transcript to a JSONB-friendly list."""
        return list(self.transcript)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class AgentTransport(Protocol):
    """Per-stage agent invocation transport.

    Implementations: `CoderServiceAgentTransport` (production
    — HTTP POST to coder-service's `/agent_invocations`),
    `MockAgentTransport` (tests).

    Implementations MUST:
    - Validate the agent's `final_answer` against the supplied
      `response_model` (Pydantic class) and fall back to
      defaults on validation failure.
    - Cap execution at `timeout_seconds` (wall-clock); on
      timeout, return a fallback `AgentResult`.
    - Return the structured tool-call transcript even on
      fallback (so PSIRT can review what the agent attempted).
    """

    def invoke(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, object],
        response_model: type,
        timeout_seconds: int,
        project: str | None = None,
    ) -> AgentResult:
        ...


# ---------------------------------------------------------------------------
# CoderServiceAgentTransport — production transport
# ---------------------------------------------------------------------------


_DEFAULT_ENDPOINT = "http://127.0.0.1:8090"
_REQUEST_TIMEOUT_SAFETY_SECONDS = 60


@dataclass
class CoderServiceAgentTransport:
    """POSTs `/agent_invocations` to xauditor-coder-service.

    The coder-service container hosts the single `claude`
    install for an xauditor deployment; both Coder
    verifications (per-finding, async) and agentic stage
    invocations (per-stage, synchronous) route through it.
    See `extend-coder-service-for-agent-invocations` for
    the endpoint contract.

    `endpoint` defaults to `http://127.0.0.1:8090` (matches
    the existing Coder transport's default). Unix-socket
    endpoints (`unix:///abs/path/socket`) work the same way
    they do for the HTTP Coder transport.

    `bearer_token_env`, when set, names an env var holding
    the bearer token for `Authorization: Bearer ...`
    headers. Unset → no auth header sent (matches
    coder-service's `enable_auth: false` setup).

    `request_timeout_safety_seconds` is the slack added to
    the per-call `timeout_seconds` for the HTTP request
    timeout. The orchestrator inside coder-service enforces
    `timeout_seconds` precisely; the HTTP client gives it a
    bit of extra room before timing out itself.

    The transport runs `GET /health` once at construction
    time as a startup probe; `RuntimeError` fires when the
    endpoint is unreachable so misconfigured operators see
    the failure before any per-stage call.
    """

    endpoint: str = _DEFAULT_ENDPOINT
    bearer_token_env: str | None = None
    request_timeout_safety_seconds: int = _REQUEST_TIMEOUT_SAFETY_SECONDS
    _probe_done: bool = field(default=False, init=False, repr=False)
    _client: object | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # Lazy probe — defer to first invoke so test instantiation
        # without a live coder-service doesn't fail. Workflow
        # construction triggers the probe before any audit work
        # via `_probe()` from `_stage_runner_init`.
        pass

    def _probe(self) -> None:
        if self._probe_done:
            return
        if httpx is None:
            raise RuntimeError(
                "CoderServiceAgentTransport requires `httpx` (a hard runtime "
                "dep of xauditor). Install it via `pip install httpx>=0.27`."
            )
        try:
            with self._http_client(timeout=10.0) as client:
                resp = client.get("/health")
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"CoderServiceAgentTransport startup probe failed: GET "
                f"{self.endpoint}/health raised {exc}. Verify "
                f"`audit.agentic.transport.coder_service.endpoint` and "
                f"that xauditor-coder-service is running."
            ) from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"CoderServiceAgentTransport startup probe failed: GET "
                f"{self.endpoint}/health returned HTTP {resp.status_code}."
                f" Verify the coder-service deployment."
            )
        self._probe_done = True

    def invoke(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, object],
        response_model: type,
        timeout_seconds: int,
        project: str | None = None,
    ) -> AgentResult:
        self._probe()
        body = {
            "system_prompt": system_prompt,
            "user_payload": user_payload,
            "response_schema": _schema_for(response_model),
            "timeout_seconds": timeout_seconds,
            "project": project,
        }
        request_timeout = float(timeout_seconds + self.request_timeout_safety_seconds)
        try:
            with self._http_client(timeout=request_timeout) as client:
                resp = client.post("/agent_invocations", json=body)
        except httpx.TimeoutException:
            return _fallback(
                response_model,
                "http_timeout",
            )
        except httpx.HTTPError as exc:
            return _fallback(
                response_model,
                f"coder_service_unavailable: {exc}",
            )

        if resp.status_code >= 400:
            # 4xx (auth / malformed request / unknown project) and 5xx
            # (infra failure) both map to fallback so the workflow
            # surface stays uniform regardless of which side failed.
            detail = resp.text[:200] if resp.text else ""
            return _fallback(
                response_model,
                f"coder_service_{resp.status_code}: {detail}".rstrip(),
            )

        try:
            envelope = resp.json()
        except json.JSONDecodeError:
            return _fallback(
                response_model,
                "coder_service_malformed_response",
            )

        return _parse_envelope(envelope, response_model)

    def _http_client(self, *, timeout: float):
        """Build an httpx.Client honouring unix-socket endpoints.

        Mirrors the pattern in `xauditor.audit.coder.HttpCoderTransport`:
        a `unix://` endpoint is rewritten to `http://localhost` and the
        client gets an `HTTPTransport(uds=...)` so the URI scheme used
        by httpx requests stays valid.
        """

        if self.endpoint.startswith("unix://"):
            socket_path = urllib.parse.urlparse(self.endpoint).path
            transport = httpx.HTTPTransport(uds=socket_path)
            base_url = "http://localhost"
        else:
            transport = None
            base_url = self.endpoint.rstrip("/")

        headers = {}
        if self.bearer_token_env:
            token = os.environ.get(self.bearer_token_env, "").strip()
            if token:
                headers["Authorization"] = f"Bearer {token}"

        if transport is not None:
            return httpx.Client(
                base_url=base_url,
                transport=transport,
                timeout=timeout,
                headers=headers,
            )
        return httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers=headers,
        )


# ---------------------------------------------------------------------------
# MockAgentTransport — test transport
# ---------------------------------------------------------------------------


@dataclass
class MockAgentTransport:
    """Records `invoke` calls and returns scripted results.

    Used by audit-side tests so the workflow's stage
    execution can be exercised without a live coder-service.
    Each call's arguments are stored on `calls`; the next
    entry in `scripted` is returned. When `scripted` runs
    out, the transport returns a default `no_issue`-shaped
    fallback.
    """

    scripted: list[AgentResult] = field(default_factory=list)
    calls: list[dict[str, object]] = field(default_factory=list)

    def invoke(
        self,
        *,
        system_prompt: str,
        user_payload: dict[str, object],
        response_model: type,
        timeout_seconds: int,
        project: str | None = None,
    ) -> AgentResult:
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_payload": user_payload,
                "response_model": response_model,
                "timeout_seconds": timeout_seconds,
                "project": project,
            }
        )
        idx = len(self.calls) - 1
        if idx < len(self.scripted):
            return self.scripted[idx]
        return _fallback(
            response_model,
            "MockAgentTransport scripted list exhausted",
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _schema_for(response_model: type) -> dict[str, object]:
    """Return the JSON schema coder-service validates final_answer
    against. Uses Pydantic's `model_json_schema` when available;
    falls back to an empty schema (coder-service then accepts
    any JSON object) when the model class isn't Pydantic-shaped.
    """

    schema_fn = getattr(response_model, "model_json_schema", None)
    if schema_fn is None:
        return {}
    try:
        return schema_fn()
    except Exception:  # noqa: BLE001 — best-effort fallback
        return {}


def _parse_envelope(envelope: object, response_model: type) -> AgentResult:
    """Project coder-service's `AgentInvocationResponse` body into
    an `AgentResult`. Validates the `final_answer` against the
    Pydantic response_model so downstream code gets the typed
    shape it expects (matching the `AnalyzerOutput` etc. surface
    `LLMClient._invoke_json` returns).
    """

    if not isinstance(envelope, dict):
        return _fallback(response_model, "coder_service_unexpected_envelope")

    fell_back = bool(envelope.get("fell_back"))
    fallback_reason = envelope.get("fallback_reason") or ""
    transcript_raw = envelope.get("transcript") or []
    transcript: list[dict[str, object]] = [
        item if isinstance(item, dict) else {"raw": item}
        for item in transcript_raw
    ]

    raw_final = envelope.get("final_answer")

    # When coder-service already flagged a fallback (timeout, schema
    # validation failure, subprocess crash, …), preserve that signal.
    # Try to validate the raw final_answer if present so downstream code
    # gets typed shape; on failure fall through to the defaults.
    if fell_back:
        if isinstance(raw_final, dict):
            parsed = _try_validate(raw_final, response_model)
            if parsed is not None:
                return AgentResult(
                    final_answer=parsed,
                    transcript=transcript,
                    tool_call_count=len(transcript),
                    fell_back=True,
                    fallback_reason=str(fallback_reason),
                )
        return _fallback(
            response_model,
            str(fallback_reason) or "coder_service_fallback",
            transcript=transcript,
        )

    if not isinstance(raw_final, dict):
        return _fallback(
            response_model,
            "coder_service_no_final_answer",
            transcript=transcript,
        )

    parsed = _try_validate(raw_final, response_model)
    if parsed is None:
        return _fallback(
            response_model,
            "coder_service_final_answer_validation_failed",
            transcript=transcript,
        )

    return AgentResult(
        final_answer=parsed,
        transcript=transcript,
        tool_call_count=len(transcript),
        fell_back=False,
        fallback_reason="",
    )


def _try_validate(payload: dict[str, object], response_model: type) -> object | None:
    validate_fn = getattr(response_model, "model_validate", None)
    if validate_fn is None:
        # Non-Pydantic test fixtures: just hand back the dict.
        return payload
    try:
        return validate_fn(payload)
    except Exception:  # noqa: BLE001 — Pydantic ValidationError surface varies
        return None


def _fallback(
    response_model: type,
    reason: str,
    *,
    transcript: list[dict[str, object]] | None = None,
) -> AgentResult:
    """Build the heuristic-fallback `AgentResult`.

    The `final_answer` is the response_model's defaults
    (typically a `no_issue` / `not_applicable` shape). Same
    surface as `LLMClient._invoke_json`'s `fallback_result`
    so downstream code handles agentic + prompt failures
    identically.
    """
    instance = _default_instance(response_model)
    return AgentResult(
        final_answer=instance,
        transcript=transcript or [],
        tool_call_count=len(transcript or []),
        fell_back=True,
        fallback_reason=reason,
    )


def _default_instance(response_model: type) -> object:
    """Return a default response_model instance.

    Uses `model_construct({})` when available (Pydantic v2)
    so required fields default to the field's
    Pydantic-defined defaults; falls back to no-arg
    construction for non-Pydantic models.
    """
    construct = getattr(response_model, "model_construct", None)
    if construct is not None:
        try:
            return construct()
        except Exception:  # noqa: BLE001
            pass
    try:
        return response_model()
    except TypeError:
        return {}


__all__ = [
    "AgentResult",
    "AgentTransport",
    "CoderServiceAgentTransport",
    "MockAgentTransport",
]
