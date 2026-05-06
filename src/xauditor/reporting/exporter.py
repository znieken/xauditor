"""On-demand exporter — reads persisted audit-run state from Postgres.

Phase 2 of ``make-postgres-the-canonical-sink`` retired the run-time
Markdown sink. The findings.md / false-positives.md / coverage-report.md
artefacts operators expect are now produced by ``xauditor audit export
<run_label>``, which reads the persisted rows from
``xauditor_portal``'s schema and either:

- emits a versioned JSON envelope on stdout (the new portable contract
  for downstream tooling), or
- renders a Markdown bundle into an operator-supplied directory.

The JSON envelope is the canonical surface; downstream tooling SHOULD
consume that. The Markdown export is preserved so existing CI scripts
that grep for `findings.md` keep working with one extra `audit export`
step.

Markdown coverage in 0.5.0:
- findings.md (parity with 0.4.10)
- false-positives.md (parity)
- coverage-report.md (parity)
- coder-results.md (parity, when coder verification ran)

Markdown gaps deferred to a follow-up:
- analyzer-results.md / validator-results.md / exploitation-results.md
- no-findings.md
- per-stage subagent files / validator-debates/
- These derive from the shared_state JSON shape which is persisted
  in ``path_raw_outputs.raw_markdown`` but reconstruction is brittle;
  tracked as a follow-up issue.

JSON envelope shape — see ``audit-export-on-demand`` capability spec
for the authoritative grammar; in short:

    {
      "format_version": "1",
      "run":      { run-level fields },
      "findings": [ ... ],
      "coverage": [ ... ],
      "validator_debates": [ ... ],
      "debug":    { ... }   # only when --include-debug
    }
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from xauditor.models import (
    AuditRun,
    CoderEvidence,
    ConfidenceLevel,
    CoverageInventory,
    CoverageRecord,
    CoverageState,
    Finding,
    SourceReference,
    ValidationStatus,
)


if TYPE_CHECKING:
    from xauditor.config import ReportDBConfig
    from xauditor.runtime_logging import RuntimeLogger


log = logging.getLogger("xauditor.reporting.exporter")


JSON_FORMAT_VERSION = "1"


def export_audit_run(
    *,
    config: "ReportDBConfig",
    run_label: str,
    fmt: str = "json",
    output_dir: Path | None = None,
    include_debug: bool = False,
    logger: "RuntimeLogger | None" = None,
) -> str | list[Path]:
    """Render the persisted audit run identified by ``run_label``.

    For ``fmt='json'``: returns the rendered JSON document as a string.
    For ``fmt='markdown'``: writes one or more ``.md`` files into
    ``output_dir`` and returns the list of paths created.

    ``include_debug`` adds an extra ``debug`` top-level key to JSON
    output (per-finding cli_exit_code / cli_stderr).
    Apply ``logger.redact`` to all rendered text so secrets are never
    leaked into the export.
    """

    if fmt not in ("json", "markdown"):
        raise ValueError(f"Unknown export format: {fmt!r}")
    if fmt == "markdown" and output_dir is None:
        raise ValueError("--output-dir is required when --format markdown")

    envelope, audit_run = _load_envelope_and_audit_run(
        config=config, run_label=run_label, include_debug=include_debug
    )

    redact = logger.redact if logger is not None else (lambda s: s)

    if fmt == "json":
        text = json.dumps(envelope, indent=2, default=_json_default, ensure_ascii=False)
        return redact(text)

    assert output_dir is not None
    return _render_markdown_bundle(
        envelope=envelope,
        audit_run=audit_run,
        output_dir=output_dir,
        redact=redact,
    )


# --------------------------------------------------------------------------
# Envelope construction
# --------------------------------------------------------------------------


def _load_envelope_and_audit_run(
    *,
    config: "ReportDBConfig",
    run_label: str,
    include_debug: bool,
) -> tuple[dict[str, Any], AuditRun]:
    """Single DB round trip: build the JSON envelope AND the AuditRun model.

    Both are needed: JSON output emits the envelope dict; Markdown
    output calls the existing ``markdown.py`` renderer, which takes
    typed ``AuditRun`` / ``Finding`` instances. We build both up
    inside one Session so we don't double-query the schema.
    """

    from xauditor_portal.db.models.report import (
        AnalyzerSubagentRecord as AnalyzerSubagentRow,
        AuditRun as AuditRunRow,
        CoderFinding as CoderFindingRow,
        CoderFindingEvidence as CoderEvidenceRow,
        CoverageFile as CoverageFileRow,
        CoverageFunction as CoverageFunctionRow,
        CoverageModule as CoverageModuleRow,
        ExploiterSubagentRecord as ExploiterSubagentRow,
        Finding as FindingRow,
        FindingSourceReference as SourceReferenceRow,
        ReferencedSymbol as ReferencedSymbolRow,
        ValidatorDebate as ValidatorDebateRow,
        ValidatorSubagentRecord as ValidatorSubagentRow,
    )
    from xauditor_portal.sinks.postgres_sink import _sync_url_for
    from sqlalchemy import create_engine

    engine = create_engine(_sync_url_for(config), pool_pre_ping=True)
    Session_ = sessionmaker(bind=engine, expire_on_commit=False)
    session: Session = Session_()
    try:
        run_row = session.execute(
            select(AuditRunRow).where(AuditRunRow.report_dir == run_label).limit(1)
        ).scalar_one_or_none()
        if run_row is None:
            raise LookupError(
                f"Run label {run_label!r} not found in the report database."
            )

        finding_rows = list(
            session.execute(
                select(FindingRow).where(FindingRow.run_id == run_row.id)
            ).scalars()
        )
        finding_id_to_pk: dict[str, Any] = {f.finding_id: f.id for f in finding_rows}

        # Children: source_references + referenced_symbols + coder_findings (+ evidence)
        ref_rows = list(
            session.execute(
                select(SourceReferenceRow).where(
                    SourceReferenceRow.finding_id.in_([f.id for f in finding_rows])
                    if finding_rows
                    else SourceReferenceRow.finding_id.is_(None)
                )
            ).scalars()
        )
        sym_rows = list(
            session.execute(
                select(ReferencedSymbolRow).where(
                    ReferencedSymbolRow.finding_id.in_([f.id for f in finding_rows])
                    if finding_rows
                    else ReferencedSymbolRow.finding_id.is_(None)
                )
            ).scalars()
        )
        coder_rows = list(
            session.execute(
                select(CoderFindingRow).where(CoderFindingRow.run_id == run_row.id)
            ).scalars()
        )
        coder_evidence_rows = list(
            session.execute(
                select(CoderEvidenceRow).where(
                    CoderEvidenceRow.coder_finding_id.in_(
                        [c.id for c in coder_rows]
                    )
                    if coder_rows
                    else CoderEvidenceRow.coder_finding_id.is_(None)
                )
            ).scalars()
        )
        coverage_module_rows = list(
            session.execute(
                select(CoverageModuleRow).where(CoverageModuleRow.run_id == run_row.id)
            ).scalars()
        )
        coverage_file_rows = list(
            session.execute(
                select(CoverageFileRow).where(CoverageFileRow.run_id == run_row.id)
            ).scalars()
        )
        coverage_function_rows = list(
            session.execute(
                select(CoverageFunctionRow).where(
                    CoverageFunctionRow.run_id == run_row.id
                )
            ).scalars()
        )
        debate_rows = list(
            session.execute(
                select(ValidatorDebateRow).where(ValidatorDebateRow.run_id == run_row.id)
            ).scalars()
        )

        envelope = _build_envelope(
            run_row=run_row,
            finding_rows=finding_rows,
            ref_rows=ref_rows,
            sym_rows=sym_rows,
            coder_rows=coder_rows,
            coder_evidence_rows=coder_evidence_rows,
            coverage_module_rows=coverage_module_rows,
            coverage_file_rows=coverage_file_rows,
            coverage_function_rows=coverage_function_rows,
            debate_rows=debate_rows,
            include_debug=include_debug,
            finding_id_to_pk=finding_id_to_pk,
        )
        audit_run = _reconstruct_audit_run(
            run_row=run_row,
            finding_rows=finding_rows,
            ref_rows=ref_rows,
            sym_rows=sym_rows,
            coder_rows=coder_rows,
            coder_evidence_rows=coder_evidence_rows,
            coverage_module_rows=coverage_module_rows,
            coverage_file_rows=coverage_file_rows,
            coverage_function_rows=coverage_function_rows,
        )
        return envelope, audit_run
    finally:
        session.close()
        engine.dispose()


def _build_envelope(
    *,
    run_row,
    finding_rows,
    ref_rows,
    sym_rows,
    coder_rows,
    coder_evidence_rows,
    coverage_module_rows,
    coverage_file_rows,
    coverage_function_rows,
    debate_rows,
    include_debug: bool,
    finding_id_to_pk: dict[str, Any],
) -> dict[str, Any]:
    refs_by_finding: dict[Any, list[Any]] = {}
    for ref in ref_rows:
        refs_by_finding.setdefault(ref.finding_id, []).append(ref)
    syms_by_finding: dict[Any, list[Any]] = {}
    for sym in sym_rows:
        syms_by_finding.setdefault(sym.finding_id, []).append(sym)
    coder_by_ref: dict[str, Any] = {c.finding_ref: c for c in coder_rows}
    coder_evidence_by_pk: dict[Any, list[Any]] = {}
    for ev in coder_evidence_rows:
        coder_evidence_by_pk.setdefault(ev.coder_finding_id, []).append(ev)

    findings_payload: list[dict[str, Any]] = []
    debug_per_finding: dict[str, dict[str, Any]] = {}
    for f in finding_rows:
        refs = refs_by_finding.get(f.id, [])
        refs.sort(key=lambda r: r.ordinal)
        coder = coder_by_ref.get(f.finding_id)
        coder_payload: dict[str, Any] | None = None
        if coder is not None:
            evidence = coder_evidence_by_pk.get(coder.id, [])
            evidence.sort(key=lambda e: e.ordinal)
            coder_payload = {
                "status": coder.status,
                "analysis": coder.analysis,
                "reason": coder.reason,
                "evidence": [
                    {
                        "file_path": e.file_path,
                        "function_name": e.function_name,
                        "snippet": e.snippet,
                        "language": e.language,
                        "role": e.role,
                    }
                    for e in evidence
                ],
            }
            if include_debug:
                debug_per_finding[f.finding_id] = {
                    "cli_exit_code": coder.cli_exit_code,
                    "cli_stderr": coder.cli_stderr,
                    "duration_ms": coder.duration_ms,
                    "dispatched_at": _iso(coder.dispatched_at),
                    "completed_at": _iso(coder.completed_at),
                }
        findings_payload.append(
            {
                "finding_id": f.finding_id,
                "finding_name": f.finding_name,
                "finding_description": f.finding_description,
                "confidence_level": f.confidence_level,
                "validation_status": f.validation_status,
                "analysis": f.analysis,
                "reason": f.reason,
                "context": f.context,
                "business_context": f.business_context,
                "exploitation_status": f.exploitation_status,
                "exploitation_steps": f.exploitation_steps,
                "validation_analysis": f.validation_analysis,
                "path_fingerprint": f.path_fingerprint,
                "suspect_function_id": f.suspect_function_id,
                "suspect_line": f.suspect_line,
                "source_references": [
                    {
                        "file_path": r.file_path,
                        "snippet": r.snippet,
                        "language": r.language,
                        "ordinal": r.ordinal,
                    }
                    for r in refs
                ],
                "referenced_symbols": [
                    {
                        "name": s.name,
                        "kind": s.kind,
                        "module_name": s.module_name,
                        "file_path": s.file_path,
                        "start_line": s.start_line,
                        "end_line": s.end_line,
                        "type_annotation": s.type_annotation,
                        "value_repr": s.value_repr,
                        "is_placeholder": s.is_placeholder,
                        "used_by": s.used_by or [],
                    }
                    for s in syms_by_finding.get(f.id, [])
                ],
                "coder": coder_payload,
            }
        )

    coverage_payload = {
        "modules": [
            {"identifier": m.module_name, "status": m.status, "reason": m.reason}
            for m in coverage_module_rows
        ],
        "files": [
            {"identifier": cf.file_path, "status": cf.status, "reason": cf.reason}
            for cf in coverage_file_rows
        ],
        "functions": [
            {
                "function_id": fn.function_id,
                "qualified_name": fn.qualified_name,
                "file_path": fn.file_path,
                "status": fn.status,
                "reason": fn.reason,
            }
            for fn in coverage_function_rows
        ],
    }

    debates_payload = [
        {
            "finding_ref": d.finding_ref,
            "path_fingerprint": d.path_fingerprint,
            "rounds": d.rounds or [],
            "final_verdict": d.final_verdict,
            "convergence_state": d.convergence_state,
            "configured_round_cap": d.configured_round_cap,
        }
        for d in debate_rows
    ]

    envelope: dict[str, Any] = {
        "format_version": JSON_FORMAT_VERSION,
        "run": {
            "run_id": str(run_row.id),
            "run_label": run_row.report_dir,
            "build_fingerprint": run_row.build_fingerprint,
            "started_at": _iso(run_row.started_at),
            "completed_at": _iso(run_row.completed_at),
            "status": run_row.status,
            "mode": run_row.mode,
            "llm_providers_used": dict(run_row.llm_providers_used or {}),
            "totals": {
                "candidates": int(run_row.total_candidates or 0),
                "valid": int(run_row.valid_findings or 0),
                "false_positives": int(run_row.false_positives or 0),
                "unlabeled": int(run_row.unlabeled_findings or 0),
            },
        },
        # Phase 5A: top-level `coverage_gaps` field (Markdown is
        # rendered by `markdown.py` from the same JSON shape).
        # `None` for runs created before alembic 0011 added the
        # column AND for runs whose `audit.coverage_gaps.report`
        # was false. The portal renders an "n/a — pre-rename
        # audit" placeholder for null values.
        "coverage_gaps": getattr(run_row, "coverage_gaps", None),
        "findings": findings_payload,
        "coverage": coverage_payload,
        "validator_debates": debates_payload,
    }
    if include_debug:
        envelope["debug"] = {"findings": debug_per_finding}
    return envelope


# --------------------------------------------------------------------------
# AuditRun reconstruction (for Markdown rendering)
# --------------------------------------------------------------------------


_VALIDATION_STATUS_BY_VALUE: dict[str, ValidationStatus] = {
    member.value: member for member in ValidationStatus
}
_CONFIDENCE_BY_VALUE: dict[str, ConfidenceLevel] = {
    member.value: member for member in ConfidenceLevel
}
_COVERAGE_STATE_BY_DB_VALUE: dict[str, CoverageState] = {
    "audited": CoverageState.AUDITED,
    "unaudited": CoverageState.NOT_AUDITED,
    "excluded": CoverageState.EXCLUDED,
    "skipped": CoverageState.INTERRUPTED,
    "failed": CoverageState.FAILED,
}


def _reconstruct_audit_run(
    *,
    run_row,
    finding_rows,
    ref_rows,
    sym_rows,
    coder_rows,
    coder_evidence_rows,
    coverage_module_rows,
    coverage_file_rows,
    coverage_function_rows,
) -> AuditRun:
    refs_by_finding: dict[Any, list[Any]] = {}
    for ref in ref_rows:
        refs_by_finding.setdefault(ref.finding_id, []).append(ref)
    syms_by_finding: dict[Any, list[Any]] = {}
    for sym in sym_rows:
        syms_by_finding.setdefault(sym.finding_id, []).append(sym)
    coder_by_ref: dict[str, Any] = {c.finding_ref: c for c in coder_rows}
    coder_evidence_by_pk: dict[Any, list[Any]] = {}
    for ev in coder_evidence_rows:
        coder_evidence_by_pk.setdefault(ev.coder_finding_id, []).append(ev)

    findings: list[Finding] = []
    for f in finding_rows:
        refs = sorted(refs_by_finding.get(f.id, []), key=lambda r: r.ordinal)
        coder = coder_by_ref.get(f.finding_id)
        coder_status = coder.status if coder is not None else "Skipped"
        coder_analysis = coder.analysis if coder is not None else ""
        coder_reason = coder.reason if coder is not None else ""
        coder_evidence: tuple[CoderEvidence, ...] = ()
        if coder is not None:
            evidence_rows = sorted(
                coder_evidence_by_pk.get(coder.id, []), key=lambda e: e.ordinal
            )
            coder_evidence = tuple(
                CoderEvidence(
                    file_path=e.file_path,
                    function_name=e.function_name,
                    snippet=e.snippet,
                    language=e.language,
                    role=e.role,
                )
                for e in evidence_rows
            )
        findings.append(
            Finding(
                finding_id=f.finding_id,
                finding_name=f.finding_name,
                finding_description=f.finding_description,
                confidence_level=_CONFIDENCE_BY_VALUE.get(
                    f.confidence_level, ConfidenceLevel.MEDIUM
                ),
                source_references=tuple(
                    SourceReference(
                        file_path=r.file_path,
                        start_line=0,
                        end_line=0,
                        focus_lines=(),
                        language=r.language or "",
                        snippet=r.snippet,
                    )
                    for r in refs
                ),
                analysis=f.analysis,
                reason=f.reason,
                context=f.context,
                business_context=f.business_context,
                exploitation_status=f.exploitation_status,
                exploitation_steps=f.exploitation_steps,
                validation_status=_VALIDATION_STATUS_BY_VALUE.get(
                    f.validation_status, ValidationStatus.INCONCLUSIVE
                ),
                validation_analysis=f.validation_analysis,
                path_fingerprint=f.path_fingerprint or "",
                analyzer_status=f.analyzer_status or "",
                evidence_strength=f.evidence_strength or "",
                suspect_function_id=f.suspect_function_id or "",
                suspect_line=int(f.suspect_line or 0),
                context_notes=f.context_notes or "",
                referenced_symbols=tuple(
                    {
                        "name": s.name,
                        "kind": s.kind,
                        "module_name": s.module_name,
                        "file_path": s.file_path,
                        "start_line": s.start_line,
                        "end_line": s.end_line,
                        "type_annotation": s.type_annotation,
                        "value_repr": s.value_repr,
                        "is_placeholder": s.is_placeholder,
                        "used_by": list(s.used_by or []),
                    }
                    for s in syms_by_finding.get(f.id, [])
                ),
                coder_status=coder_status,
                coder_analysis=coder_analysis,
                coder_reason=coder_reason,
                coder_call_chain_evidence=coder_evidence,
            )
        )

    coverage = CoverageInventory()
    for m in coverage_module_rows:
        coverage.add(
            CoverageRecord(
                category="module",
                identifier=m.module_name,
                state=_COVERAGE_STATE_BY_DB_VALUE.get(m.status, CoverageState.NOT_AUDITED),
            )
        )
    for cf in coverage_file_rows:
        coverage.add(
            CoverageRecord(
                category="file",
                identifier=cf.file_path,
                state=_COVERAGE_STATE_BY_DB_VALUE.get(cf.status, CoverageState.NOT_AUDITED),
            )
        )
    for fn in coverage_function_rows:
        coverage.add(
            CoverageRecord(
                category="function",
                identifier=fn.qualified_name or fn.function_id,
                state=_COVERAGE_STATE_BY_DB_VALUE.get(fn.status, CoverageState.NOT_AUDITED),
            )
        )

    return AuditRun(
        build_fingerprint=run_row.build_fingerprint or "",
        findings=tuple(findings),
        coverage=coverage,
    )


# --------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------


def _render_markdown_bundle(
    *,
    envelope: dict[str, Any],
    audit_run: AuditRun,
    output_dir: Path,
    redact,
) -> list[Path]:
    """Render the four findings/coverage-derived files into ``output_dir``.

    See module docstring for the list of files NOT covered by 0.5.0
    Markdown export (analyzer / validator / exploitation stage reports
    and per-stage subagent / debate transcripts).
    """

    from xauditor.reporting.markdown import (
        render_coder_results_report,
        render_coverage_report,
        render_false_positives_report,
        render_findings_report,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    coder_enabled = bool(envelope["run"]["llm_providers_used"].get("coder"))

    files: list[Path] = []
    files.append(
        _write_md(
            output_dir / "findings.md",
            render_findings_report(audit_run.findings, coder_enabled=coder_enabled),
            redact=redact,
        )
    )
    files.append(
        _write_md(
            output_dir / "false-positives.md",
            render_false_positives_report(
                audit_run.findings, coder_enabled=coder_enabled
            ),
            redact=redact,
        )
    )
    files.append(
        _write_md(
            output_dir / "coverage-report.md",
            render_coverage_report(audit_run.coverage),
            redact=redact,
        )
    )
    if coder_enabled or any(
        f.coder_status not in ("Skipped", "") for f in audit_run.findings
    ):
        files.append(
            _write_md(
                output_dir / "coder-results.md",
                render_coder_results_report(
                    audit_run.findings, coder_enabled=coder_enabled
                ),
                redact=redact,
            )
        )
    # Phase 5A: emit `coverage-gaps.md` whenever the envelope
    # carries a non-null `coverage_gaps` payload. NULL means the
    # operator turned off `audit.coverage_gaps.report` OR the run
    # predates the column.
    coverage_gaps_payload = envelope.get("coverage_gaps")
    if coverage_gaps_payload:
        from xauditor.coverage_gaps import CoverageGaps

        gaps = CoverageGaps(
            audited_classes=tuple(coverage_gaps_payload.get("audited_classes", ())),
            skipped_by_mode=tuple(coverage_gaps_payload.get("skipped_by_mode", ())),
            out_of_scope=tuple(coverage_gaps_payload.get("out_of_scope", ())),
            mode=str(coverage_gaps_payload.get("mode", "")),
            advice_to_user=str(coverage_gaps_payload.get("advice_to_user", "")),
        )
        files.append(
            _write_md(
                output_dir / "coverage-gaps.md",
                gaps.to_markdown_section(),
                redact=redact,
            )
        )
    return files


def _write_md(path: Path, content: str, *, redact) -> Path:
    path.write_text(redact(content), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _iso(value) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


__all__ = ["JSON_FORMAT_VERSION", "export_audit_run"]
