"""PostgresReportSink — mirrors the Markdown audit artifacts into PostgreSQL.

This sink implements the `ReportSink` protocol from
``xauditor.reporting.sinks``. It is wired into the audit runtime by
``xauditor.services._attach_portal_sink_if_available`` when the portal
package is installed and the report DB is reachable.

Design choices:

- The `ReportSink` protocol is snapshot-based: every `emit_snapshot` call
  receives a full `AuditRun`. The sink rebuilds all per-run DB state from
  that snapshot so writes are idempotent and a late-arriving snapshot can
  correct earlier partial state.
- A synchronous SQLAlchemy session is used. The audit runtime itself is
  synchronous; forcing callers into an async context just to persist a
  snapshot buys us nothing here. The rest of the portal (FastAPI side)
  continues to use the async engine via ``db.base.create_async_engine_for``.
- Every write is wrapped in a savepoint-style try/except: a malformed row
  logs a warning and skips, it never aborts a full snapshot. If the
  enclosing transaction is uncommittable, the snapshot is rolled back and
  the next snapshot will retry.
- We use ``RunMeta.run_label`` (the per-invocation ``YYYYMMDD-HHMMSS``
  timestamp) as the natural key for an audit run — it is generated
  fresh per ``xauditor audit`` invocation. Keying on
  ``build_fingerprint`` alone is wrong: many audit runs can share one
  graph build, so that key collapsed distinct runs onto one DB row.
  The DB column is still named ``audit_runs.report_dir`` (its 0.4.x
  name) to avoid an unrelated rename migration.
- This sink mirrors: audit_runs, findings (with source_references and
  referenced_symbols in the same transaction), progress_events, coverage
  (modules / files / functions), validator_debates, no_finding_paths,
  subagent records for all three stages, and path_raw_outputs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from xauditor_portal.db.base import build_engine_url
from xauditor_portal.db.models.feedback import (
    LABEL_DUPLICATE,
    FindingAnnotation,
)
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
    NoFindingPath as NoFindingPathRow,
    PathRawOutput as PathRawOutputRow,
    ProgressEvent as ProgressEventRow,
    ReferencedSymbol as ReferencedSymbolRow,
    ValidatorDebate as ValidatorDebateRow,
    ValidatorSubagentRecord as ValidatorSubagentRow,
)

if TYPE_CHECKING:
    from xauditor.config import ReportDBConfig
    from xauditor.models import AuditRun
    from xauditor.reporting.sinks import ProgressEvent, RunMeta


log = logging.getLogger("xauditor_portal.sinks.postgres")


class RunDeletedExternallyError(Exception):
    """Raised when a sink write discovers that the run row no longer exists.

    The sink stamps ``self._run_id`` on a successful ``open_run``. After
    that, every write-time lookup of ``report.audit_runs`` for that id is
    expected to find the row. When it does not, an external actor (typically
    a portal admin via ``DELETE /api/runs/{id}``) has removed the run row
    out from under the still-running audit worker. The sink raises this
    exception so the audit workflow can abort cleanly with a clear message
    instead of silently degrading into orphan rows or foreign-key errors.

    The exception carries the missing ``run_id`` so the workflow / CLI
    handler can surface it in the user-visible error message.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(
            f"Audit aborted: run {run_id} was deleted from the portal "
            "while the audit was still running. Any in-flight LLM "
            "responses are lost. The orphaned LLM cost is a known "
            "consequence of admin-override deletion."
        )


_COVERAGE_STATE_TO_STATUS = {
    "audited": "audited",
    "not_audited": "unaudited",
    "excluded": "excluded",
    "interrupted": "skipped",
    "failed": "failed",
}


def _sync_url_for(config: "ReportDBConfig") -> str:
    """Return a synchronous psycopg-v3-compatible URL."""

    url = build_engine_url(config)
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + url[len("postgresql+asyncpg://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    return url


class PostgresReportSink:
    def __init__(self, config: "ReportDBConfig") -> None:
        self.config = config
        self._engine = create_engine(_sync_url_for(config), pool_pre_ping=True)
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)
        self._run_id: str | None = None  # uuid string of the current report.audit_runs row

    # ReportSink protocol ----------------------------------------------------

    def open_run(self, meta: "RunMeta") -> None:
        self._with_session_transaction(lambda s: self._upsert_run(s, meta=meta))

    def emit_snapshot(self, snapshot: "AuditRun") -> None:
        self._with_session_transaction(
            lambda s: self._sync_snapshot(s, snapshot=snapshot)
        )

    def upsert_finding(self, run_id: str, finding) -> None:
        """Upsert ONE finding row + its children; leave sibling rows untouched.

        Replaces the per-finding write storm where every coder settle
        triggered a full-snapshot ``emit_snapshot`` (~250 K UPSERTs for
        a 500-finding run). The streaming callback on the workflow side
        now calls this method per settled finding, so the run-time write
        count is O(N) rather than O(N²).
        """

        del run_id  # The active run is identified by ``self._run_id``.
        self._with_session_transaction(
            lambda s: self._upsert_single_finding(s, finding=finding)
        )

    def _upsert_single_finding(self, session: Session, *, finding) -> None:
        if self._run_id is None:
            # ``open_run`` has not been called yet; the run row is missing.
            # Drop the upsert silently — the next ``emit_snapshot`` (which
            # always follows ``open_run`` in services.run_audit) will
            # re-establish state.
            return
        run_row = session.get(AuditRunRow, self._run_id)
        if run_row is None:
            # We have a stamped ``_run_id`` but the row is gone — an
            # admin (or another process) deleted it from under us.
            # Silently swallowing the finding write here would lose
            # data forever; raise so the workflow aborts cleanly.
            raise RunDeletedExternallyError(self._run_id)
        finding_id = getattr(finding, "finding_id", None) or getattr(finding, "id", None)
        if not finding_id:
            return
        existing_row = session.execute(
            select(FindingRow).where(
                FindingRow.run_id == run_row.id,
                FindingRow.finding_id == str(finding_id),
            )
        ).scalar_one_or_none()
        existing = (
            {str(finding_id): existing_row} if existing_row is not None else {}
        )
        self._upsert_finding(session, run_row, finding, existing=existing)

    def write_progress(self, event: "ProgressEvent") -> None:
        self._with_session_transaction(
            lambda s: self._append_progress(s, event=event)
        )

    def close_run(self, final: "AuditRun") -> None:
        self._with_session_transaction(
            lambda s: self._sync_snapshot(s, snapshot=final, finalize=True)
        )

    def fail_run(self, *, status: str, error: str) -> None:
        """Mark the current run as terminally failed/cancelled."""

        self._with_session_transaction(
            lambda s: self._mark_terminal(s, status=status, error=error)
        )


    def write_resume_state(self, payload: Mapping[str, Any]) -> None:
        """Persist the audit-resume payload onto ``audit_runs.resume_state``.

        Replaces the on-disk ``<report_dir>/resume-state.json`` file the
        Markdown sink used to write. The portal can also read this column
        so future UI features (e.g. "this run is resumable" badges)
        don't need a separate read path.

        Best-effort: if the run row is missing the call no-ops; if the
        UPDATE itself fails the bus logs a warning so the operator
        knows resume won't work for this run, but the audit still
        proceeds.
        """

        if self._run_id is None:
            return
        self._with_session_transaction(
            lambda s: self._update_resume_state(s, payload=payload)
        )

    def _update_resume_state(
        self, session: Session, *, payload: Mapping[str, Any]
    ) -> None:
        run_row = session.get(AuditRunRow, self._run_id)
        if run_row is None:
            # ``_run_id`` is set (caller checked) but the row is gone —
            # the run was deleted externally. Abort the audit cleanly.
            raise RunDeletedExternallyError(self._run_id)
        run_row.resume_state = dict(payload)

    def _mark_terminal(
        self, session: Session, *, status: str, error: str
    ) -> None:
        if self._run_id is None:
            return
        run_row = session.get(AuditRunRow, self._run_id)
        if run_row is None:
            # The run row is gone — this is the workflow's exception
            # handler trying to flush a terminal status, but the row
            # has already been deleted externally. Silently no-op:
            # there's nothing to update, and the original exception
            # will continue to propagate up the workflow. We do NOT
            # raise RunDeletedExternallyError here because we're
            # likely already in an exception path and replacing the
            # original cause with this one would lose information.
            return
        run_row.status = status
        run_row.completed_at = datetime.now(timezone.utc)

    # Internals --------------------------------------------------------------

    def _with_session_transaction(self, fn) -> None:
        session: Session = self._session_factory()
        try:
            fn(session)
            session.commit()
        except RunDeletedExternallyError:
            # The run row is gone — admin override deletion. We
            # deliberately let this propagate so the workflow can abort
            # cleanly. All other sink errors stay swallowed (best-effort
            # secondary sink) but this one is a hard signal.
            session.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - secondary sink; never raise
            session.rollback()
            log.warning("PostgresReportSink write failed and was skipped: %s", exc)
        finally:
            session.close()

    def _upsert_run(self, session: Session, *, meta: "RunMeta") -> None:
        # The DB column is still ``report_dir`` (0.4.x name); we now
        # store the run label here.
        report_dir = str(meta.run_label)
        existing = self._find_run_by_report_dir(session, report_dir)
        if existing is None:
            row = AuditRunRow(
                repo_root=meta.repo_root,
                project_name=meta.project_name,
                build_fingerprint=meta.build_fingerprint,
                mode=meta.mode,
                status="in_progress",
                progress_percent=0,
                total_candidates=0,
                valid_findings=0,
                false_positives=0,
                unlabeled_findings=0,
                duplicate_findings=0,
                llm_providers_used=dict(meta.llm_providers_used),
                report_dir=report_dir,
                started_at=meta.started_at,
            )
            session.add(row)
            session.flush()
            self._run_id = str(row.id)
        else:
            # Resume path: same report_dir means we are continuing the same run.
            existing.repo_root = meta.repo_root
            existing.project_name = meta.project_name
            existing.build_fingerprint = meta.build_fingerprint
            existing.mode = meta.mode
            existing.llm_providers_used = dict(meta.llm_providers_used)
            existing.started_at = meta.started_at
            resumed = bool(getattr(meta, "resumed", False))
            if resumed:
                # Flip status back to in_progress and clear the terminal
                # timestamp so the portal UI un-terminates the run. The
                # child rows (findings, coverage, debates, subagents,
                # etc.) stay untouched — resume must not lose anything
                # captured by the prior execution.
                existing.status = "in_progress"
                existing.completed_at = None
                session.add(
                    ProgressEventRow(
                        run_id=existing.id,
                        stage="reporting",
                        heartbeat_kind="resumed",
                        current_path_index=None,
                        total_paths=None,
                        message="audit resumed",
                        timestamp=datetime.now(timezone.utc),
                    )
                )
            self._run_id = str(existing.id)

    def _sync_snapshot(
        self,
        session: Session,
        *,
        snapshot: "AuditRun",
        finalize: bool = False,
    ) -> None:
        run_row = self._resolve_run_row(session, snapshot)
        findings = list(snapshot.findings or ())
        # Read this run's duplicate-labeled annotations once and
        # build a set of the corresponding ``Finding.finding_id``
        # strings (F-XXXX form). Findings in this set are
        # subtracted from the LLM-side ``valid`` / ``false_positives``
        # buckets and surfaced via the new ``duplicate_findings``
        # column. The duplicate label takes priority over the LLM
        # verdict for dashboard counts (see
        # ``add-duplicate-feedback-label`` D6).
        duplicate_finding_ids: set[str] = set()
        if run_row.id is not None:
            rows = session.execute(
                select(FindingRow.finding_id)
                .join(
                    FindingAnnotation,
                    FindingAnnotation.finding_id == FindingRow.id,
                )
                .where(
                    FindingAnnotation.run_id == run_row.id,
                    FindingAnnotation.label == LABEL_DUPLICATE,
                )
            ).scalars().all()
            duplicate_finding_ids = set(rows)
        valid = sum(
            1
            for f in findings
            if _finding_is_valid(f)
            and f.finding_id not in duplicate_finding_ids
        )
        false_positives = sum(
            1
            for f in findings
            if _finding_is_false_positive(f)
            and f.finding_id not in duplicate_finding_ids
        )
        duplicates = sum(
            1 for f in findings if f.finding_id in duplicate_finding_ids
        )
        unlabeled = len(findings) - valid - false_positives - duplicates
        run_row.total_candidates = len(findings)
        run_row.valid_findings = valid
        run_row.false_positives = false_positives
        run_row.duplicate_findings = duplicates
        run_row.unlabeled_findings = max(0, unlabeled)
        if finalize:
            run_row.status = "completed"
            run_row.progress_percent = 100
            run_row.completed_at = datetime.now(timezone.utc)

        self._sync_findings(session, run_row, findings)
        # Build the {"<path_fingerprint>::f<idx>" -> "F-NNNN"} map once the
        # findings are in hand, then thread it into the writers that persist
        # per-finding references. This is what makes the portal's
        # finding_id-based queries actually retrieve debates and per-finding
        # subagent rows.
        finding_fp_map = _build_finding_fingerprint_map(findings)
        self._sync_coverage(session, run_row, snapshot)
        self._sync_debates(session, run_row, snapshot, finding_fp_map)
        self._sync_no_finding_paths(session, run_row, snapshot)
        self._sync_subagents(session, run_row, snapshot, finding_fp_map)
        self._sync_path_raw_outputs(session, run_row, snapshot)

    # Findings ---------------------------------------------------------------

    def _sync_findings(
        self,
        session: Session,
        run_row: AuditRunRow,
        findings: list,
    ) -> None:
        existing_findings = {
            row.finding_id: row
            for row in session.execute(
                select(FindingRow).where(FindingRow.run_id == run_row.id)
            ).scalars()
        }
        for source in findings:
            self._upsert_finding(session, run_row, source, existing=existing_findings)

    def _upsert_finding(
        self,
        session: Session,
        run_row: AuditRunRow,
        source,
        *,
        existing: dict[str, FindingRow],
    ) -> None:
        try:
            finding_id = getattr(source, "finding_id", None) or getattr(source, "id", None)
            if not finding_id:
                return
            row = existing.get(str(finding_id))
            function_names = getattr(source, "function_names", ()) or ()
            file_path, function_name = _suspect_location(source)
            fields = {
                "finding_id": str(finding_id),
                "path_fingerprint": _opt_str(getattr(source, "path_fingerprint", None)),
                "finding_name": _str(getattr(source, "finding_name", "")),
                "finding_description": _str(getattr(source, "finding_description", "")),
                "confidence_level": _str(_coerce_enum_value(getattr(source, "confidence_level", ""))),
                "analyzer_status": _opt_str(getattr(source, "analyzer_status", None)),
                "evidence_strength": _opt_str(getattr(source, "evidence_strength", None)),
                "analysis": _str(getattr(source, "analysis", "")),
                "reason": _str(getattr(source, "reason", "")),
                "context": _str(getattr(source, "context", "")),
                "business_context": _str(getattr(source, "business_context", "")),
                "context_notes": _opt_str(getattr(source, "context_notes", None)),
                "suspect_function_id": _opt_str(getattr(source, "suspect_function_id", None)),
                "suspect_line": _opt_int(getattr(source, "suspect_line", None)),
                "file_path": file_path,
                "function_name": function_name or (function_names[0] if function_names else None),
                "exploitation_status": _str(getattr(source, "exploitation_status", "")),
                "exploitation_steps": _str(getattr(source, "exploitation_steps", "")),
                "validation_status": _str(_coerce_enum_value(getattr(source, "validation_status", ""))),
                "validation_analysis": _str(getattr(source, "validation_analysis", "")),
            }
            if row is None:
                row = FindingRow(run_id=run_row.id, **fields)
                session.add(row)
                session.flush()
            else:
                for key, value in fields.items():
                    setattr(row, key, value)
            self._sync_finding_children(session, row, source)
        except Exception as exc:  # noqa: BLE001 - per-row resilience
            log.warning("Skipping finding row: %s", exc)

    def _sync_finding_children(
        self,
        session: Session,
        finding_row: FindingRow,
        source,
    ) -> None:
        """Replace source_references and referenced_symbols atomically.

        Snapshot semantics mean the authoritative payload for a finding
        lives on the in-memory model. We delete the existing child rows
        for this finding and re-insert from the snapshot; the whole
        operation commits in the caller's transaction alongside the
        parent ``Finding`` row.
        """

        session.execute(
            delete(SourceReferenceRow).where(
                SourceReferenceRow.finding_id == finding_row.id
            )
        )
        session.execute(
            delete(ReferencedSymbolRow).where(
                ReferencedSymbolRow.finding_id == finding_row.id
            )
        )
        self._upsert_coder_finding(session, finding_row, source)
        for ordinal, ref in enumerate(getattr(source, "source_references", ()) or ()):
            try:
                session.add(
                    SourceReferenceRow(
                        finding_id=finding_row.id,
                        file_path=_str(getattr(ref, "file_path", "")),
                        snippet=_str(getattr(ref, "snippet", "")),
                        language=_opt_str(getattr(ref, "language", None)),
                        ordinal=int(getattr(ref, "ordinal", ordinal) or ordinal),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - per-row resilience
                log.warning("Skipping finding source reference: %s", exc)
        for symbol in getattr(source, "referenced_symbols", ()) or ():
            if not isinstance(symbol, Mapping):
                continue
            try:
                session.add(
                    ReferencedSymbolRow(
                        finding_id=finding_row.id,
                        name=_str(symbol.get("name", "")),
                        kind=_opt_str(symbol.get("kind")),
                        module_name=_opt_str(symbol.get("module_name")),
                        file_path=_opt_str(symbol.get("file_path")),
                        start_line=_opt_int(symbol.get("start_line")),
                        end_line=_opt_int(symbol.get("end_line")),
                        type_annotation=_opt_str(symbol.get("type_annotation")),
                        value_repr=_opt_str(symbol.get("value_repr")),
                        is_placeholder=bool(symbol.get("is_placeholder", False)),
                        used_by=list(symbol.get("used_by") or []),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - per-row resilience
                log.warning("Skipping referenced symbol: %s", exc)

    # Coder verification -----------------------------------------------------

    def _upsert_coder_finding(
        self,
        session: Session,
        finding_row: FindingRow,
        source,
    ) -> None:
        """Upsert the per-finding coder verdict and replace its evidence.

        Always writes a row, even when the verdict is ``Skipped``, so the
        DB schema is uniform across runs (matches the spec's "every finding
        has a coder row" contract). The evidence list is replaced atomically
        on every snapshot so a Pending → Verdict transition cannot leave
        stale rows behind.
        """

        try:
            run_id = finding_row.run_id
            finding_ref = finding_row.finding_id
            existing = session.execute(
                select(CoderFindingRow).where(
                    CoderFindingRow.run_id == run_id,
                    CoderFindingRow.finding_ref == finding_ref,
                )
            ).scalar_one_or_none()
            status = _str(getattr(source, "coder_status", "Skipped")) or "Skipped"
            analysis = _str(getattr(source, "coder_analysis", ""))
            reason = _str(getattr(source, "coder_reason", ""))
            if existing is None:
                row = CoderFindingRow(
                    run_id=run_id,
                    finding_ref=finding_ref,
                    status=status,
                    analysis=analysis,
                    reason=reason,
                )
                session.add(row)
                session.flush()
            else:
                row = existing
                row.status = status
                row.analysis = analysis
                row.reason = reason
                # Drop the prior evidence so the new snapshot's items
                # replace it cleanly. SQLAlchemy cascades the delete via
                # the FK, but we issue an explicit DELETE to keep the
                # operation idempotent at the SQL level.
                session.execute(
                    delete(CoderEvidenceRow).where(
                        CoderEvidenceRow.coder_finding_id == row.id
                    )
                )
            for ordinal, evidence in enumerate(
                getattr(source, "coder_call_chain_evidence", ()) or ()
            ):
                try:
                    session.add(
                        CoderEvidenceRow(
                            coder_finding_id=row.id,
                            file_path=_str(getattr(evidence, "file_path", "")),
                            function_name=_opt_str(
                                getattr(evidence, "function_name", None)
                            ),
                            snippet=_str(getattr(evidence, "snippet", "")),
                            language=_opt_str(getattr(evidence, "language", None)),
                            role=_str(getattr(evidence, "role", "supporting")) or "supporting",
                            ordinal=ordinal,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - per-row resilience
                    log.warning("Skipping coder finding evidence: %s", exc)
        except Exception as exc:  # noqa: BLE001 - per-row resilience
            log.warning("Skipping coder finding row: %s", exc)

    # Coverage ---------------------------------------------------------------

    def _sync_coverage(
        self,
        session: Session,
        run_row: AuditRunRow,
        snapshot: "AuditRun",
    ) -> None:
        coverage = getattr(snapshot, "coverage", None)
        if coverage is None:
            return
        # Snapshot semantics: replace all coverage rows for the run.
        session.execute(
            delete(CoverageModuleRow).where(CoverageModuleRow.run_id == run_row.id)
        )
        session.execute(
            delete(CoverageFileRow).where(CoverageFileRow.run_id == run_row.id)
        )
        session.execute(
            delete(CoverageFunctionRow).where(CoverageFunctionRow.run_id == run_row.id)
        )
        for record in coverage.items("module"):
            try:
                session.add(
                    CoverageModuleRow(
                        run_id=run_row.id,
                        module_name=_str(record.identifier),
                        status=_coverage_status(record.state),
                        reason=None,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - per-row resilience
                log.warning("Skipping coverage module row: %s", exc)
        for record in coverage.items("file"):
            try:
                session.add(
                    CoverageFileRow(
                        run_id=run_row.id,
                        file_path=_str(record.identifier),
                        status=_coverage_status(record.state),
                        reason=None,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - per-row resilience
                log.warning("Skipping coverage file row: %s", exc)
        for record in coverage.items("function"):
            try:
                qualified = _str(record.identifier)
                session.add(
                    CoverageFunctionRow(
                        run_id=run_row.id,
                        function_id=qualified,
                        qualified_name=qualified,
                        file_path="",
                        status=_coverage_status(record.state),
                        reason=None,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - per-row resilience
                log.warning("Skipping coverage function row: %s", exc)

    # Debates ----------------------------------------------------------------

    def _sync_debates(
        self,
        session: Session,
        run_row: AuditRunRow,
        snapshot: "AuditRun",
        finding_fp_map: dict[str, str],
    ) -> None:
        debates = _collect_debates(snapshot)
        session.execute(
            delete(ValidatorDebateRow).where(ValidatorDebateRow.run_id == run_row.id)
        )
        for finding_ref, entry in debates.items():
            try:
                path_fingerprint = _str(entry.get("path_fingerprint", finding_ref))
                rounds = _debate_rounds(entry)
                resolved_ref = finding_fp_map.get(str(finding_ref))
                if resolved_ref is None:
                    log.warning(
                        "validator_debates entry %r in run %s has no matching finding; "
                        "persisting with the raw key so the Markdown path still resolves, "
                        "but the portal will not join it to a finding",
                        finding_ref,
                        run_row.id,
                    )
                    resolved_ref = str(finding_ref)
                session.add(
                    ValidatorDebateRow(
                        run_id=run_row.id,
                        path_fingerprint=path_fingerprint,
                        finding_ref=resolved_ref,
                        rounds=rounds,
                        final_verdict=_str(entry.get("final_verdict", "")),
                        convergence_state=_str(
                            "converged"
                            if bool(entry.get("converged", False))
                            else "cap_reached"
                        ),
                        configured_round_cap=int(entry.get("max_rounds", 0) or 0),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - per-row resilience
                log.warning("Skipping validator debate row: %s", exc)

    # No-finding paths -------------------------------------------------------

    def _sync_no_finding_paths(
        self,
        session: Session,
        run_row: AuditRunRow,
        snapshot: "AuditRun",
    ) -> None:
        session.execute(
            delete(NoFindingPathRow).where(NoFindingPathRow.run_id == run_row.id)
        )
        chains_by_fp = {
            chain.path_fingerprint: chain
            for chain in getattr(getattr(snapshot, "coverage", None), "audited_call_chains", [])
            or []
        }
        for fingerprint, payload in (snapshot.shared_state or {}).items():
            if not isinstance(payload, Mapping):
                continue
            exploitation = payload.get("exploitation")
            validator = payload.get("validator")
            exploitation_skipped = (
                isinstance(exploitation, Mapping) and exploitation.get("status") == "skipped"
            )
            validator_skipped = (
                isinstance(validator, Mapping) and validator.get("status") == "skipped"
            )
            if not (exploitation_skipped or validator_skipped):
                continue
            chain = chains_by_fp.get(fingerprint)
            analyzer = payload.get("analyzer") if isinstance(payload.get("analyzer"), Mapping) else {}
            skipped_stages: list[str] = []
            if exploitation_skipped:
                skipped_stages.append("exploitation")
            if validator_skipped:
                skipped_stages.append("validator")
            try:
                session.add(
                    NoFindingPathRow(
                        run_id=run_row.id,
                        path_fingerprint=_str(fingerprint),
                        entry_function=_str(chain.entry_function if chain else ""),
                        function_chain=list(chain.function_chain) if chain else [],
                        analyzer_status=_str(analyzer.get("status", "")),
                        analyzer_reason=_opt_str(analyzer.get("reason")),
                        skipped_stages=skipped_stages,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - per-row resilience
                log.warning("Skipping no-finding-path row: %s", exc)

    # Subagent records -------------------------------------------------------

    def _sync_subagents(
        self,
        session: Session,
        run_row: AuditRunRow,
        snapshot: "AuditRun",
        finding_fp_map: dict[str, str],
    ) -> None:
        session.execute(
            delete(AnalyzerSubagentRow).where(AnalyzerSubagentRow.run_id == run_row.id)
        )
        session.execute(
            delete(ValidatorSubagentRow).where(ValidatorSubagentRow.run_id == run_row.id)
        )
        session.execute(
            delete(ExploiterSubagentRow).where(ExploiterSubagentRow.run_id == run_row.id)
        )
        for fingerprint, payload in (snapshot.shared_state or {}).items():
            if not isinstance(payload, Mapping):
                continue
            # Analyzer subagents are a flat list keyed by "analyzer_subagents".
            for record in payload.get("analyzer_subagents") or []:
                if not isinstance(record, Mapping):
                    continue
                try:
                    session.add(
                        AnalyzerSubagentRow(
                            run_id=run_row.id,
                            path_fingerprint=_str(fingerprint),
                            subagent_index=int(record.get("subagent_index", 0) or 0),
                            provider_name=_str(record.get("provider_name", "")),
                            raw_output=_json_safe(record),
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - per-row resilience
                    log.warning("Skipping analyzer subagent row: %s", exc)
            # Validator / exploiter subagents are keyed by finding_ref -> list.
            # Translate the per-path key (<path_fingerprint>::f<idx>) to the
            # owning Finding.finding_id so the portal can join by finding_id.
            for finding_ref, records in (payload.get("validator_subagents") or {}).items():
                if not isinstance(records, list):
                    continue
                resolved_ref = finding_fp_map.get(str(finding_ref), str(finding_ref))
                for record in records:
                    if not isinstance(record, Mapping):
                        continue
                    try:
                        session.add(
                            ValidatorSubagentRow(
                                run_id=run_row.id,
                                path_fingerprint=_str(fingerprint),
                                subagent_index=int(record.get("subagent_index", 0) or 0),
                                provider_name=_str(record.get("provider_name", "")),
                                raw_output=_json_safe(record),
                                finding_ref=_opt_str(resolved_ref),
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - per-row resilience
                        log.warning("Skipping validator subagent row: %s", exc)
            for finding_ref, records in (payload.get("exploiter_subagents") or {}).items():
                if not isinstance(records, list):
                    continue
                resolved_ref = finding_fp_map.get(str(finding_ref), str(finding_ref))
                for record in records:
                    if not isinstance(record, Mapping):
                        continue
                    try:
                        session.add(
                            ExploiterSubagentRow(
                                run_id=run_row.id,
                                path_fingerprint=_str(fingerprint),
                                subagent_index=int(record.get("subagent_index", 0) or 0),
                                provider_name=_str(record.get("provider_name", "")),
                                raw_output=_json_safe(record),
                                finding_ref=_opt_str(resolved_ref),
                            )
                        )
                    except Exception as exc:  # noqa: BLE001 - per-row resilience
                        log.warning("Skipping exploiter subagent row: %s", exc)

    # Path raw outputs -------------------------------------------------------

    def _sync_path_raw_outputs(
        self,
        session: Session,
        run_row: AuditRunRow,
        snapshot: "AuditRun",
    ) -> None:
        session.execute(
            delete(PathRawOutputRow).where(PathRawOutputRow.run_id == run_row.id)
        )
        for fingerprint, payload in (snapshot.shared_state or {}).items():
            if not isinstance(payload, Mapping):
                continue
            for stage in ("analyzer", "validator", "exploitation"):
                stage_payload = payload.get(stage)
                if not isinstance(stage_payload, Mapping):
                    continue
                try:
                    serialized = json.dumps(_json_safe(stage_payload), ensure_ascii=False, indent=2)
                except (TypeError, ValueError):
                    serialized = str(stage_payload)
                try:
                    session.add(
                        PathRawOutputRow(
                            run_id=run_row.id,
                            stage=stage,
                            path_fingerprint=_str(fingerprint),
                            raw_markdown=serialized,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - per-row resilience
                    log.warning("Skipping path raw output row: %s", exc)

    # Progress ---------------------------------------------------------------

    def _append_progress(self, session: Session, *, event: "ProgressEvent") -> None:
        if self._run_id is None:
            # open_run hasn't happened yet; ignore pre-open progress events.
            return
        run_row = session.get(AuditRunRow, self._run_id)
        if run_row is None:
            # ``_run_id`` is set but the row is gone — the run was
            # deleted externally. Inserting the ProgressEventRow below
            # would FK-violate; raise so the workflow aborts cleanly.
            raise RunDeletedExternallyError(self._run_id)
        derived = _derive_progress_percent(event, run_row.progress_percent)
        if derived is not None:
            run_row.progress_percent = derived
        session.add(
            ProgressEventRow(
                run_id=self._run_id,
                stage=event.stage,
                heartbeat_kind=event.heartbeat_kind,
                current_path_index=event.current_path_index,
                total_paths=event.total_paths,
                message=event.message,
                timestamp=event.timestamp,
            )
        )

    def _resolve_run_row(self, session: Session, snapshot) -> AuditRunRow:
        """Look up the active run row for this sink instance.

        ``open_run`` must have been called first; it stamps ``self._run_id``.
        We deliberately do NOT fall back to ``build_fingerprint`` here — many
        audit runs can share one graph build, and picking "the most recent
        row with this fingerprint" would collapse distinct runs onto the
        same DB row (the exact bug this sink used to have).

        Two distinct missing-row cases must be told apart:

        - ``_run_id is not None`` but ``session.get`` returns no row:
          the sink had a stamped run id (open_run succeeded) but the
          row has since been deleted out from under us. This happens
          when an admin runs ``DELETE /api/runs/{id}`` against an
          in-progress run from the portal. Creating a brand-new orphan
          row here would silently corrupt state — every subsequent
          write would land in a phantom row that no one is watching.
          We raise ``RunDeletedExternallyError`` so the audit aborts
          cleanly with a clear message.
        - ``_run_id is None``: ``open_run`` was never called. This is
          a programming bug, not an admin action. We materialize a
          minimal orphan row (flagged with a descriptive ``project_name``)
          so the write does not crash, log a warning, and continue —
          the orphan row is what operators see and prune when they
          investigate the bug.
        """

        if self._run_id is not None:
            row = session.get(AuditRunRow, self._run_id)
            if row is not None:
                return row
            raise RunDeletedExternallyError(self._run_id)
        fingerprint = getattr(snapshot, "build_fingerprint", None)
        row = AuditRunRow(
            repo_root="",
            project_name="(orphan — open_run not called)",
            build_fingerprint=fingerprint or "unknown",
            mode="single",
            status="in_progress",
        )
        session.add(row)
        session.flush()
        self._run_id = str(row.id)
        log.warning(
            "PostgresReportSink emit_snapshot called before open_run; "
            "created orphan run row id=%s",
            self._run_id,
        )
        return row

    @staticmethod
    def _find_run_by_report_dir(
        session: Session, report_dir: str
    ) -> AuditRunRow | None:
        return session.execute(
            select(AuditRunRow)
            .where(AuditRunRow.report_dir == report_dir)
            .limit(1)
        ).scalar_one_or_none()


def _finding_is_valid(finding) -> bool:
    # ``Inconclusive`` is intentionally NOT in the valid bucket:
    # the validator couldn't reach a verdict, the finding needs
    # human review, and it falls through subtraction into the
    # ``unlabeled`` bucket which the dashboard surfaces as the
    # operator-actionable backlog. Lumping it here used to produce
    # ``valid + false_positives == total``, making the dashboard's
    # "Unlabeled" metric stuck on 0 forever.
    status = _coerce_enum_value(getattr(finding, "validation_status", ""))
    return str(status).strip().lower() in {"valid", "partial valid"}


def _finding_is_false_positive(finding) -> bool:
    status = _coerce_enum_value(getattr(finding, "validation_status", ""))
    return str(status).strip().lower() == "false positive"


def _coerce_enum_value(value):
    return getattr(value, "value", value)


def _str(value) -> str:
    if value is None:
        return ""
    return str(_coerce_enum_value(value))


def _opt_str(value) -> str | None:
    if value is None:
        return None
    coerced = _coerce_enum_value(value)
    if coerced is None or coerced == "":
        return None
    return str(coerced)


def _opt_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coverage_status(state) -> str:
    raw = _str(state).strip().lower() or "unaudited"
    return _COVERAGE_STATE_TO_STATUS.get(raw, raw)


def _suspect_location(finding) -> tuple[str | None, str | None]:
    """Pick a representative file_path / function_name for the row header.

    Prefer the first source reference (which is what the Markdown renderer
    uses) and fall back to the finding's explicit function_names tuple.
    """

    refs = list(getattr(finding, "source_references", ()) or ())
    if refs:
        first = refs[0]
        file_path = _opt_str(getattr(first, "file_path", None))
        function_names = getattr(finding, "function_names", ()) or ()
        return file_path, function_names[0] if function_names else None
    function_names = getattr(finding, "function_names", ()) or ()
    return None, function_names[0] if function_names else None


def _build_finding_fingerprint_map(findings: list) -> dict[str, str]:
    """Map ``<path_fingerprint>::f<intra-path-idx>`` → ``Finding.finding_id``.

    ``workflow.py`` keys ``validator_debates`` and the per-finding
    ``validator_subagents`` / ``exploiter_subagents`` maps by
    ``f"{path_fingerprint}::f{finding_index}"`` where ``finding_index`` is
    the local enumeration within a single path. The DB-side ``Finding`` row
    identifies the same finding by its globally-assigned ``F-NNNN`` id.
    This helper rebuilds the link so the sink can translate one to the
    other at persist time, which is what lets the portal's
    ``finding_id``-based queries actually retrieve debates and per-finding
    subagent records.
    """

    by_path: dict[str, list[Any]] = {}
    for finding in findings:
        path_fp = getattr(finding, "path_fingerprint", None) or ""
        if not path_fp:
            continue
        by_path.setdefault(str(path_fp), []).append(finding)
    mapping: dict[str, str] = {}
    for path_fp, group in by_path.items():
        for idx, finding in enumerate(group):
            finding_id = getattr(finding, "finding_id", None)
            if not finding_id:
                continue
            mapping[f"{path_fp}::f{idx}"] = str(finding_id)
    return mapping


def _collect_debates(snapshot: "AuditRun") -> dict[str, dict[str, Any]]:
    """Merge per-path debate maps (keyed by finding_ref) from shared_state."""

    merged: dict[str, dict[str, Any]] = {}
    for fingerprint, payload in (snapshot.shared_state or {}).items():
        if not isinstance(payload, Mapping):
            continue
        debate_map = payload.get("validator_debates")
        if not isinstance(debate_map, Mapping):
            continue
        for finding_ref, debate in debate_map.items():
            if not isinstance(debate, Mapping):
                continue
            entry = dict(debate)
            entry.setdefault("path_fingerprint", fingerprint)
            merged[str(finding_ref)] = entry
    return merged


def _debate_rounds(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Normalize the debate 'turns' list into a per-round JSON structure."""

    turns = entry.get("turns") or []
    rounds: dict[int, list[dict[str, Any]]] = {}
    for turn in turns:
        if not isinstance(turn, Mapping):
            continue
        round_index = int(turn.get("round_index", 0) or 0)
        rounds.setdefault(round_index, []).append(
            {
                "subagent_index": turn.get("subagent_index"),
                "provider_name": turn.get("provider_name"),
                "verdict": turn.get("verdict"),
                "rebuttal": turn.get("rebuttal"),
                "system_prompt": turn.get("system_prompt"),
                "user_message": turn.get("user_message"),
                "raw_response": turn.get("raw_response"),
            }
        )
    return [
        {"round_index": idx, "turns": rounds[idx]}
        for idx in sorted(rounds)
    ]


def _json_safe(value: Any) -> Any:
    """Best-effort conversion of dataclass / enum / frozen structures to JSON primitives."""

    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "value") and not isinstance(value, (str, bytes, int, float, bool)):
        return _json_safe(value.value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _derive_progress_percent(event, current: int | None) -> int | None:
    kind = getattr(event, "heartbeat_kind", "") or ""
    if kind == "finished":
        return 100
    if kind == "failed":
        return current
    total = getattr(event, "total_paths", None)
    idx = getattr(event, "current_path_index", None)
    if total and idx is not None and total > 0:
        fraction = min(max(idx / total, 0.0), 1.0)
        return int(round(fraction * 100))
    return current


@dataclass(frozen=True)
class ResumeTarget:
    """Resolved audit-resume target read from ``audit_runs``.

    ``run_label`` is the per-invocation identifier (the ``YYYYMMDD-HHMMSS``
    string in ``audit_runs.report_dir``). ``resume_state`` is the
    persisted JSONB payload — None means the run row exists but its
    resume state was never written (legacy xauditor 0.4.x; resume code
    surfaces a clear "started on a previous version" error).
    ``status`` is the row's ``status`` column at lookup time so the
    resume code can decide whether the run is actually resumable.
    """

    run_id: str
    run_label: str
    status: str
    build_fingerprint: str
    resume_state: dict[str, Any] | None
    completed_at: datetime | None


def fetch_resume_target(
    config: "ReportDBConfig", *, run_label: str | None = None
) -> ResumeTarget | None:
    """Resolve which audit run to resume.

    When ``run_label`` is supplied, return the matching ``audit_runs``
    row (regardless of status — caller decides resumability). When
    ``run_label`` is None, return the most-recent failed / cancelled /
    in-progress row (i.e. anything that is not ``completed``). Returns
    ``None`` when nothing matches.

    Reads through a synchronous engine identical to the one
    ``PostgresReportSink`` uses, so reachability problems surface here
    the same way the run-time sink would surface them.
    """

    engine = create_engine(_sync_url_for(config), pool_pre_ping=True)
    Session_ = sessionmaker(bind=engine, expire_on_commit=False)
    session: Session = Session_()
    try:
        if run_label is not None:
            stmt = select(AuditRunRow).where(AuditRunRow.report_dir == run_label).limit(1)
        else:
            stmt = (
                select(AuditRunRow)
                .where(AuditRunRow.status.in_(("failed", "cancelled", "in_progress")))
                .order_by(AuditRunRow.started_at.desc())
                .limit(1)
            )
        row = session.execute(stmt).scalar_one_or_none()
        if row is None:
            return None
        return ResumeTarget(
            run_id=str(row.id),
            run_label=str(row.report_dir or ""),
            status=str(row.status),
            build_fingerprint=str(row.build_fingerprint),
            resume_state=dict(row.resume_state) if row.resume_state is not None else None,
            completed_at=row.completed_at,
        )
    finally:
        session.close()
        engine.dispose()


__all__ = ["PostgresReportSink", "ResumeTarget", "fetch_resume_target"]
