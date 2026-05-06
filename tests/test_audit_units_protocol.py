"""Tests for the `AuditUnit` Protocol seam (Phase 3A).

Covers `restructure-audit-modes-and-coverage` Phase 3 tasks 3.1.x,
3.4.x, and the planner-emission half of 3.2.1 / 3.5.1 / 3.5.2:

- All six unit-kind dataclasses satisfy the `AuditUnit` Protocol
  (runtime_checkable).
- `PathAuditUnit` wraps the historical `models.AuditUnit` and
  exposes the legacy `path` / `function_ids` attributes for
  backward compatibility with existing workflow code.
- `to_*_payload()` projections include the GraphSlice-shaped
  legacy keys so existing stage agents consume the new wrapper
  unchanged.
- `enumerate_units` returns only `PathAuditUnit` instances in
  Phase 3A regardless of the experimental flag (sink / entry /
  state / boundary / config enumeration is deferred to a
  graph-builder follow-up).
- The six new sink / entry stage prompt families
  (`analyzer_sink`, `validator_sink`, `exploiter_sink`,
  `analyzer_entry`, `validator_entry`, `exploiter_entry`)
  resolve via `get_prompt_definition`.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.context import GraphSlice
from xauditor.audit.units import (
    AuditUnit,
    BoundaryAuditUnit,
    ConfigAuditUnit,
    EntryAuditUnit,
    PathAuditUnit,
    SinkAuditUnit,
    StateAuditUnit,
    as_audit_unit,
)
from xauditor.models import AuditUnit as LegacyAuditUnit
from xauditor.models import PathRecord
from xauditor.prompts import get_prompt_definition


def _legacy_unit() -> LegacyAuditUnit:
    return LegacyAuditUnit(
        path=PathRecord(
            entry_function="main",
            function_names=("main", "handler"),
            file_paths=("app.py",),
            path_fingerprint="fp::test",
            function_ids=("fn::main", "fn::handler"),
        ),
        function_ids=("fn::main", "fn::handler"),
    )


def _empty_slice() -> GraphSlice:
    return GraphSlice(
        call_chain=(), function_definitions=(), referenced_symbols=()
    )


class AuditUnitProtocolConformanceTests(unittest.TestCase):
    """Every concrete unit kind is a runtime AuditUnit."""

    def test_path_audit_unit_satisfies_protocol(self) -> None:
        unit = as_audit_unit(_legacy_unit())
        self.assertIsInstance(unit, AuditUnit)
        self.assertEqual(unit.unit_kind, "path")
        self.assertEqual(unit.unit_id, "fp::test")
        self.assertEqual(unit.fingerprint(), "fp::test")

    def test_sink_audit_unit_satisfies_protocol(self) -> None:
        unit = SinkAuditUnit(sink_qualified_name="subprocess.run")
        self.assertIsInstance(unit, AuditUnit)
        self.assertEqual(unit.unit_kind, "sink")
        self.assertEqual(unit.unit_id, "sink::subprocess.run")

    def test_entry_audit_unit_satisfies_protocol(self) -> None:
        unit = EntryAuditUnit(
            entry_function_id="fn::main", entry_qualified_name="main"
        )
        self.assertIsInstance(unit, AuditUnit)
        self.assertEqual(unit.unit_kind, "entry")
        self.assertEqual(unit.unit_id, "entry::fn::main")

    def test_state_audit_unit_satisfies_protocol(self) -> None:
        unit = StateAuditUnit(class_id="cls::S", class_qualified_name="S")
        self.assertIsInstance(unit, AuditUnit)
        self.assertEqual(unit.unit_kind, "state")

    def test_boundary_audit_unit_satisfies_protocol(self) -> None:
        unit = BoundaryAuditUnit(
            boundary_kind="queue", producer_function_id="fn::publish"
        )
        self.assertIsInstance(unit, AuditUnit)
        self.assertEqual(unit.unit_kind, "boundary")

    def test_config_audit_unit_satisfies_protocol(self) -> None:
        unit = ConfigAuditUnit(config_source="config/values.yaml")
        self.assertIsInstance(unit, AuditUnit)
        self.assertEqual(unit.unit_kind, "config")


class PathAuditUnitBackwardCompatTests(unittest.TestCase):
    """PathAuditUnit preserves `unit.path.*` and `unit.function_ids`."""

    def test_path_attribute_returns_path_record(self) -> None:
        unit = as_audit_unit(_legacy_unit())
        self.assertEqual(unit.path.path_fingerprint, "fp::test")
        self.assertEqual(unit.path.entry_function, "main")
        self.assertEqual(unit.function_ids, ("fn::main", "fn::handler"))

    def test_to_analyzer_payload_includes_legacy_keys(self) -> None:
        unit = PathAuditUnit(
            legacy=_legacy_unit(),
            graph_slice=GraphSlice(
                call_chain=({"function_id": "fn::main"},),
                function_definitions=({"function_id": "fn::main"},),
                referenced_symbols=(),
            ),
        )
        payload = unit.to_analyzer_payload()
        # Legacy three keys still surface at the top level.
        self.assertIn("call_chain", payload)
        self.assertIn("function_definitions", payload)
        self.assertIn("referenced_symbols", payload)
        # Phase-2 graph_slice sub-key still surfaces too.
        self.assertIn("graph_slice", payload)
        # Path-specific framing.
        self.assertEqual(payload["entry_function"], "main")
        self.assertEqual(
            payload["function_names"], ["main", "handler"]
        )

    def test_to_validator_and_exploiter_payloads_share_path_framing(self) -> None:
        unit = as_audit_unit(_legacy_unit())
        for payload in (
            unit.to_validator_payload(),
            unit.to_exploiter_payload(),
        ):
            self.assertEqual(payload["entry_function"], "main")
            self.assertIn("call_chain", payload)


class SinkAndEntryUnitPayloadShapeTests(unittest.TestCase):
    """Sink/Entry shells produce shape-stable payloads."""

    def test_sink_payload_contains_inbound_path_framing(self) -> None:
        sink = SinkAuditUnit(
            sink_qualified_name="db.execute",
            inbound_paths=(
                PathRecord(
                    entry_function="handler_a",
                    function_names=("handler_a", "db.execute"),
                    file_paths=("app.py",),
                    path_fingerprint="fp::a",
                    function_ids=("fn::a",),
                ),
                PathRecord(
                    entry_function="handler_b",
                    function_names=("handler_b", "db.execute"),
                    file_paths=("app.py",),
                    path_fingerprint="fp::b",
                    function_ids=("fn::b",),
                ),
            ),
            graph_slice=_empty_slice(),
        )
        payload = sink.to_analyzer_payload()
        self.assertEqual(payload["sink_qualified_name"], "db.execute")
        self.assertEqual(payload["inbound_path_count"], 2)
        self.assertEqual(len(payload["inbound_paths"]), 2)
        self.assertEqual(
            payload["inbound_paths"][0]["entry_function"], "handler_a"
        )

    def test_entry_payload_contains_downstream_function_ids(self) -> None:
        entry = EntryAuditUnit(
            entry_function_id="fn::admin_users",
            entry_qualified_name="admin_users",
            downstream_function_ids=("fn::list_admins", "fn::db.query"),
            graph_slice=_empty_slice(),
        )
        payload = entry.to_analyzer_payload()
        self.assertEqual(payload["entry_function_id"], "fn::admin_users")
        self.assertEqual(
            payload["downstream_function_ids"],
            ["fn::list_admins", "fn::db.query"],
        )


class FingerprintUniquenessTests(unittest.TestCase):
    """Each unit kind's fingerprint is unique in its own namespace."""

    def test_path_sink_entry_fingerprints_are_distinct_namespaces(self) -> None:
        path_unit = as_audit_unit(_legacy_unit())
        sink_unit = SinkAuditUnit(sink_qualified_name="fp::test")
        entry_unit = EntryAuditUnit(
            entry_function_id="fp::test", entry_qualified_name="x"
        )
        # Even with identical anchor strings, the unit-kind prefix
        # keeps fingerprints distinct so the reconciler doesn't
        # falsely group them.
        fingerprints = {
            path_unit.fingerprint(),
            sink_unit.fingerprint(),
            entry_unit.fingerprint(),
        }
        self.assertEqual(len(fingerprints), 3)


class PromptFamilyResolutionTests(unittest.TestCase):
    """Six new sink / entry stage prompts resolve via `get_prompt_definition`."""

    def test_sink_prompt_family_resolves(self) -> None:
        for name in ("analyzer_sink", "validator_sink", "exploiter_sink"):
            with self.subTest(name=name):
                spec = get_prompt_definition(name)
                self.assertEqual(spec.version, "v1")
                self.assertGreater(len(spec.system), 100)

    def test_entry_prompt_family_resolves(self) -> None:
        for name in ("analyzer_entry", "validator_entry", "exploiter_entry"):
            with self.subTest(name=name):
                spec = get_prompt_definition(name)
                self.assertEqual(spec.version, "v1")
                self.assertGreater(len(spec.system), 100)

    def test_path_kind_prompts_unchanged(self) -> None:
        # Sanity: the existing path-kind prompts (no `_path` suffix
        # because the historical names already imply path-based
        # auditing) still resolve at their established versions.
        self.assertEqual(get_prompt_definition("analyzer").version, "v4")
        self.assertEqual(get_prompt_definition("validator").version, "v3")
        self.assertEqual(get_prompt_definition("exploitation").version, "v3")


class EnumerateUnitsTests(unittest.TestCase):
    """`enumerate_units` returns Path-only AuditUnit instances in Phase 3A."""

    def test_enumerate_units_returns_path_audit_units(self) -> None:
        # Lightweight stub source: yields one path.
        class _StubSource:
            def iter_paths(self):
                yield PathRecord(
                    entry_function="main",
                    function_names=("main",),
                    file_paths=("app.py",),
                    path_fingerprint="fp::stub-1",
                    function_ids=("fn::main",),
                    business_context="ctx",
                    trust_boundary="boundary",
                )

        from xauditor.audit.planner import enumerate_units

        units = enumerate_units(_StubSource(), audit_mode=None)
        self.assertEqual(len(units), 1)
        self.assertIsInstance(units[0], PathAuditUnit)
        self.assertIsInstance(units[0], AuditUnit)
        self.assertEqual(units[0].unit_kind, "path")
        self.assertEqual(units[0].fingerprint(), "fp::stub-1")


if __name__ == "__main__":
    unittest.main()
