"""Tests for `coverage_gaps.compute_coverage_gaps` (Phase 5A).

Covers `restructure-audit-modes-and-coverage` Phase 5 tasks
5.4.1, 5.4.2, 5.7.3, 5.7.4 partially:

- `compute_coverage_gaps(mode="fast")` returns the expected
  per-mode taxonomy projection (audited / skipped_by_mode /
  out_of_scope / advice_to_user).
- `compute_coverage_gaps(mode="deep")` audits a strict
  superset of fast mode and surfaces no `skipped_by_mode`
  classes (deep covers everything any mode covers).
- `to_payload()` round-trips cleanly to a JSONB-compatible
  dict (lists, strings only — no tuples).
- `to_markdown_section()` renders the documented `##
  Coverage Gaps` block with all three groups.
- `out_of_scope` is identical across modes (the closed set
  of classes xauditor doesn't audit at all).
- Operator override of `units` is respected.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.coverage_gaps import (
    CoverageGaps,
    compute_coverage_gaps,
)


class FastModeTaxonomyTests(unittest.TestCase):
    def test_fast_audited_includes_path_sink_entry_classes(self) -> None:
        gaps = compute_coverage_gaps(mode="fast")
        # Path-shaped catches:
        self.assertIn("sql_injection", gaps.audited_classes)
        self.assertIn("command_injection", gaps.audited_classes)
        self.assertIn("ssrf", gaps.audited_classes)
        self.assertIn("path_traversal", gaps.audited_classes)
        self.assertIn("deserialization", gaps.audited_classes)
        self.assertIn("xss", gaps.audited_classes)
        self.assertIn("xxe", gaps.audited_classes)
        # Sink + Entry catches:
        self.assertIn("sink_convergence", gaps.audited_classes)
        self.assertIn("entry_exposure", gaps.audited_classes)

    def test_fast_skipped_by_mode_lists_deep_only_classes(self) -> None:
        gaps = compute_coverage_gaps(mode="fast")
        # Deep-only unit kinds catch these:
        self.assertIn("state_machine_violation", gaps.skipped_by_mode)
        self.assertIn("toctou_race", gaps.skipped_by_mode)
        self.assertIn("cross_process_taint", gaps.skipped_by_mode)
        self.assertIn("configuration_misuse", gaps.skipped_by_mode)
        # Path/sink/entry classes already audited in fast — not skipped.
        self.assertNotIn("sql_injection", gaps.skipped_by_mode)

    def test_fast_advice_mentions_deep_mode(self) -> None:
        gaps = compute_coverage_gaps(mode="fast")
        self.assertIn("audit.mode: deep", gaps.advice_to_user)


class DeepModeTaxonomyTests(unittest.TestCase):
    def test_deep_audited_is_superset_of_fast(self) -> None:
        fast = set(compute_coverage_gaps(mode="fast").audited_classes)
        deep = set(compute_coverage_gaps(mode="deep").audited_classes)
        self.assertTrue(fast.issubset(deep))
        # Deep adds the four deep-only classes.
        self.assertTrue(
            {
                "state_machine_violation",
                "toctou_race",
                "cross_process_taint",
                "configuration_misuse",
            }.issubset(deep)
        )

    def test_deep_skipped_by_mode_is_empty(self) -> None:
        gaps = compute_coverage_gaps(mode="deep")
        # Deep covers everything any mode covers — nothing left
        # to "skip by mode".
        self.assertEqual(gaps.skipped_by_mode, ())


class OutOfScopeAcrossModesTests(unittest.TestCase):
    def test_out_of_scope_identical_in_fast_and_deep(self) -> None:
        fast = compute_coverage_gaps(mode="fast").out_of_scope
        deep = compute_coverage_gaps(mode="deep").out_of_scope
        self.assertEqual(fast, deep)

    def test_out_of_scope_includes_supply_chain(self) -> None:
        gaps = compute_coverage_gaps(mode="fast")
        self.assertIn("supply_chain", gaps.out_of_scope)
        self.assertIn("business_logic_idor", gaps.out_of_scope)
        self.assertIn("cryptographic_primitives", gaps.out_of_scope)
        self.assertIn("unknown_unknowns", gaps.out_of_scope)


class OperatorOverrideTests(unittest.TestCase):
    def test_explicit_units_override_mode_preset(self) -> None:
        # Operator picks fast mode but adds the `config` unit.
        gaps = compute_coverage_gaps(
            mode="fast", units=("path", "sink", "entry", "config")
        )
        self.assertIn("configuration_misuse", gaps.audited_classes)
        # `configuration_misuse` no longer in skipped_by_mode now
        # that the operator opted in.
        self.assertNotIn("configuration_misuse", gaps.skipped_by_mode)

    def test_minimal_unit_set_audits_minimal_classes(self) -> None:
        gaps = compute_coverage_gaps(mode="fast", units=("path",))
        # Path-only — no sink_convergence, no entry_exposure.
        self.assertNotIn("sink_convergence", gaps.audited_classes)
        self.assertNotIn("entry_exposure", gaps.audited_classes)
        # And both of those land in skipped_by_mode (deep covers them).
        self.assertIn("sink_convergence", gaps.skipped_by_mode)
        self.assertIn("entry_exposure", gaps.skipped_by_mode)


class PayloadAndMarkdownProjectionTests(unittest.TestCase):
    def test_to_payload_returns_jsonb_compatible_dict(self) -> None:
        gaps = compute_coverage_gaps(mode="fast")
        payload = gaps.to_payload()
        # All five keys present.
        self.assertEqual(
            set(payload.keys()),
            {"audited_classes", "skipped_by_mode", "out_of_scope", "mode", "advice_to_user"},
        )
        # Lists, not tuples (JSONB-friendly).
        self.assertIsInstance(payload["audited_classes"], list)
        self.assertIsInstance(payload["skipped_by_mode"], list)
        self.assertIsInstance(payload["out_of_scope"], list)
        self.assertIsInstance(payload["mode"], str)
        self.assertIsInstance(payload["advice_to_user"], str)

    def test_to_markdown_section_renders_three_groups(self) -> None:
        gaps = compute_coverage_gaps(mode="fast")
        md = gaps.to_markdown_section()
        self.assertIn("## Coverage Gaps", md)
        self.assertIn("This audit covered:", md)
        self.assertIn("This audit did NOT cover", md)
        self.assertIn("Outside xauditor's scope", md)
        # Each audited class lands as a `- \`name\`` bullet.
        for cls in gaps.audited_classes:
            self.assertIn(f"`{cls}`", md)

    def test_deep_markdown_omits_skipped_section(self) -> None:
        gaps = compute_coverage_gaps(mode="deep")
        md = gaps.to_markdown_section()
        # Deep has no `skipped_by_mode`, so the "did NOT cover"
        # section is absent.
        self.assertNotIn("This audit did NOT cover", md)
        # The covered + out-of-scope sections still render.
        self.assertIn("This audit covered:", md)
        self.assertIn("Outside xauditor's scope", md)


if __name__ == "__main__":
    unittest.main()
