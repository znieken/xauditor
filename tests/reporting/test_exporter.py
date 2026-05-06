"""Unit tests for the on-demand audit-export verb (Phase 2 §7).

Live-DB integration tests are out of scope for this file — they ship in
``packages/xauditor-portal/tests/sinks/test_postgres_sink_integration.py``
(gated on ``XAUDITOR_TEST_DATABASE_URL``). The tests below mock the DB
boundary so the envelope-shape, Markdown-bundle, and redaction
contracts are validated without a Postgres dependency.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xauditor.models import (
    AuditRun,
    ConfidenceLevel,
    CoverageInventory,
    CoverageRecord,
    CoverageState,
    Finding,
    SourceReference,
    ValidationStatus,
)
from xauditor.reporting.exporter import (
    JSON_FORMAT_VERSION,
    _COVERAGE_STATE_BY_DB_VALUE,
    export_audit_run,
)


def _fixture_envelope_and_audit_run(
    *, include_debug: bool = False
) -> tuple[dict, AuditRun]:
    finding = Finding(
        finding_id="F-0001",
        finding_name="SQL Injection",
        finding_description="user input flows into raw SQL",
        confidence_level=ConfidenceLevel.HIGH,
        source_references=(
            SourceReference(
                file_path="src/app.py",
                start_line=0,
                end_line=0,
                focus_lines=(),
                language="python",
                snippet="db.exec(payload)",
            ),
        ),
        analysis="user input flows into raw SQL",
        reason="payload is not parameterised",
        context="entry → handler → db",
        business_context="login endpoint",
        exploitation_status="exploitable",
        exploitation_steps="POST {\"username\": \"' OR 1=1--\"}",
        validation_status=ValidationStatus.VALID,
        validation_analysis="confirmed",
        path_fingerprint="path-1",
    )
    coverage = CoverageInventory()
    coverage.add(
        CoverageRecord(
            category="module", identifier="app", state=CoverageState.AUDITED
        )
    )
    audit_run = AuditRun(
        build_fingerprint="bf-test",
        findings=(finding,),
        coverage=coverage,
    )
    envelope = {
        "format_version": JSON_FORMAT_VERSION,
        "run": {
            "run_id": "uuid-1",
            "run_label": "20260429-010203",
            "build_fingerprint": "bf-test",
            "started_at": "2026-04-29T01:02:03+00:00",
            "completed_at": "2026-04-29T01:05:03+00:00",
            "status": "completed",
            "mode": "fast",
            "llm_providers_used": {"auditor": "shared", "validator": "shared"},
            "totals": {
                "candidates": 1,
                "valid": 1,
                "false_positives": 0,
                "unlabeled": 0,
            },
        },
        "findings": [
            {
                "finding_id": "F-0001",
                "finding_name": "SQL Injection",
                "finding_description": "user input flows into raw SQL",
                "confidence_level": "HIGH",
                "validation_status": "Valid",
                "source_references": [
                    {
                        "file_path": "src/app.py",
                        "snippet": "db.exec(payload)",
                        "language": "python",
                        "ordinal": 0,
                    }
                ],
                "coder": None,
            }
        ],
        "coverage": {"modules": [{"identifier": "app", "status": "audited"}]},
        "validator_debates": [],
    }
    if include_debug:
        envelope["debug"] = {"findings": {}}
    return envelope, audit_run


class ExporterArgValidationTests(unittest.TestCase):
    def test_unknown_format_raises(self) -> None:
        with self.assertRaises(ValueError):
            export_audit_run(
                config=None,  # type: ignore[arg-type]
                run_label="20260429-010203",
                fmt="csv",
            )

    def test_markdown_requires_output_dir(self) -> None:
        with self.assertRaises(ValueError):
            export_audit_run(
                config=None,  # type: ignore[arg-type]
                run_label="20260429-010203",
                fmt="markdown",
                output_dir=None,
            )


class ExporterJsonEnvelopeTests(unittest.TestCase):
    def test_default_json_envelope_shape(self) -> None:
        envelope, audit_run = _fixture_envelope_and_audit_run()
        with patch(
            "xauditor.reporting.exporter._load_envelope_and_audit_run",
            return_value=(envelope, audit_run),
        ):
            text = export_audit_run(
                config=None,  # type: ignore[arg-type]
                run_label="20260429-010203",
                fmt="json",
            )
        document = json.loads(text)
        self.assertEqual(
            set(document.keys()),
            {"format_version", "run", "findings", "coverage", "validator_debates"},
        )
        self.assertEqual(document["format_version"], "1")
        self.assertNotIn("debug", document)

    def test_envelope_mode_field_passes_through_new_literals(self) -> None:
        """`run.mode` is `"fast"` / `"deep"` after Phase 1 rename.

        Snapshot test for `restructure-audit-modes-and-coverage`
        Phase 1 task 1.6.7 — confirms the exporter does NOT
        synthesize a mode value from anywhere else and faithfully
        passes through the persisted column literal. Future
        consumers (CI dashboards, RL training) can rely on
        `{"fast", "deep"}` as the closed value set.
        """

        for mode_literal in ("fast", "deep"):
            with self.subTest(mode=mode_literal):
                envelope, audit_run = _fixture_envelope_and_audit_run()
                envelope["run"]["mode"] = mode_literal
                with patch(
                    "xauditor.reporting.exporter._load_envelope_and_audit_run",
                    return_value=(envelope, audit_run),
                ):
                    text = export_audit_run(
                        config=None,  # type: ignore[arg-type]
                        run_label="20260429-010203",
                        fmt="json",
                    )
                document = json.loads(text)
                self.assertEqual(document["run"]["mode"], mode_literal)

    def test_include_debug_emits_debug_top_level_key(self) -> None:
        envelope, audit_run = _fixture_envelope_and_audit_run(include_debug=True)
        with patch(
            "xauditor.reporting.exporter._load_envelope_and_audit_run",
            return_value=(envelope, audit_run),
        ):
            text = export_audit_run(
                config=None,  # type: ignore[arg-type]
                run_label="20260429-010203",
                fmt="json",
                include_debug=True,
            )
        document = json.loads(text)
        self.assertIn("debug", document)


class ExporterMarkdownBundleTests(unittest.TestCase):
    def test_markdown_writes_findings_and_coverage_files(self) -> None:
        envelope, audit_run = _fixture_envelope_and_audit_run()
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "xauditor.reporting.exporter._load_envelope_and_audit_run",
                return_value=(envelope, audit_run),
            ):
                paths = export_audit_run(
                    config=None,  # type: ignore[arg-type]
                    run_label="20260429-010203",
                    fmt="markdown",
                    output_dir=Path(tmp),
                )
            self.assertIsInstance(paths, list)
            names = sorted(p.name for p in paths)
            self.assertIn("findings.md", names)
            self.assertIn("false-positives.md", names)
            self.assertIn("coverage-report.md", names)
            findings_text = (Path(tmp) / "findings.md").read_text(encoding="utf-8")
            self.assertIn("F-0001", findings_text)
            self.assertIn("SQL Injection", findings_text)


class ExporterRedactionTests(unittest.TestCase):
    """The exported output SHALL pass through ``logger.redact`` so secrets
    configured at run time never appear in operator-facing artefacts.
    """

    def test_logger_redact_scrubs_secret_from_json(self) -> None:
        from xauditor.runtime_logging import RuntimeLogger

        envelope, audit_run = _fixture_envelope_and_audit_run()
        # Plant the secret somewhere a future user might paste it —
        # here, into the finding's description as if a model echoed
        # an API key back.
        envelope["findings"][0]["finding_description"] = (
            "user input flows into raw SQL — token sk-test-secret-xyz"
        )
        logger = RuntimeLogger(stream=None, level="info", redactions=("sk-test-secret-xyz",))
        with patch(
            "xauditor.reporting.exporter._load_envelope_and_audit_run",
            return_value=(envelope, audit_run),
        ):
            text = export_audit_run(
                config=None,  # type: ignore[arg-type]
                run_label="20260429-010203",
                fmt="json",
                logger=logger,
            )
        self.assertNotIn("sk-test-secret-xyz", text)
        self.assertIn("***", text)

    def test_logger_redact_scrubs_secret_from_markdown(self) -> None:
        from xauditor.runtime_logging import RuntimeLogger

        envelope, audit_run = _fixture_envelope_and_audit_run()
        # Mutate the AuditRun finding so the secret ends up in
        # rendered Markdown.
        leaky_finding = audit_run.findings[0]
        from dataclasses import replace as dataclass_replace

        leaky_finding = dataclass_replace(
            leaky_finding,
            analysis=leaky_finding.analysis + " token=sk-test-secret-xyz",
        )
        audit_run_mut = AuditRun(
            build_fingerprint=audit_run.build_fingerprint,
            findings=(leaky_finding,),
            coverage=audit_run.coverage,
        )
        logger = RuntimeLogger(stream=None, level="info", redactions=("sk-test-secret-xyz",))
        with tempfile.TemporaryDirectory() as tmp:
            with patch(
                "xauditor.reporting.exporter._load_envelope_and_audit_run",
                return_value=(envelope, audit_run_mut),
            ):
                paths = export_audit_run(
                    config=None,  # type: ignore[arg-type]
                    run_label="20260429-010203",
                    fmt="markdown",
                    output_dir=Path(tmp),
                    logger=logger,
                )
            for path in paths:
                content = path.read_text(encoding="utf-8")
                self.assertNotIn("sk-test-secret-xyz", content)


class CoverageStateRoundTripTests(unittest.TestCase):
    """The DB-string ↔ CoverageState mapping must round-trip every state.

    Anchors ``failed`` end-to-end so a future renamer cannot quietly
    break the spec by editing one side of the bridge.
    """

    def test_failed_round_trips(self) -> None:
        self.assertEqual(_COVERAGE_STATE_BY_DB_VALUE["failed"], CoverageState.FAILED)

    def test_every_coverage_state_has_a_db_string(self) -> None:
        # Reverse map: CoverageState → at least one DB string maps back to it.
        reverse: dict[CoverageState, str] = {
            state: db_value
            for db_value, state in _COVERAGE_STATE_BY_DB_VALUE.items()
        }
        for member in CoverageState:
            self.assertIn(
                member,
                reverse,
                f"CoverageState.{member.name} has no DB-string mapping; "
                "exporter readers will fall back to NOT_AUDITED for any row "
                "carrying its on-disk string.",
            )


if __name__ == "__main__":
    unittest.main()
