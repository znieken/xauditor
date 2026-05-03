"""Vendored helpers from ``xauditor.audit.coder`` and ``xauditor.models``.

This module exists so the coder microservice has zero hard dependency on
``xauditor``. The xauditor wheel is not on PyPI; depending on it forced
the docker build to copy a wheel file into the build context, which in
turn forced operators to pre-stage wheels in a magic directory before
``xauditor coder build`` could run.

After vendoring, the only thing the container needs is
``xauditor-coder-service`` itself — installable with a single ``pip
install``. The Dockerfile then mirrors the ``xauditor-portal`` pattern:
copy the package source tree in, install service deps from a shipped
``requirements.txt``.

The functions below are byte-equivalent (or behaviorally equivalent) to
their xauditor counterparts. ``tests/test_coder_service_vendor_drift.py``
in the xauditor repo asserts this every commit so the two copies cannot
silently drift. Source of truth:

  - ``xauditor.audit.coder.ENV_ALLOWLIST``
  - ``xauditor.audit.coder.ENV_ALLOWLIST_PREFIXES``
  - ``xauditor.audit.coder.scrub_environment``
  - ``xauditor.audit.coder.build_cli_argv``
  - ``xauditor.audit.coder.parse_coder_response``  (without the optional
    ``logger`` argument — the service does not have a RuntimeLogger)
  - ``xauditor.models.CODER_STATUS_*``
  - ``xauditor.models.normalize_coder_status``
  - ``xauditor.models.CoderEvidence``
  - ``xauditor.audit.coder.CoderResult``
  - ``xauditor.integrations.coder.projects_walk.walk_projects``

If you change behaviour in xauditor, update both sides AND the drift
test in the same PR.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence, TypedDict


# --------------------------------------------------------------------------
# Status constants and synonyms (mirror xauditor.models)
# --------------------------------------------------------------------------

CODER_STATUS_VERIFIED = "Verified"
CODER_STATUS_NOT_VERIFIED = "Not Verified"
CODER_STATUS_INCONCLUSIVE = "Inconclusive"
CODER_STATUS_SKIPPED = "Skipped"
CODER_STATUS_PENDING = "Pending"

CODER_STATUSES: tuple[str, ...] = (
    CODER_STATUS_VERIFIED,
    CODER_STATUS_NOT_VERIFIED,
    CODER_STATUS_INCONCLUSIVE,
    CODER_STATUS_SKIPPED,
    CODER_STATUS_PENDING,
)

_CODER_STATUS_SYNONYMS: dict[str, str] = {
    "verified": CODER_STATUS_VERIFIED,
    "confirmed": CODER_STATUS_VERIFIED,
    "valid": CODER_STATUS_VERIFIED,
    "true_positive": CODER_STATUS_VERIFIED,
    "true positive": CODER_STATUS_VERIFIED,
    "not_verified": CODER_STATUS_NOT_VERIFIED,
    "not verified": CODER_STATUS_NOT_VERIFIED,
    "false_positive": CODER_STATUS_NOT_VERIFIED,
    "false positive": CODER_STATUS_NOT_VERIFIED,
    "rejected": CODER_STATUS_NOT_VERIFIED,
    "invalid": CODER_STATUS_NOT_VERIFIED,
    "inconclusive": CODER_STATUS_INCONCLUSIVE,
    "uncertain": CODER_STATUS_INCONCLUSIVE,
    "unknown": CODER_STATUS_INCONCLUSIVE,
    "indeterminate": CODER_STATUS_INCONCLUSIVE,
    "skipped": CODER_STATUS_SKIPPED,
    "pending": CODER_STATUS_PENDING,
    "in_progress": CODER_STATUS_PENDING,
    "in progress": CODER_STATUS_PENDING,
}


def normalize_coder_status(value: object) -> str:
    if value is None:
        return CODER_STATUS_INCONCLUSIVE
    text = " ".join(str(value).strip().split())
    if not text:
        return CODER_STATUS_INCONCLUSIVE
    if text in CODER_STATUSES:
        return text
    return _CODER_STATUS_SYNONYMS.get(text.lower(), CODER_STATUS_INCONCLUSIVE)


# --------------------------------------------------------------------------
# Result types (mirror xauditor.models.CoderEvidence and xauditor.audit.coder.CoderResult)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CoderEvidence:
    file_path: str
    function_name: str | None
    snippet: str
    language: str | None
    role: str


@dataclass(frozen=True)
class CoderResult:
    status: str
    analysis: str = ""
    reason: str = ""
    evidence: tuple[CoderEvidence, ...] = ()
    cli_exit_code: int | None = None
    cli_stderr: str | None = None
    duration_ms: int | None = None


# --------------------------------------------------------------------------
# Environment scrub (mirror xauditor.audit.coder.ENV_ALLOWLIST + scrub_environment)
# --------------------------------------------------------------------------

ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "TERM",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
)
ENV_ALLOWLIST_PREFIXES: tuple[str, ...] = ("CLAUDE_",)


def scrub_environment(parent_env: Mapping[str, str] | None = None) -> dict[str, str]:
    source: Mapping[str, str] = parent_env if parent_env is not None else os.environ
    scrubbed: dict[str, str] = {}
    for name, value in source.items():
        if name in ENV_ALLOWLIST or any(
            name.startswith(prefix) for prefix in ENV_ALLOWLIST_PREFIXES
        ):
            scrubbed[name] = value
    return scrubbed


# --------------------------------------------------------------------------
# CLI argv builder (mirror xauditor.audit.coder.build_cli_argv)
# --------------------------------------------------------------------------


def build_cli_argv(
    cli_command: Sequence[str],
    *,
    thinking_effort: str | None = None,
) -> list[str]:
    argv = list(cli_command)
    argv.append("-p")
    if thinking_effort:
        argv.extend(["--effort", thinking_effort])
    return argv


# --------------------------------------------------------------------------
# Response parser (mirror xauditor.audit.coder.parse_coder_response without logger)
# --------------------------------------------------------------------------

_STDERR_TRUNCATE_BYTES = 32 * 1024
_ANALYSIS_TRUNCATE_CHARS = 16_000
_REASON_TRUNCATE_CHARS = 2_000


def _truncate_str(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(" ... [truncated]"))] + " ... [truncated]"


def _extract_json_object(stdout: str) -> str | None:
    """Lenient JSON extractor. See xauditor.audit.coder._extract_json_object."""

    text = stdout.strip()
    if text.startswith("```"):
        nl = text.find("\n")
        text = text[nl + 1:] if nl >= 0 else text[3:]
        text = text.rstrip()
        if text.endswith("```"):
            text = text[:-3].rstrip()
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
            str(function_name_raw).strip()
            if function_name_raw not in (None, "")
            else None
        )
        snippet = str(raw.get("snippet", ""))
        language_raw = raw.get("language")
        language = (
            str(language_raw).strip() if language_raw not in (None, "") else None
        )
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
) -> CoderResult:
    """Parse the Claude Code subprocess output into a ``CoderResult``."""

    redacted_stderr = _truncate_str(stderr, _STDERR_TRUNCATE_BYTES) or None
    if exit_code != 0:
        analysis = _truncate_str(stdout, _ANALYSIS_TRUNCATE_CHARS)
        return CoderResult(
            status=CODER_STATUS_INCONCLUSIVE,
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
            status=CODER_STATUS_INCONCLUSIVE,
            analysis="",
            reason="transport error: empty stdout",
            evidence=(),
            cli_exit_code=exit_code,
            cli_stderr=redacted_stderr,
            duration_ms=duration_ms,
        )

    extracted = _extract_json_object(stripped)
    if extracted is None:
        analysis = _truncate_str(stdout, _ANALYSIS_TRUNCATE_CHARS)
        return CoderResult(
            status=CODER_STATUS_INCONCLUSIVE,
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
        analysis = _truncate_str(stdout, _ANALYSIS_TRUNCATE_CHARS)
        return CoderResult(
            status=CODER_STATUS_INCONCLUSIVE,
            analysis=analysis,
            reason="transport error: malformed JSON",
            evidence=(),
            cli_exit_code=exit_code,
            cli_stderr=redacted_stderr,
            duration_ms=duration_ms,
        )

    if not isinstance(parsed, Mapping):
        analysis = _truncate_str(stdout, _ANALYSIS_TRUNCATE_CHARS)
        return CoderResult(
            status=CODER_STATUS_INCONCLUSIVE,
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
    analysis = _truncate_str(raw_analysis, _ANALYSIS_TRUNCATE_CHARS)
    reason = _truncate_str(raw_reason, _REASON_TRUNCATE_CHARS)
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
# Project workspace walk (mirror xauditor.integrations.coder.projects_walk)
# --------------------------------------------------------------------------

_DEFAULT_ENTRY_BUDGET = 10_000
_DEFAULT_TIME_BUDGET_SECONDS = 3.0


class WalkInfo(TypedDict):
    truncated: bool
    entries_visited: int
    elapsed_seconds: float


def _name_is_filtered(name: str) -> bool:
    return not name or name.startswith(".") or name.startswith("__")


def walk_projects(
    root: str,
    *,
    entry_budget: int = _DEFAULT_ENTRY_BUDGET,
    time_budget_seconds: float = _DEFAULT_TIME_BUDGET_SECONDS,
) -> tuple[list[str], WalkInfo]:
    import time as _time

    started = _time.monotonic()
    info: WalkInfo = {
        "truncated": False,
        "entries_visited": 0,
        "elapsed_seconds": 0.0,
    }
    try:
        root_real = os.path.realpath(root)
    except OSError:
        info["elapsed_seconds"] = _time.monotonic() - started
        return [], info
    if not os.path.isdir(root_real):
        info["elapsed_seconds"] = _time.monotonic() - started
        return [], info

    deadline = started + max(0.0, float(time_budget_seconds))
    visited_realpaths: set[str] = {root_real}
    found: list[str] = []

    # Phase 1: enumerate every top-level directory (always completes).
    try:
        with os.scandir(root_real) as iterator:
            top_entries = list(iterator)
    except OSError:
        info["elapsed_seconds"] = _time.monotonic() - started
        return [], info

    descend_targets: list[tuple[str, str]] = []
    for entry in top_entries:
        if info["entries_visited"] >= entry_budget:
            info["truncated"] = True
            break
        info["entries_visited"] += 1
        if _name_is_filtered(entry.name):
            continue
        try:
            if not entry.is_dir(follow_symlinks=True):
                continue
        except OSError:
            continue
        try:
            real = os.path.realpath(entry.path)
        except OSError:
            continue
        if real in visited_realpaths:
            continue
        visited_realpaths.add(real)
        found.append(entry.name)
        descend_targets.append((entry.path, entry.name))

    # Phase 2: recurse into each top-level subtree (best-effort).
    def _descend(current_path: str, prefix: str) -> bool:
        if info["entries_visited"] >= entry_budget:
            info["truncated"] = True
            return False
        if _time.monotonic() >= deadline:
            info["truncated"] = True
            return False
        try:
            iterator = os.scandir(current_path)
        except OSError:
            return True
        with iterator as entries:
            for entry in entries:
                if info["entries_visited"] >= entry_budget:
                    info["truncated"] = True
                    return False
                if _time.monotonic() >= deadline:
                    info["truncated"] = True
                    return False
                info["entries_visited"] += 1
                if _name_is_filtered(entry.name):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                try:
                    real = os.path.realpath(entry.path)
                except OSError:
                    continue
                if real in visited_realpaths:
                    continue
                visited_realpaths.add(real)
                rel = f"{prefix}/{entry.name}"
                found.append(rel)
                if not _descend(entry.path, rel):
                    return False
        return True

    for descend_path, descend_prefix in descend_targets:
        if not _descend(descend_path, descend_prefix):
            break

    found.sort()
    info["elapsed_seconds"] = _time.monotonic() - started
    return found, info


__all__ = [
    "CODER_STATUS_INCONCLUSIVE",
    "CODER_STATUS_NOT_VERIFIED",
    "CODER_STATUS_PENDING",
    "CODER_STATUS_SKIPPED",
    "CODER_STATUS_VERIFIED",
    "CODER_STATUSES",
    "CoderEvidence",
    "CoderResult",
    "ENV_ALLOWLIST",
    "ENV_ALLOWLIST_PREFIXES",
    "WalkInfo",
    "build_cli_argv",
    "normalize_coder_status",
    "parse_coder_response",
    "scrub_environment",
    "walk_projects",
]
