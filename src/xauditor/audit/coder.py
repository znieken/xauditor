"""Repo-global coder verification agent.

This module implements the fourth audit-stage agent — the ``coder`` —
defined by the ``repo-global-coder-verification`` capability. The coder
agent consumes a finding produced by the upstream analyzer / validator /
exploiter chain, hands it to an external repo-global verifier (today the
Claude Code CLI), and emits a structured verdict that is appended to the
finding under the ``coder_*`` fields.

Design notes
------------

* The transport is hidden behind a ``CoderTransport`` ``Protocol`` so the
  Claude Code CLI can be replaced in a future change without touching the
  workflow integration.
* The CLI subprocess inherits **only** an explicit allowlist of environment
  variables. ``XAUDITOR_LLM_PROVIDERS_*`` and any other internal credentials
  are dropped before ``execve``.
* ``request_timeout_seconds`` is enforced with ``SIGTERM`` then ``SIGKILL``
  (after a 2-second grace) so a hung CLI cannot stall the audit run.
* The dispatcher uses a ``concurrent.futures.ThreadPoolExecutor`` whose
  ``max_workers`` equals ``coder.concurrency``. Threads block on
  ``subprocess.wait``; that is intentional and far cheaper than rewiring
  the synchronous audit workflow to ``asyncio``.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, Sequence

if TYPE_CHECKING:
    from xauditor.audit._cancellation import RunCancellation

from xauditor.config import CoderConfig
from xauditor.models import (
    CODER_STATUS_FAIL,
    CoderEvidence,
    normalize_coder_status,
)
from xauditor.runtime_logging import RuntimeLogger

try:
    import httpx  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised only when httpx absent
    httpx = None  # type: ignore[assignment]


log = logging.getLogger("xauditor.audit.coder")


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "TERM",
    # Claude Code reads ANTHROPIC_API_KEY for authentication. When the
    # operator hasn't set ``coder.model_api_key`` in xauditor config, we
    # let the parent shell's value pass through so the CLI can use the
    # ambient credentials. When ``coder.model_api_key`` IS set, the
    # transport overrides this entry explicitly (see ``invoke``).
    "ANTHROPIC_API_KEY",
    # Claude Code 2.x drives model / endpoint / auth purely through
    # env vars. ``ANTHROPIC_BASE_URL`` points at a non-default
    # endpoint (internal gateway, proxy, 3P provider speaking the
    # Anthropic API protocol). ``ANTHROPIC_MODEL`` selects the model
    # by name. Claude has NO CLI flag for the base URL; ``--model``
    # exists but we standardise on env-only so the worker spawn
    # path is uniform across all three values.
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
)
ENV_ALLOWLIST_PREFIXES: tuple[str, ...] = ("CLAUDE_",)

# Cap stdout / stderr captured into persisted analysis / debug fields so a
# runaway CLI cannot bloat the database row or the Markdown artifact.
_STDOUT_TRUNCATE_BYTES = 256 * 1024  # 256 KB of analysis text is plenty
_STDERR_TRUNCATE_BYTES = 32 * 1024
_ANALYSIS_TRUNCATE_CHARS = 16_000
_REASON_TRUNCATE_CHARS = 2_000

CODER_SYSTEM_PROMPT = """\
You are an internal product-security reviewer running in a CLI inside a
target repository. You are given one finding produced by an upstream
audit pipeline (analyzer + exploitation + validator), the call chain of
the affected path, and the source of every function on that path.

Your job is to read the rest of the repository (any file, any module —
you have full read access via your tools) and verify whether the finding
is correct. Surface sanitizers, capability checks, type constraints, or
control-flow guards in sibling modules that the path-local agents could
not see. Do not re-audit the path; trust the upstream evidence except
where repo-global context contradicts it.

Reply with a single JSON object on stdout, no Markdown, no preamble,
matching this schema:

{
  "status": "Verified" | "Not Verified" | "Inconclusive",
  "analysis": "<free-form explanation>",
  "reason": "<short justification distilled from analysis>",
  "call_chain_evidence": [
    {
      "file_path": "<repo-relative path>",
      "function_name": "<symbol or null>",
      "snippet": "<verbatim code snippet>",
      "language": "<python | typescript | go | c | cpp | rust | java | ...>",
      "role": "call_site | definition | sanitizer | capability_check | supporting"
    }
  ]
}

Status semantics:
- "Verified"     — the finding holds up under repo-global review.
- "Not Verified" — the finding is contradicted by code outside the path
                   (sanitizer, capability check, unreachable in practice, …).
- "Inconclusive" — repo-global evidence is missing or mixed; flag it for
                   a human reviewer.

OUTPUT FORMAT — read carefully:
- Your ENTIRE response MUST be a single valid JSON object.
- DO NOT wrap the JSON in markdown code fences (no ``` json ... ```).
- DO NOT include any preamble like "Here is the analysis:" or postamble
  like "Let me know if you need more detail.".
- DO NOT use the literal "json" tag, indentation hints, or any text
  whatsoever before the opening '{' or after the closing '}'.
- The first character of your output MUST be '{' and the last
  character MUST be '}'. Anything else and the orchestrator will
  flag your verdict as a transport error.
"""


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CoderResult:
    """The settled output of one coder verification task.

    ``cli_exit_code`` and ``cli_stderr`` are operator-debug fields. They
    are persisted into ``report.coder_findings`` so an outage can be
    triaged from the DB, but they are NEVER surfaced in the portal UI.
    """

    status: str
    analysis: str = ""
    reason: str = ""
    evidence: tuple[CoderEvidence, ...] = ()
    cli_exit_code: int | None = None
    cli_stderr: str | None = None
    duration_ms: int | None = None


# --------------------------------------------------------------------------
# Transport protocol
# --------------------------------------------------------------------------


class CoderTransport(Protocol):
    """Run one verification request synchronously and return a ``CoderResult``.

    Implementations MUST be safe to call from a worker thread inside a
    ``ThreadPoolExecutor`` and MUST surface every transport failure as a
    ``CoderResult`` with ``status="Inconclusive"`` rather than raising.
    """

    def invoke(self, payload: Mapping[str, Any]) -> CoderResult: ...


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def scrub_environment(parent_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a minimal env map for the Claude Code subprocess.

    Inherits only the explicit allowlist plus any variable whose name
    starts with one of the allowed prefixes. ``XAUDITOR_LLM_PROVIDERS_*``
    and other internal credentials are dropped on purpose.
    """

    source: Mapping[str, str] = parent_env if parent_env is not None else os.environ
    scrubbed: dict[str, str] = {}
    for name, value in source.items():
        if name in ENV_ALLOWLIST or any(name.startswith(prefix) for prefix in ENV_ALLOWLIST_PREFIXES):
            scrubbed[name] = value
    return scrubbed


def build_cli_argv(
    cli_command: Sequence[str],
    *,
    thinking_effort: str | None = None,
) -> list[str]:
    """Build the argv for the Claude Code subprocess.

    Two real flags only:

    - ``-p`` (``--print``): non-interactive print-and-exit mode. We
      always pipe stdin and read stdout, never want the interactive
      UI; without this flag claude's stdin handling is fuzzy.
    - ``--effort <level>``: thinking-effort knob. Verified against
      ``claude --help`` (claude-code 2.1.122) — ``--thinking-effort``
      does NOT exist; ``--effort`` is the real spelling.

    Model / endpoint / API key are NOT passed via argv. Claude Code
    2.x reads them from environment variables (``ANTHROPIC_MODEL``,
    ``ANTHROPIC_BASE_URL``, ``ANTHROPIC_API_KEY``); see
    ``ENV_ALLOWLIST``. Callers MUST inject those env vars on the
    spawned subprocess (subprocess transport) or rely on the worker
    container's baked env (HTTP transport — ``build_run_argv``
    bakes them at ``coder init`` time).

    Operators who need a different argv shape can supply the entire
    ``cli_command`` as a list (e.g.
    ``["wrapper", "claude", "--my-flag", "value"]``); the two
    standard flags above are appended after.
    """

    argv = list(cli_command)
    argv.append("-p")
    if thinking_effort:
        argv.extend(["--effort", thinking_effort])
    return argv


def _truncate_bytes(data: bytes, limit: int) -> bytes:
    if len(data) <= limit:
        return data
    suffix = b"\n... [truncated]\n"
    return data[: max(0, limit - len(suffix))] + suffix


def _truncate_str(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(" ... [truncated]"))] + " ... [truncated]"


def _redact_text(text: str, logger: RuntimeLogger | None) -> str:
    if logger is None:
        return text
    redactor = getattr(logger, "_redact", None)
    if callable(redactor):
        return redactor(text)
    return text


# --------------------------------------------------------------------------
# Payload renderer
# --------------------------------------------------------------------------


def render_coder_payload(
    *,
    finding: Mapping[str, Any],
    path_context: Mapping[str, Any],
    analyzer: Mapping[str, Any] | None = None,
    exploitation: Mapping[str, Any] | None = None,
    validator: Mapping[str, Any] | None = None,
    validator_debate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Marshal the upstream chain output into the coder request payload.

    The schema is the JSON contract that ``CODER_SYSTEM_PROMPT`` describes
    on the model side; keep them in lock-step.
    """

    payload: dict[str, Any] = {
        "system_prompt": CODER_SYSTEM_PROMPT,
        "finding": dict(finding),
        "path": {
            "call_chain": list(path_context.get("call_chain", [])),
            "function_definitions": list(path_context.get("function_definitions", [])),
            "referenced_symbols": list(path_context.get("referenced_symbols", [])),
        },
        "upstream": {
            "analyzer": dict(analyzer) if analyzer is not None else None,
            "exploitation": dict(exploitation) if exploitation is not None else None,
            "validator": dict(validator) if validator is not None else None,
            "validator_debate": dict(validator_debate) if validator_debate is not None else None,
        },
    }
    return payload


# --------------------------------------------------------------------------
# Response parser
# --------------------------------------------------------------------------


def _repair_json_typos(text: str) -> str:
    """Fix the two most common JSON typos in LLM stdout.

    - **Missing comma between values** — Claude regularly emits a JSON
      object where a closing ``"``/``]``/``}`` is followed by whitespace
      and the next key's opening ``"`` (or array's ``[``, or nested
      object's ``{``) without a separator. Strict ``json.loads`` rejects
      it. Observed shape::

          "snippet": "..."
                "language": "cpp",

    - **Trailing comma before ``}`` / ``]``** — second most common,
      sometimes from extended-thinking models that "edited" the output
      mid-stream.

    The fixer is string-aware: it walks character-by-character, tracking
    string boundaries with the same escape semantics as ``json.loads``,
    and only injects / drops commas in structural positions. String
    contents are preserved verbatim. The pass is **idempotent on
    well-formed JSON** (it only adds/drops commas at positions where the
    spec already says they should be), so the fast path of
    ``json.loads(extracted)`` is unaffected — the repair only runs as a
    fallback after a strict-parse failure.
    """

    out: list[str] = []
    n = len(text)
    i = 0
    in_str = False
    esc = False
    while i < n:
        ch = text[i]
        if esc:
            out.append(ch)
            esc = False
            i += 1
            continue
        if in_str:
            out.append(ch)
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
                # String just closed — peek for missing-comma.
                j = i + 1
                while j < n and text[j] in " \t\n\r":
                    j += 1
                if j < n and text[j] in '"{[':
                    out.append(",")
            i += 1
            continue
        # Outside any string.
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\n\r":
                j += 1
            if j < n and text[j] in "}]":
                # Trailing comma — drop it.
                i += 1
                continue
        out.append(ch)
        if ch in "]}":
            j = i + 1
            while j < n and text[j] in " \t\n\r":
                j += 1
            if j < n and text[j] in '"{[':
                out.append(",")
        i += 1
    return "".join(out)


def _extract_json_object(stdout: str) -> str | None:
    """Pull the first complete JSON object out of claude's stdout.

    The system prompt instructs claude to emit a single JSON object,
    no preamble, no markdown fences. In practice — observed against
    claude-code 2.1.122 — claude regularly wraps its response in
    ```json ... ``` fences anyway, occasionally adds a "Here's the
    analysis:" preamble, or trails prose after the JSON. Without
    leniency every such response surfaces as
    ``coder_status: "Inconclusive (transport error: invalid JSON)"``
    even though the model produced a perfectly valid analysis.

    This helper handles the three observed shapes:
      1. Markdown fences: strip leading ```json (or ```) and trailing
         ```.
      2. Preamble / postamble: locate the first '{' and walk through
         brace pairs (with string-aware escaping) to its matching
         '}'. Drop everything before and after.
      3. No JSON at all (refusal / pure prose): return ``None``.

    Returns the substring that should be JSON, or ``None`` when no
    balanced object is present.
    """

    text = stdout.strip()
    # 1. Strip markdown fences if present.
    if text.startswith("```"):
        # Remove opening fence (with optional language tag like 'json').
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl + 1:]
        else:
            text = text[3:]
        # Remove trailing fence.
        text = text.rstrip()
        if text.endswith("```"):
            text = text[:-3].rstrip()

    # 2. Brace-walk to find the first complete JSON object. Tolerates
    # preamble before the opening brace and any postamble after the
    # closer.
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if in_str:
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    # 3. Unbalanced braces — treat as no JSON.
    return None


def _parse_evidence(items: Any) -> tuple[CoderEvidence, ...]:
    if not isinstance(items, list):
        return ()
    parsed: list[CoderEvidence] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        file_path = str(raw.get("file_path", "")).strip()
        if not file_path:
            continue
        function_name_raw = raw.get("function_name")
        function_name = (
            str(function_name_raw).strip() if function_name_raw not in (None, "") else None
        )
        snippet = str(raw.get("snippet", ""))
        language_raw = raw.get("language")
        language = str(language_raw).strip() if language_raw not in (None, "") else None
        role = str(raw.get("role", "supporting")).strip() or "supporting"
        parsed.append(
            CoderEvidence(
                file_path=file_path,
                function_name=function_name,
                snippet=snippet,
                language=language,
                role=role,
            )
        )
    return tuple(parsed)


def parse_coder_response(
    *,
    stdout: str,
    stderr: str,
    exit_code: int,
    duration_ms: int,
    logger: RuntimeLogger | None = None,
) -> CoderResult:
    """Parse the Claude Code subprocess output into a ``CoderResult``.

    Any failure path (non-zero exit, malformed JSON, missing ``status``)
    yields ``status="Inconclusive"`` so a transport hiccup never silently
    drops a finding's verdict.
    """

    redacted_stderr = _redact_text(_truncate_str(stderr, _STDERR_TRUNCATE_BYTES), logger) or None
    if exit_code != 0:
        analysis = _redact_text(_truncate_str(stdout, _ANALYSIS_TRUNCATE_CHARS), logger)
        return CoderResult(
            status=CODER_STATUS_FAIL,
            analysis=analysis,
            reason=f"transport error: exit {exit_code}",
            evidence=(),
            cli_exit_code=exit_code,
            cli_stderr=redacted_stderr,
            duration_ms=duration_ms,
        )

    stripped = stdout.strip()
    if not stripped:
        return CoderResult(
            status=CODER_STATUS_FAIL,
            analysis="",
            reason="transport error: empty stdout",
            evidence=(),
            cli_exit_code=exit_code,
            cli_stderr=redacted_stderr,
            duration_ms=duration_ms,
        )

    extracted = _extract_json_object(stripped)
    if extracted is None:
        analysis = _redact_text(_truncate_str(stdout, _ANALYSIS_TRUNCATE_CHARS), logger)
        return CoderResult(
            status=CODER_STATUS_FAIL,
            analysis=analysis,
            reason="transport error: no JSON object in stdout",
            evidence=(),
            cli_exit_code=exit_code,
            cli_stderr=redacted_stderr,
            duration_ms=duration_ms,
        )
    try:
        parsed = json.loads(extracted)
    except json.JSONDecodeError:
        # Common LLM typo: missing comma between key-value pairs, or a
        # trailing comma before ``}``/``]``. Run the structural repair
        # pass and try once more before declaring transport failure.
        repaired = _repair_json_typos(extracted)
        try:
            parsed = json.loads(repaired)
        except json.JSONDecodeError:
            analysis = _redact_text(_truncate_str(stdout, _ANALYSIS_TRUNCATE_CHARS), logger)
            return CoderResult(
                status=CODER_STATUS_FAIL,
                analysis=analysis,
                reason="transport error: malformed JSON",
                evidence=(),
                cli_exit_code=exit_code,
                cli_stderr=redacted_stderr,
                duration_ms=duration_ms,
            )
        if logger is not None:
            # Worth logging — if this fires often we should look at the
            # prompt instead of just patching parser-side.
            logger.warning(
                "Coder stdout had a JSON typo (missing/trailing comma); "
                "structural repair recovered the response."
            )

    if not isinstance(parsed, Mapping):
        analysis = _redact_text(_truncate_str(stdout, _ANALYSIS_TRUNCATE_CHARS), logger)
        return CoderResult(
            status=CODER_STATUS_FAIL,
            analysis=analysis,
            reason="transport error: response is not a JSON object",
            evidence=(),
            cli_exit_code=exit_code,
            cli_stderr=redacted_stderr,
            duration_ms=duration_ms,
        )

    raw_status = parsed.get("status")
    status = normalize_coder_status(raw_status)
    raw_analysis = str(parsed.get("analysis", ""))
    raw_reason = str(parsed.get("reason", ""))
    analysis = _redact_text(_truncate_str(raw_analysis, _ANALYSIS_TRUNCATE_CHARS), logger)
    reason = _redact_text(_truncate_str(raw_reason, _REASON_TRUNCATE_CHARS), logger)
    evidence = _parse_evidence(parsed.get("call_chain_evidence"))

    return CoderResult(
        status=status,
        analysis=analysis,
        reason=reason,
        evidence=evidence,
        cli_exit_code=exit_code,
        cli_stderr=redacted_stderr,
        duration_ms=duration_ms,
    )


# --------------------------------------------------------------------------
# Claude Code CLI transport
# --------------------------------------------------------------------------


class ClaudeCodeCliTransport:
    """Spawn the Claude Code CLI as a subprocess for one verification.

    The transport is intentionally thread-safe per call: every ``invoke``
    creates an independent subprocess, so the executor can fan out without
    shared per-instance mutable state.
    """

    def __init__(
        self,
        config: CoderConfig,
        *,
        logger: RuntimeLogger | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self._parent_env: Mapping[str, str] = env if env is not None else os.environ

    # `cwd` for the subprocess (resolved once per call so config changes do
    # not leak across audit runs).
    def _resolve_cwd(self) -> str | None:
        if not self.config.working_directory:
            return None
        path = Path(self.config.working_directory)
        return str(path) if path.exists() else str(path)

    def invoke(self, payload: Mapping[str, Any]) -> CoderResult:
        argv = build_cli_argv(
            self.config.cli_command,
            thinking_effort=self.config.thinking_effort,
        )
        # Subprocess transport: inject the three claude env vars from
        # xauditor config, overriding any parent-shell values. Mirrors
        # what ``build_run_argv`` bakes into the container env for the
        # HTTP transport — same source of truth (the YAML), same set
        # of three vars.
        env = scrub_environment(self._parent_env)
        if self.config.model_api_key:
            env["ANTHROPIC_API_KEY"] = self.config.model_api_key
        if self.config.model_url:
            env["ANTHROPIC_BASE_URL"] = self.config.model_url
        if self.config.model_name:
            env["ANTHROPIC_MODEL"] = self.config.model_name
        cwd = self._resolve_cwd()
        encoded_payload = json.dumps(payload, default=str).encode("utf-8")

        start = monotonic()
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=cwd,
                close_fds=True,
            )
        except FileNotFoundError as exc:
            duration_ms = int((monotonic() - start) * 1000)
            return CoderResult(
                status=CODER_STATUS_FAIL,
                analysis="",
                reason=f"transport error: cli not found ({exc})",
                evidence=(),
                cli_exit_code=None,
                cli_stderr=None,
                duration_ms=duration_ms,
            )
        except OSError as exc:
            duration_ms = int((monotonic() - start) * 1000)
            return CoderResult(
                status=CODER_STATUS_FAIL,
                analysis="",
                reason=f"transport error: spawn failed ({exc})",
                evidence=(),
                cli_exit_code=None,
                cli_stderr=None,
                duration_ms=duration_ms,
            )

        try:
            stdout_bytes, stderr_bytes = process.communicate(
                input=encoded_payload,
                timeout=self.config.request_timeout_seconds,
            )
            exit_code = process.returncode
        except subprocess.TimeoutExpired:
            self._terminate_process(process)
            stdout_bytes, stderr_bytes = self._drain_after_kill(process)
            duration_ms = int((monotonic() - start) * 1000)
            redacted_stderr = _redact_text(
                _truncate_str(stderr_bytes.decode("utf-8", errors="replace"), _STDERR_TRUNCATE_BYTES),
                self.logger,
            ) or None
            return CoderResult(
                status=CODER_STATUS_FAIL,
                analysis="",
                reason=f"transport error: timed out after {self.config.request_timeout_seconds}s",
                evidence=(),
                cli_exit_code=process.returncode,
                cli_stderr=redacted_stderr,
                duration_ms=duration_ms,
            )

        duration_ms = int((monotonic() - start) * 1000)
        stdout_bytes = _truncate_bytes(stdout_bytes, _STDOUT_TRUNCATE_BYTES)
        stderr_bytes = _truncate_bytes(stderr_bytes, _STDERR_TRUNCATE_BYTES)
        return parse_coder_response(
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            exit_code=exit_code,
            duration_ms=duration_ms,
            logger=self.logger,
        )

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            return
        try:
            process.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass
        if process.poll() is not None:
            return
        try:
            if hasattr(signal, "SIGKILL"):
                process.kill()
            else:  # pragma: no cover - non-POSIX
                process.terminate()
        except OSError:
            return
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:  # pragma: no cover - process won't die
            pass

    @staticmethod
    def _drain_after_kill(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
        try:
            stdout_bytes, stderr_bytes = process.communicate(timeout=2)
        except (subprocess.TimeoutExpired, ValueError):
            stdout_bytes, stderr_bytes = b"", b""
        return stdout_bytes, stderr_bytes


# --------------------------------------------------------------------------
# HTTP transport (xauditor → xauditor-coder-service)
# --------------------------------------------------------------------------


def _parse_evidence_payload(items: Any) -> tuple[CoderEvidence, ...]:
    """Same shape as the subprocess parser but accepts already-parsed JSON."""

    if not isinstance(items, list):
        return ()
    parsed: list[CoderEvidence] = []
    for raw in items:
        if not isinstance(raw, Mapping):
            continue
        file_path = str(raw.get("file_path", "")).strip()
        if not file_path:
            continue
        function_name_raw = raw.get("function_name")
        function_name = (
            str(function_name_raw).strip() if function_name_raw not in (None, "") else None
        )
        snippet = str(raw.get("snippet", ""))
        language_raw = raw.get("language")
        language = str(language_raw).strip() if language_raw not in (None, "") else None
        role = str(raw.get("role", "supporting")).strip() or "supporting"
        parsed.append(
            CoderEvidence(
                file_path=file_path,
                function_name=function_name,
                snippet=snippet,
                language=language,
                role=role,
            )
        )
    return tuple(parsed)


def _coder_result_from_service_payload(result: Mapping[str, Any]) -> CoderResult:
    """Reconstruct a ``CoderResult`` from the service's `done` JSON payload."""

    raw_status = result.get("status")
    return CoderResult(
        status=normalize_coder_status(raw_status),
        analysis=str(result.get("analysis", "")),
        reason=str(result.get("reason", "")),
        evidence=_parse_evidence_payload(result.get("evidence")),
        cli_exit_code=result.get("cli_exit_code"),
        cli_stderr=result.get("cli_stderr"),
        duration_ms=result.get("duration_ms"),
    )


_TRANSPORT_ERROR_KIND_RULES: tuple[tuple[str, str], ...] = (
    # Order matters — first match wins. Patterns are substrings of the
    # ``reason`` string passed to ``_fail``.
    ("connect failed", "connection_refused"),
    ("submit timed out", "timeout"),
    ("poll timed out", "timeout"),
    ("timed out after", "timeout"),
    ("poll deadline exceeded", "timeout"),
    ("project no longer available", "project_not_found"),
    ("idempotency", "idempotency_conflict"),
    ("HTTP 401", "auth"),
    ("HTTP 403", "auth"),
    ("submit HTTP 5", "http_5xx"),
    ("poll HTTP 5", "http_5xx"),
    ("submit HTTP 4", "http_4xx"),
    ("poll HTTP 4", "http_4xx"),
    ("job not found", "http_4xx"),
    ("submit response", "parse_error"),
    ("poll response", "parse_error"),
    ("invalid JSON", "parse_error"),
    ("malformed JSON", "parse_error"),
    ("no JSON object", "parse_error"),
    ("empty stdout", "parse_error"),
    ("not a JSON object", "parse_error"),
    ("done response missing result", "parse_error"),
    ("cli not found", "subprocess_exit"),
    ("spawn failed", "subprocess_exit"),
    ("exit ", "subprocess_exit"),
)


def _classify_transport_error(reason: str) -> str:
    """Map a Fail-result ``reason`` string to a structured ``error_kind``.

    Falls back to ``subprocess_crashed`` for unrecognised reasons since
    the only un-categorised path is the catch-all ``except Exception``
    in the dispatcher's task wrapper.
    """

    for needle, kind in _TRANSPORT_ERROR_KIND_RULES:
        if needle in reason:
            return kind
    return "subprocess_crashed"


def _log_coder_transport_failure(
    *,
    logger: RuntimeLogger | None,
    run_id: str,
    finding_id: str,
    project: str,
    endpoint: str,
    error_kind: str,
    error_detail: str,
) -> None:
    """Emit one structured ERROR log entry for a coder transport failure.

    Per spec, every Fail outcome SHALL trigger exactly one entry with
    the ``event=coder.transport_failure`` shape. ``project`` and
    ``endpoint`` SHALL be the literal ``-`` when not applicable
    (subprocess transport, missing config) so downstream log parsers
    see a uniform shape.
    """

    if logger is None:
        return
    detail = (error_detail or "").strip()
    if len(detail) > 200:
        detail = detail[:197] + "..."
    logger.error_kv(
        "coder.transport_failure",
        run_id=run_id or "-",
        finding_id=finding_id or "-",
        project=project or "-",
        endpoint=endpoint or "-",
        error_kind=error_kind,
        error_detail=detail,
    )


def _fail(reason: str) -> CoderResult:
    """Return a Fail-status CoderResult.

    Used for transport-level failures (network, HTTP error, timeout,
    parse error, subprocess crash). Distinct from claude-side
    deliberation outcomes (``Inconclusive``); ``Fail`` findings are
    retryable via ``xauditor audit resume``.
    """

    return CoderResult(
        status=CODER_STATUS_FAIL,
        analysis="",
        reason=reason,
        evidence=(),
        cli_exit_code=None,
        cli_stderr=None,
        duration_ms=None,
    )




class HttpCoderTransport:
    """Talk to ``xauditor-coder-service`` over HTTP.

    The transport is sync (uses ``httpx.Client``) and runs on the
    existing ``CoderDispatcher`` worker thread, so blocking I/O is fine.

    Endpoint URL forms:
      * ``http://host:port`` — plain HTTP
      * ``https://host:port`` — HTTPS (cert verification follows stdlib defaults)
      * ``unix:///abs/path/to/socket`` — Unix domain socket; we synthesise
        a ``http://localhost`` base and route through ``HTTPTransport(uds=...)``
    """

    def __init__(
        self,
        config: CoderConfig,
        *,
        logger: RuntimeLogger | None = None,
        run_id: str = "",
    ) -> None:
        if httpx is None:  # pragma: no cover - exercised when httpx absent
            raise RuntimeError(
                "HttpCoderTransport requires the `httpx` package; "
                "install xauditor with its standard dependencies."
            )
        self.config = config
        self.logger = logger
        self.run_id = run_id or "default-run"
        self._client = self._build_client()

    # client construction -----------------------------------------------------

    def _build_client(self):  # type: ignore[no-untyped-def]
        endpoint = self.config.endpoint
        if not endpoint:
            raise ValueError("HttpCoderTransport requires coder.endpoint to be set.")
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.config.enable_auth:
            headers["Authorization"] = f"Bearer {self.config.endpoint_token}"
        # We give the per-request HTTP timeout enough headroom that the
        # service's own timeout fires first; if the client times out before
        # the service can answer we'd return Inconclusive without giving
        # the worker a chance to clean up.
        timeout = httpx.Timeout(
            connect=10.0,
            read=float(self.config.request_timeout_seconds * 2),
            write=30.0,
            pool=10.0,
        )
        if endpoint.startswith("unix://"):
            socket_path = endpoint[len("unix://"):]
            transport = httpx.HTTPTransport(uds=socket_path)
            return httpx.Client(
                base_url="http://localhost",
                transport=transport,
                headers=headers,
                timeout=timeout,
            )
        # http:// or https://
        return httpx.Client(
            base_url=endpoint.rstrip("/"),
            headers=headers,
            timeout=timeout,
        )

    # CoderTransport protocol -------------------------------------------------

    def invoke(self, payload: Mapping[str, Any]) -> CoderResult:
        finding_id = self._extract_finding_id(payload)
        idempotency_key = f"{self.run_id}::{finding_id}" if finding_id else f"{self.run_id}::anon-{monotonic()}"
        # The HTTP request body NO LONGER carries model_url / model_name
        # / model_api_key. Those three values are baked into the worker
        # container's env at ``xauditor coder init`` time (see
        # ``build_run_argv``); the worker reads them from its own env
        # when spawning claude, not from the request body. Only
        # ``thinking_effort`` and the timeout still travel per-request,
        # because they may legitimately vary across runs (a future
        # capability) without requiring a container rebuild.
        # ``project`` routes per-finding work to the right
        # ``/workspace/<project>/`` subdirectory inside the multi-project
        # coder container. Resolved at audit-startup from
        # ``coder.effective_project_name`` (post-shim). Empty string when
        # the legacy single-repo container is in use; the service ignores
        # the field in that mode.
        request_body = {
            "idempotency_key": idempotency_key,
            "project": (self.config.effective_project_name or "").strip(),
            "payload": dict(payload),
            "claude_args": {
                "thinking_effort": self.config.thinking_effort,
            },
            "request_timeout_seconds": self.config.request_timeout_seconds,
        }

        try:
            submit = self._client.post("/verifications", json=request_body)
        except httpx.ConnectError as exc:
            return _fail(f"transport error: connect failed ({exc})")
        except httpx.TimeoutException as exc:
            return _fail(f"transport error: submit timed out ({exc})")
        except httpx.HTTPError as exc:  # noqa: BLE001 - surface as Inconclusive
            return _fail(f"transport error: submit failed ({exc})")
        # Multi-project response codes (since `multi-project-coder-service`):
        # 400 → project name failed allowlist or contained traversal;
        # 404 → named project does not exist on the service's /workspace;
        # 409 → idempotency key bound to a different project. Each
        # surfaces as ``Fail`` with a categorical reason so the dispatcher
        # log + portal chip carry the right diagnosis.
        if submit.status_code == 400:
            return _fail(
                "transport error: project name rejected by service "
                f"(HTTP 400: {submit.text[:200]})"
            )
        if submit.status_code == 404:
            return _fail("project no longer available")
        if submit.status_code == 409:
            return _fail(
                "transport error: idempotency conflict "
                f"(HTTP 409: {submit.text[:200]})"
            )
        if submit.status_code not in (200, 201):
            return _fail(
                f"transport error: submit HTTP {submit.status_code}: "
                f"{submit.text[:200]}"
            )
        try:
            submit_body = submit.json()
        except ValueError:
            return _fail("transport error: submit response not JSON")
        job_id = str(submit_body.get("job_id", "")).strip()
        if not job_id:
            return _fail("transport error: submit response missing job_id")

        # long-poll
        return self._poll_until_settled(job_id)

    def _poll_until_settled(self, job_id: str) -> CoderResult:
        path = f"/verifications/{job_id}"
        # Total poll budget = 2x the per-job timeout, giving the service time
        # to enforce its own SIGTERM/SIGKILL and respond before we give up.
        deadline = monotonic() + (self.config.request_timeout_seconds * 2)
        interval = max(0.1, float(self.config.poll_interval_seconds))
        while True:
            try:
                resp = self._client.get(path)
            except httpx.ConnectError as exc:
                return _fail(f"transport error: connect failed ({exc})")
            except httpx.TimeoutException as exc:
                return _fail(f"transport error: poll timed out ({exc})")
            except httpx.HTTPError as exc:  # noqa: BLE001
                return _fail(f"transport error: poll failed ({exc})")
            if resp.status_code == 404:
                return _fail(
                    "transport error: job not found (service likely restarted)"
                )
            if resp.status_code != 200:
                return _fail(
                    f"transport error: poll HTTP {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
            try:
                state = resp.json()
            except ValueError:
                return _fail("transport error: poll response not JSON")
            status = str(state.get("status", "pending"))
            if status == "done":
                result = state.get("result")
                if not isinstance(result, Mapping):
                    return _fail("transport error: done response missing result")
                return _coder_result_from_service_payload(result)
            if status in ("error", "cancelled"):
                err = state.get("error") or status
                return _fail(f"transport error: {err}")
            if monotonic() >= deadline:
                return _fail(
                    f"transport error: poll deadline exceeded "
                    f"(>{self.config.request_timeout_seconds * 2}s)"
                )
            _sleep(interval)

    # cancel hook used by CoderDispatcher.cancel_all on KeyboardInterrupt
    def cancel(self, idempotency_key: str) -> None:
        """Best-effort DELETE of a job whose idempotency_key may still be running.

        We don't track service-side `job_id`s on the xauditor side (the
        long-poll loop holds it locally on the worker thread), so cancel is
        a no-op for in-flight jobs. The service will time them out via its
        own ``request_timeout_seconds`` watchdog. The dispatcher still calls
        this method for symmetry with the subprocess transport.
        """

        # Future enhancement: maintain a (idempotency_key -> job_id) map
        # written by ``invoke`` so cancellation can reach running jobs.
        return None

    @staticmethod
    def _extract_finding_id(payload: Mapping[str, Any]) -> str:
        finding = payload.get("finding")
        if isinstance(finding, Mapping):
            value = finding.get("finding_id")
            if value:
                return str(value)
        return ""

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - close is best-effort
            pass


# --------------------------------------------------------------------------
# Coder agent
# --------------------------------------------------------------------------


class CoderAgent:
    """High-level wrapper combining the transport, the parser, and the renderer.

    Workflow code interacts with the agent (or, more commonly, with the
    ``CoderDispatcher`` below). The agent is purely synchronous; the
    dispatcher provides the asynchronous fan-out.
    """

    def __init__(
        self,
        *,
        transport: CoderTransport,
        logger: RuntimeLogger | None = None,
    ) -> None:
        self.transport = transport
        self.logger = logger

    @classmethod
    def from_config(
        cls,
        config: CoderConfig,
        *,
        logger: RuntimeLogger | None = None,
        env: Mapping[str, str] | None = None,
        run_id: str = "",
    ) -> "CoderAgent":
        if config.transport == "http":
            transport: CoderTransport = HttpCoderTransport(
                config, logger=logger, run_id=run_id,
            )
        else:
            transport = ClaudeCodeCliTransport(config, logger=logger, env=env)
        return cls(transport=transport, logger=logger)

    def verify(self, payload: Mapping[str, Any]) -> CoderResult:
        result = self.transport.invoke(payload)
        if self.logger is not None:
            self.logger.debug_kv(
                "Coder verification settled",
                status=result.status,
                duration_ms=result.duration_ms,
                exit_code=result.cli_exit_code,
                evidence_count=len(result.evidence),
            )
        if result.status == CODER_STATUS_FAIL:
            _log_coder_transport_failure(
                logger=self.logger,
                run_id=self._run_id_for_logging(),
                finding_id=self._finding_id_for_logging(payload),
                project=self._project_for_logging(),
                endpoint=self._endpoint_for_logging(),
                error_kind=_classify_transport_error(result.reason),
                error_detail=result.reason,
            )
        return result

    def _run_id_for_logging(self) -> str:
        # The HTTP transport carries its own ``run_id``; subprocess
        # transport does not. Surface the agent's view via the transport
        # when the attribute exists.
        return getattr(self.transport, "run_id", "") or ""

    @staticmethod
    def _finding_id_for_logging(payload: Mapping[str, Any]) -> str:
        finding = payload.get("finding")
        if isinstance(finding, Mapping):
            value = finding.get("finding_id")
            if value:
                return str(value)
        return ""

    def _project_for_logging(self) -> str:
        # Resolved project name lives on the transport's config under the
        # ``coder.workspace_root`` model. Subprocess transport sets ``-``
        # so the structured log line keeps a uniform shape.
        cfg = getattr(self.transport, "config", None)
        if cfg is None:
            return ""
        return getattr(cfg, "effective_project_name", "") or ""

    def _endpoint_for_logging(self) -> str:
        cfg = getattr(self.transport, "config", None)
        if cfg is None:
            return ""
        # Only the HTTP transport has a meaningful endpoint.
        if getattr(cfg, "transport", "") != "http":
            return ""
        return getattr(cfg, "endpoint", "") or ""


# --------------------------------------------------------------------------
# Dispatcher
# --------------------------------------------------------------------------


@dataclass
class _PendingTask:
    finding_id: str
    future: Future
    submitted_at: float = field(default_factory=monotonic)
    on_settled: Callable[[str, "CoderResult"], None] | None = None


class CoderDispatcher:
    """Bounded thread-pool wrapper over a ``CoderAgent``.

    The dispatcher hides the executor and exposes the four operations the
    audit workflow needs: ``submit``, ``poll_completed``, ``cancel_all``,
    and ``drain``.
    """

    def __init__(
        self,
        *,
        agent: CoderAgent,
        concurrency: int,
        logger: RuntimeLogger | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self.agent = agent
        self.concurrency = concurrency
        self.logger = logger
        self._executor = ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="xauditor-coder",
        )
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingTask] = {}
        self._closed = False
        self._cancellation: "RunCancellation | None" = None

    def set_cancellation(self, cancellation: "RunCancellation | None") -> None:
        """Inject the run-level cancellation token; called by ``AuditWorkflow``.

        After this is set, :py:meth:`drain` exits the moment the event
        is set even mid-poll-interval (the inner ``_sleep`` is replaced
        with ``cancellation.wait``).
        """
        self._cancellation = cancellation

    @classmethod
    def from_config(
        cls,
        config: CoderConfig,
        *,
        logger: RuntimeLogger | None = None,
        env: Mapping[str, str] | None = None,
        run_id: str = "",
    ) -> "CoderDispatcher":
        agent = CoderAgent.from_config(config, logger=logger, env=env, run_id=run_id)
        return cls(agent=agent, concurrency=config.concurrency, logger=logger)

    # ----------------------------------------------------------------- API

    def submit(
        self,
        finding_id: str,
        payload: Mapping[str, Any],
        *,
        on_settled: Callable[[str, "CoderResult"], None] | None = None,
    ) -> None:
        """Queue a verification for a finding. No-op if already submitted.

        When ``on_settled`` is provided, the dispatcher registers a
        ``Future.add_done_callback`` that fires the callback **from the
        worker thread** the moment the task settles. This is what makes
        per-finding streaming to the report sinks finer-grained than
        the old per-path drain pattern: portal users see ``coder_status``
        flip from ``Pending`` → terminal as each subprocess returns,
        without waiting for the next ``poll_completed`` from the audit
        main loop.

        Callbacks compete with ``poll_completed`` / ``drain`` for the
        right to handle a settled task — the ``_pending`` pop is the
        coordination point. Whoever pops first runs its handler; the
        other sees a ``None`` and exits silently. This guarantees
        exactly-once semantics regardless of which path "wins".
        """

        with self._lock:
            if self._closed:
                raise RuntimeError("CoderDispatcher is closed")
            if finding_id in self._pending:
                return
            future = self._executor.submit(self.agent.verify, dict(payload))
            self._pending[finding_id] = _PendingTask(
                finding_id=finding_id, future=future, on_settled=on_settled,
            )
        if on_settled is not None:
            future.add_done_callback(
                lambda fut, fid=finding_id: self._fire_settled(fid, fut)
            )
        if self.logger is not None:
            self.logger.debug_kv("Coder task submitted", finding_id=finding_id)

    def _fire_settled(self, finding_id: str, future: Future) -> None:
        """``add_done_callback`` handler: invoke ``on_settled`` from the worker thread.

        Pops the task atomically — if ``poll_completed`` or ``drain``
        already grabbed it, this exits without invoking the callback.
        Errors raised by the callback are logged and swallowed so a
        snapshot-emit failure cannot crash a worker.
        """

        try:
            result = future.result()
        except Exception as exc:  # noqa: BLE001 — same Inconclusive shape as poll_completed
            if self.logger is not None:
                self.logger.warning(f"Coder task crashed for {finding_id}: {exc}")
            result = CoderResult(
                status=CODER_STATUS_FAIL,
                analysis="",
                reason=f"transport error: {exc}",
                evidence=(),
                cli_exit_code=None,
                cli_stderr=None,
                duration_ms=None,
            )
        with self._lock:
            task = self._pending.pop(finding_id, None)
        if task is None or task.on_settled is None:
            return
        try:
            task.on_settled(finding_id, result)
        except Exception as exc:  # noqa: BLE001 — observer must not break worker
            if self.logger is not None:
                self.logger.warning(
                    f"Coder on_settled callback failed for {finding_id}: {exc}"
                )

    def poll_completed(self) -> list[tuple[str, CoderResult]]:
        """Return every settled task without blocking.

        The order matches submission order; settled entries are removed
        from the pending map atomically.
        """

        results: list[tuple[str, CoderResult]] = []
        with self._lock:
            settled_ids = [fid for fid, task in self._pending.items() if task.future.done()]
            for finding_id in settled_ids:
                task = self._pending.pop(finding_id)
                try:
                    result = task.future.result()
                except Exception as exc:  # noqa: BLE001 — surface as Inconclusive
                    if self.logger is not None:
                        self.logger.warning(f"Coder task crashed for {finding_id}: {exc}")
                    result = CoderResult(
                        status=CODER_STATUS_FAIL,
                        analysis="",
                        reason=f"transport error: {exc}",
                        evidence=(),
                        cli_exit_code=None,
                        cli_stderr=None,
                        duration_ms=None,
                    )
                results.append((finding_id, result))
        return results

    def pending_finding_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._pending.keys())

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._pending)

    def cancel_all(self) -> tuple[str, ...]:
        """Cancel every still-pending task; return the finding ids cancelled.

        Tasks already running are interrupted at the OS level (the worker
        thread blocks on the subprocess; cancelling the future does not
        terminate it). Callers SHOULD pair this with marking the affected
        findings as ``Skipped`` in the snapshot.
        """

        cancelled: list[str] = []
        with self._lock:
            for finding_id, task in list(self._pending.items()):
                if task.future.cancel():
                    cancelled.append(finding_id)
                    self._pending.pop(finding_id, None)
        return tuple(cancelled)

    def drain(
        self,
        *,
        timeout: float,
        on_settled: Callable[[str, CoderResult], None] | None = None,
    ) -> list[tuple[str, CoderResult]]:
        """Block until every pending task settles (or ``timeout`` expires).

        Each settled task is removed from the pending map and returned.
        When ``on_settled`` is supplied it is invoked synchronously per
        settlement so the caller can emit a snapshot per verdict.

        ``timeout`` is now required (no default) so callers cannot
        accidentally drain forever — the previous ``timeout=None``
        default was the root cause of the Ctrl+C-during-coder-stage
        hang. When the dispatcher has a run-level cancellation token
        injected via :py:meth:`set_cancellation`, the inner poll uses
        ``cancellation.wait`` instead of ``time.sleep`` so a cancel
        request is observed within ≤ 50 ms instead of having to wait
        out the current poll interval. The drain also exits the moment
        ``cancellation.is_cancelled()`` becomes true OR the run-level
        deadline elapses, whichever is sooner.
        """

        deadline: float = monotonic() + timeout
        cancellation = self._cancellation
        collected: list[tuple[str, CoderResult]] = []
        while True:
            with self._lock:
                pending_count = len(self._pending)
            if pending_count == 0:
                break
            if cancellation is not None and (
                cancellation.is_cancelled() or cancellation.deadline_reached()
            ):
                break
            new_results = self.poll_completed()
            for finding_id, result in new_results:
                collected.append((finding_id, result))
                if on_settled is not None:
                    try:
                        on_settled(finding_id, result)
                    except Exception as exc:  # noqa: BLE001 — observer must not break drain
                        log.warning("on_settled callback failed for %s: %s", finding_id, exc)
            if not new_results:
                if monotonic() >= deadline:
                    break
                # Sleep briefly before the next poll. With a cancellation
                # token, the wait short-circuits the moment cancel is
                # requested so we don't have to ride out the 50 ms.
                if cancellation is not None:
                    if cancellation.wait(0.05):
                        break
                else:
                    _sleep(0.05)
        return collected

    def shutdown(self, *, wait: bool = False) -> None:
        """Release the dispatcher's executor.

        ``wait=False`` is the normal call: the executor returns
        immediately with ``cancel_futures=True``. Worker threads blocked
        in synchronous subprocess / HTTP I/O may outlive this call —
        the OS process exit reaps them. Production callers that need a
        bounded wait SHOULD use the run-level shutdown helper which
        observes ``audit.coder.shutdown_timeout_seconds`` and falls back
        to ``wait=False`` when the cap is hit.
        """
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=True)


def _sleep(seconds: float) -> None:
    """Indirection so tests can monkeypatch the sleep call."""

    import time

    time.sleep(seconds)


def startup_log_line(coder_cfg: CoderConfig) -> str | None:
    """Return the one-line `INFO` summary of the HTTP transport's posture.

    Returns ``None`` when the line should not be emitted (subprocess
    transport, coder disabled, or no endpoint configured). The caller is
    responsible for emitting via ``logger.info(...)``. Token values are
    NEVER included; only the boolean ``enable_auth`` state.
    """

    if not coder_cfg.enabled:
        return None
    if coder_cfg.transport != "http":
        return None
    endpoint = coder_cfg.endpoint or "<unset>"
    auth_state = "enabled" if coder_cfg.enable_auth else "disabled"
    return f"Coder transport: http via {endpoint} (auth: {auth_state})"


__all__ = [
    "CODER_SYSTEM_PROMPT",
    "ClaudeCodeCliTransport",
    "CoderAgent",
    "CoderDispatcher",
    "CoderResult",
    "CoderTransport",
    "ENV_ALLOWLIST",
    "ENV_ALLOWLIST_PREFIXES",
    "HttpCoderTransport",
    "build_cli_argv",
    "parse_coder_response",
    "render_coder_payload",
    "scrub_environment",
    "startup_log_line",
]
