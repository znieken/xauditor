"""Cross-unit reconciler stage (Phase 5A — passthrough today).

`restructure-audit-modes-and-coverage` Phase 5A introduces the
reconciler — the deep-mode-only stage that consolidates findings
surfaced from MULTIPLE audit-unit kinds (e.g. the same SQL-injection
finding emerging from `PathAuditUnit` + `SinkAuditUnit` +
`EntryAuditUnit`) into one consolidated verdict.

Phase 5A scope:

- The reconciler module + Protocol surface ship today, ready to
  consume multi-unit findings as soon as the planner emits more
  than `Path` units.
- A `ReconciledFinding` record carries `per_unit_verdicts` plus
  the `consolidated_verdict` and `consolidation_reasoning`.
- The grouping logic (by `Finding.fingerprint()`) is real and
  deterministic.
- The actual cross-unit consolidation prompt is reserved as
  `reconciler` v1 in `prompts.py`. Phase 5A's
  `PassthroughReconciler` does NOT call any LLM — it just
  groups by fingerprint and returns each group's first finding
  unchanged. Today's planner only emits `Path` units, so every
  group has size 1 and reconciliation is a no-op anyway. The
  real LLM-driven `AgenticReconciler` ships alongside the
  `agentic-stage-runner-real` follow-up because it shares the
  same SDK choice.

The persisted shape lands in the new `findings.reconciliation`
JSONB column (alembic migration 0012) so the portal /
exporter can render the per-unit verdicts panel verbatim
without round-trip JSON parsing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol

from xauditor.models import Finding


@dataclass(frozen=True)
class PerUnitVerdict:
    """One unit's verdict on a finding identified by fingerprint.

    Carries the verdict literal (`"Valid" | "Partial Valid" |
    "Inconclusive" | "False Positive" | "Refuted"`) and a short
    rationale. `Refuted` is reserved for unit kinds that
    actively contradict another unit's claim — e.g. an
    `EntryAuditUnit` saying "this admin route requires hard auth
    so even though the path unit found SQLi, exploitation is
    blocked at the entry".
    """

    unit_kind: str
    unit_id: str
    verdict: str
    analysis: str = ""


@dataclass(frozen=True)
class ReconciledFinding:
    """One consolidated finding aggregated from N per-unit verdicts.

    The `finding` field is the canonical record (typically the
    Path unit's, since path-shaped findings carry the richest
    framing). `per_unit_verdicts` holds the full N entries the
    portal renders side-by-side. `consolidated_verdict` is the
    final verdict the run reports.

    `consolidation_reasoning` is the reconciler agent's prose;
    in Phase 5A's passthrough mode it's an empty string.
    """

    finding: Finding
    per_unit_verdicts: tuple[PerUnitVerdict, ...]
    consolidated_verdict: str
    consolidation_reasoning: str = ""

    def to_payload(self) -> dict[str, object]:
        """Project to the JSONB shape persisted in
        `findings.reconciliation`."""

        return {
            "per_unit_verdicts": [
                {
                    "unit_kind": v.unit_kind,
                    "unit_id": v.unit_id,
                    "verdict": v.verdict,
                    "analysis": v.analysis,
                }
                for v in self.per_unit_verdicts
            ],
            "consolidated_verdict": self.consolidated_verdict,
            "consolidation_reasoning": self.consolidation_reasoning,
        }


class Reconciler(Protocol):
    """Reconciler contract.

    Implementations: `PassthroughReconciler` (Phase 5A;
    no-op for single-unit groups), and a future
    `AgenticReconciler` (LLM-driven cross-unit consolidation).
    """

    def reconcile(
        self, findings: Iterable[Finding]
    ) -> tuple[ReconciledFinding, ...]:
        ...


# ---------------------------------------------------------------------------
# Passthrough reconciler — Phase 5A
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PassthroughReconciler:
    """Group findings by `Finding.fingerprint()` and emit each
    group's first finding unchanged.

    For Path-only audits (today's planner output) every group has
    size 1, so this reconciler is structurally a no-op. The
    grouping shape is real — when SinkAuditUnit / EntryAuditUnit
    /etc. enumeration lands and the same SQL-injection finding
    surfaces from multiple unit kinds, the same code path
    consolidates them with the same fingerprint key.

    `PerUnitVerdict.verdict` is sourced from the underlying
    finding's `validation_status` (a `ValidationStatus` enum
    today). The `unit_kind` is currently unknown at this layer
    so it defaults to `"path"`; a future change wires the
    `Finding`'s originating unit kind through.
    """

    def reconcile(
        self, findings: Iterable[Finding]
    ) -> tuple[ReconciledFinding, ...]:
        groups: dict[str, list[Finding]] = {}
        order: list[str] = []
        for finding in findings:
            key = self._fingerprint(finding)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(finding)

        reconciled: list[ReconciledFinding] = []
        for key in order:
            members = groups[key]
            primary = members[0]
            verdicts = tuple(
                PerUnitVerdict(
                    unit_kind="path",
                    unit_id=key,
                    verdict=f.validation_status.value,
                    analysis=f.validation_analysis or "",
                )
                for f in members
            )
            reconciled.append(
                ReconciledFinding(
                    finding=primary,
                    per_unit_verdicts=verdicts,
                    consolidated_verdict=primary.validation_status.value,
                    consolidation_reasoning="",
                )
            )
        return tuple(reconciled)

    @staticmethod
    def _fingerprint(finding: Finding) -> str:
        """Group key for cross-unit reconciliation.

        Same key the spec calls `Finding.fingerprint()`. Today
        the field doesn't exist on `Finding`; the Phase 5A
        passthrough derives it from
        `(finding_name, suspect_function_id, suspect_line)` —
        the same exact-match key the dedup pipeline uses.
        """

        return (
            f"{(finding.finding_name or '').strip().casefold()}|"
            f"{finding.suspect_function_id or ''}|"
            f"{int(finding.suspect_line or 0)}"
        )


# ---------------------------------------------------------------------------
# Agentic reconciler — `agentic-stage-runner-real`
# ---------------------------------------------------------------------------


@dataclass
class AgenticReconciler:
    """Real LLM-driven cross-unit reconciler.

    Selected when `audit.stages.form: agentic`. For each
    multi-unit fingerprint group, invokes the
    `reconciler` v1 prompt via the same `AgentTransport`
    the stage runners use; produces a populated
    `consolidated_verdict` + `consolidation_reasoning` on
    the resulting `ReconciledFinding`.

    Single-unit groups (the common case for path-only audits)
    SHORTCUT to passthrough behaviour without invoking the
    transport — same logic as `PassthroughReconciler`. This
    avoids burning ~$0.05/finding for a no-op consolidation.

    `agentic_transcripts` accumulates the agent's tool-call
    transcript for each multi-unit reconciliation so the
    workflow can attach it to `Finding.reconciliation`'s
    `transcript` field.
    """

    transport: object  # AgentTransport — typed loosely to avoid cycle
    timeout_seconds: int = 120
    project: str | None = None
    logger: object | None = None
    agentic_transcripts: dict[str, list[dict[str, object]]] = field(
        default_factory=dict
    )

    def reconcile(
        self, findings: Iterable[Finding]
    ) -> tuple[ReconciledFinding, ...]:
        groups: dict[str, list[Finding]] = {}
        order: list[str] = []
        for finding in findings:
            key = PassthroughReconciler._fingerprint(finding)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(finding)

        reconciled: list[ReconciledFinding] = []
        for key in order:
            members = groups[key]
            if len(members) == 1:
                # Single-unit shortcut — no agent call.
                reconciled.append(self._passthrough_one(members[0]))
                continue
            reconciled.append(self._agentic_consolidate(key, members))
        return tuple(reconciled)

    def _passthrough_one(self, finding: Finding) -> ReconciledFinding:
        return ReconciledFinding(
            finding=finding,
            per_unit_verdicts=(
                PerUnitVerdict(
                    unit_kind="path",
                    unit_id=PassthroughReconciler._fingerprint(finding),
                    verdict=finding.validation_status.value,
                    analysis=finding.validation_analysis or "",
                ),
            ),
            consolidated_verdict=finding.validation_status.value,
            consolidation_reasoning="",
        )

    def _agentic_consolidate(
        self, fingerprint: str, members: list[Finding]
    ) -> ReconciledFinding:
        # Lazy import to avoid cycle with prompts.py at module load.
        from xauditor.llm_outputs import ReconcilerOutput
        from xauditor.prompts import get_prompt

        per_unit = tuple(
            PerUnitVerdict(
                unit_kind="path",  # follow-up wires real unit kinds
                unit_id=PassthroughReconciler._fingerprint(f),
                verdict=f.validation_status.value,
                analysis=f.validation_analysis or "",
            )
            for f in members
        )
        payload = {
            "finding_fingerprint": fingerprint,
            "per_unit_verdicts": [
                {
                    "unit_kind": v.unit_kind,
                    "unit_id": v.unit_id,
                    "verdict": v.verdict,
                    "analysis": v.analysis,
                }
                for v in per_unit
            ],
        }
        result = self.transport.invoke(
            system_prompt=get_prompt("reconciler"),
            user_payload=payload,
            response_model=ReconcilerOutput,
            timeout_seconds=self.timeout_seconds,
            project=self.project,
        )
        self.agentic_transcripts[fingerprint] = result.transcript_as_payload()
        verdict = _verdict_from(result.final_answer)
        reasoning = _reasoning_from(result.final_answer)
        return ReconciledFinding(
            finding=members[0],
            per_unit_verdicts=per_unit,
            consolidated_verdict=verdict,
            consolidation_reasoning=reasoning,
        )


def _verdict_from(payload: object) -> str:
    if isinstance(payload, dict):
        return str(payload.get("verdict", "Inconclusive"))
    dump = getattr(payload, "model_dump", None)
    if dump is not None:
        try:
            return str(dump().get("verdict", "Inconclusive"))
        except Exception:  # noqa: BLE001
            pass
    return str(getattr(payload, "verdict", "Inconclusive"))


def _reasoning_from(payload: object) -> str:
    if isinstance(payload, dict):
        return str(payload.get("reasoning", "") or "")
    dump = getattr(payload, "model_dump", None)
    if dump is not None:
        try:
            return str(dump().get("reasoning", "") or "")
        except Exception:  # noqa: BLE001
            pass
    return str(getattr(payload, "reasoning", "") or "")


def build_reconciler(
    *,
    audit_mode,
    transport: object | None = None,
) -> Reconciler:
    """Pick the reconciler based on `audit.stages.form`.

    `audit.stages.form: agentic` → `AgenticReconciler` wired
    to the supplied transport (typically a
    `CoderServiceAgentTransport`). Anything else → the
    `PassthroughReconciler` (parent change Phase 5A baseline).
    """

    form = getattr(audit_mode, "stages_form", "prompt")
    if form == "agentic":
        if transport is None:
            raise ValueError(
                "build_reconciler requires a transport when "
                "stages.form='agentic'."
            )
        return AgenticReconciler(
            transport=transport,
            timeout_seconds=audit_mode.agentic.timeout_seconds,
            project=audit_mode.agentic.transport.coder_service.project or None,
        )
    return PassthroughReconciler()


__all__ = [
    "Reconciler",
    "PassthroughReconciler",
    "AgenticReconciler",
    "ReconciledFinding",
    "PerUnitVerdict",
    "build_reconciler",
]
