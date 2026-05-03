"""CLI + resume-target resolver tests.

Covers:
- `xauditor audit run --build X` parses as a fresh-run invocation.
- `xauditor audit resume --run-id TS` parses as a resume invocation.
- `xauditor audit --build X` (legacy bare-with-flag) is shimmed to
  `xauditor audit run --build X` with a deprecation warning.
- Bare `xauditor audit` is shimmed to `xauditor audit run`.
- `xauditor audit run --help` passes through untouched.

Phase 2 of make-postgres-the-canonical-sink moved the resume-target
picker out of the on-disk ``<report_dir>/resume-state.json`` walker
(``resolve_most_recent_resumable``) into a Postgres query
(``xauditor_portal.sinks.postgres_sink.fetch_resume_target``). The
on-disk picker tests in this file have been removed; the DB picker is
covered by the live-DB integration suite under
``packages/xauditor-portal/tests/sinks/test_postgres_sink_integration.py``.
"""

from __future__ import annotations

import unittest

from xauditor.cli import _apply_legacy_audit_shim, build_parser


class CliShimTests(unittest.TestCase):
    def test_bare_audit_is_shimmed(self) -> None:
        argv, fired = _apply_legacy_audit_shim(["audit"])
        self.assertEqual(argv, ["audit", "run"])
        self.assertTrue(fired)

    def test_audit_with_build_is_shimmed(self) -> None:
        argv, fired = _apply_legacy_audit_shim(["audit", "--build", "bf-1"])
        self.assertEqual(argv, ["audit", "run", "--build", "bf-1"])
        self.assertTrue(fired)

    def test_run_subverb_is_not_shimmed(self) -> None:
        argv, fired = _apply_legacy_audit_shim(["audit", "run"])
        self.assertFalse(fired)
        self.assertEqual(argv, ["audit", "run"])

    def test_resume_subverb_is_not_shimmed(self) -> None:
        argv, fired = _apply_legacy_audit_shim(
            ["audit", "resume", "--run-id", "20260419-083512"]
        )
        self.assertFalse(fired)
        self.assertEqual(argv, ["audit", "resume", "--run-id", "20260419-083512"])

    def test_help_passes_through(self) -> None:
        argv, fired = _apply_legacy_audit_shim(["audit", "--help"])
        self.assertFalse(fired)


class CliParserTests(unittest.TestCase):
    def test_audit_run_parses_with_build(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["audit", "run", "--build", "fp-1"])
        self.assertEqual(args.command, "audit")
        self.assertEqual(args.audit_command, "run")
        self.assertEqual(args.build, "fp-1")

    def test_audit_resume_parses_with_run_id(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["audit", "resume", "--run-id", "20260419-083512"]
        )
        self.assertEqual(args.command, "audit")
        self.assertEqual(args.audit_command, "resume")
        self.assertEqual(args.run_id, "20260419-083512")
        self.assertIsNone(args.build)

    def test_audit_resume_parses_without_run_id(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["audit", "resume"])
        self.assertIsNone(args.run_id)


if __name__ == "__main__":
    unittest.main()
