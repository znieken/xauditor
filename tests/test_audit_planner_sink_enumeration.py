"""Tests for `xauditor.audit.planner.enumerate_units` SinkAuditUnit emission.

Covers `capture-decorators-and-registrations` Commit 3:

- `enumerate_units(source, audit_mode)` emits PathAuditUnit per
  planned path (existing baseline) PLUS one SinkAuditUnit per
  `is_well_known_sink: true` Function node when
  `audit.experimental.units: true`.
- The flag is OFF by default → only PathAuditUnits emitted (no
  behaviour change for existing operators).
- SinkAuditUnits filter `inbound_paths` to those that reach the
  sink (function_id appears in the path's function_ids).
- Sinks with no inbound planned paths are skipped (nothing for
  the analyzer to audit on them).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.planner import enumerate_units
from xauditor.audit.units import PathAuditUnit, SinkAuditUnit
from xauditor.config import AuditExperimentalConfig, AuditModeConfig
from xauditor.models import (
    AuditPlan,
    ClassRecord,
    EdgeRecord,
    FunctionRecord,
    ModuleSymbolRecord,
    PathRecord,
)


def _fn(*, function_id: str, qualified_name: str, is_sink: bool = False, sink_kind: str = "") -> FunctionRecord:
    return FunctionRecord(
        function_id=function_id,
        name=qualified_name.rsplit(".", 1)[-1],
        qualified_name=qualified_name,
        file_path="src/" + qualified_name.replace(".", "/") + ".py",
        module_name=qualified_name.rsplit(".", 1)[0] if "." in qualified_name else "",
        start_line=1,
        end_line=10,
        source="def f(): pass",
        is_well_known_sink=is_sink,
        sink_kind=sink_kind,
    )


def _path(
    *,
    fingerprint: str,
    entry: str,
    function_ids: tuple[str, ...],
    function_names: tuple[str, ...],
) -> PathRecord:
    return PathRecord(
        entry_function=entry,
        function_names=function_names,
        file_paths=tuple(f"src/{name}.py" for name in function_names),
        path_fingerprint=fingerprint,
        function_ids=function_ids,
    )


class _StubGraphSource:
    """Minimal `AuditGraphSource` stand-in for planner tests.

    The real Protocol has many methods; we implement only the
    surface enumerate_units actually consumes
    (`iter_paths` + `iter_functions`).
    """

    build_fingerprint = "bf::test"
    repo_root = Path("/tmp/x")

    def __init__(
        self,
        *,
        paths: list[PathRecord],
        functions: list[FunctionRecord],
    ) -> None:
        self._paths = paths
        self._functions = functions

    def iter_paths(self, *, page_size: int = 100) -> Iterator[PathRecord]:
        del page_size
        yield from self._paths

    def path_count(self) -> int:
        return len(self._paths)

    def iter_functions(self, *, page_size: int = 1000) -> Iterator[FunctionRecord]:
        del page_size
        yield from self._functions

    def fetch_decorators_for(self, function_ids):
        return {}

    def fetch_registrations_for(self, function_ids):
        return {}

    # Stubs for the rest of the Protocol — never called by these tests.
    def load_path_functions(self, function_ids):
        return []

    def load_path_symbols(self, function_ids):
        return ((), ())

    def build_coverage(self, *, plan, skipped_paths, failed_paths=frozenset()):
        raise NotImplementedError

    def iter_classes(self, *, page_size=1000):
        return iter([])

    def iter_edges(self, *, page_size=1000):
        return iter([])

    def iter_module_symbols(self, *, page_size=1000):
        return iter([])

    def lookup_function_by_id(self, function_id):
        return next(
            (fn for fn in self._functions if fn.function_id == function_id), None
        )

    def lookup_class_by_id(self, class_id):
        return None


class FlagOffEmitsOnlyPathsTests(unittest.TestCase):
    """Default behaviour — `audit.experimental.units: false` (off)
    keeps today's path-only enumeration. No SinkAuditUnits.
    """

    def test_default_audit_mode_emits_only_paths(self) -> None:
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="handle",
                    function_ids=("fn::handle", "external::subprocess.run"),
                    function_names=("handle",),
                ),
            ],
            functions=[
                _fn(function_id="fn::handle", qualified_name="myapp.handle"),
                _fn(
                    function_id="external::subprocess.run",
                    qualified_name="subprocess.run",
                    is_sink=True,
                    sink_kind="subprocess",
                ),
            ],
        )

        units = enumerate_units(source, audit_mode=AuditModeConfig())

        # Only the PathAuditUnit; no SinkAuditUnit because the flag
        # is off (default).
        self.assertEqual(len(units), 1)
        self.assertIsInstance(units[0], PathAuditUnit)

    def test_audit_mode_none_also_emits_only_paths(self) -> None:
        # Defensive: callers that forget audit_mode shouldn't see
        # the flag accidentally treated as on.
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="handle",
                    function_ids=("fn::handle",),
                    function_names=("handle",),
                ),
            ],
            functions=[_fn(function_id="fn::handle", qualified_name="myapp.handle")],
        )
        units = enumerate_units(source, audit_mode=None)
        self.assertEqual(len(units), 1)


class FlagOnEmitsSinkUnitsTests(unittest.TestCase):
    """`audit.experimental.units: true` adds SinkAuditUnits."""

    def _enable_units_mode(self) -> AuditModeConfig:
        return AuditModeConfig(
            experimental=AuditExperimentalConfig(units=True)
        )

    def test_emits_one_sink_unit_per_labelled_function(self) -> None:
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="handle",
                    function_ids=("fn::handle", "external::subprocess.run"),
                    function_names=("handle",),
                ),
                _path(
                    fingerprint="fp::2",
                    entry="cleanup",
                    function_ids=("fn::cleanup", "external::os.system"),
                    function_names=("cleanup",),
                ),
            ],
            functions=[
                _fn(function_id="fn::handle", qualified_name="myapp.handle"),
                _fn(function_id="fn::cleanup", qualified_name="myapp.cleanup"),
                _fn(
                    function_id="external::subprocess.run",
                    qualified_name="subprocess.run",
                    is_sink=True,
                    sink_kind="subprocess",
                ),
                _fn(
                    function_id="external::os.system",
                    qualified_name="os.system",
                    is_sink=True,
                    sink_kind="subprocess",
                ),
            ],
        )

        units = enumerate_units(source, audit_mode=self._enable_units_mode())

        # 2 PathAuditUnits + 2 SinkAuditUnits emerge here. Other
        # kinds (boundary etc.) may also emerge for these
        # subprocess sinks once boundary enumeration is wired —
        # those are out of scope for this test, so check sinks
        # specifically.
        path_units = [u for u in units if isinstance(u, PathAuditUnit)]
        sink_units = [u for u in units if isinstance(u, SinkAuditUnit)]
        self.assertEqual(len(path_units), 2)
        self.assertEqual(len(sink_units), 2)
        sink_fqns = {u.sink_qualified_name for u in sink_units}
        self.assertEqual(sink_fqns, {"subprocess.run", "os.system"})

    def test_sink_unit_carries_only_inbound_paths(self) -> None:
        # Two paths, but only one reaches the sink.
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="handle",
                    function_ids=("fn::handle", "external::subprocess.run"),
                    function_names=("handle",),
                ),
                _path(
                    fingerprint="fp::2",
                    entry="other",
                    function_ids=("fn::other",),  # no sink
                    function_names=("other",),
                ),
            ],
            functions=[
                _fn(function_id="fn::handle", qualified_name="myapp.handle"),
                _fn(function_id="fn::other", qualified_name="myapp.other"),
                _fn(
                    function_id="external::subprocess.run",
                    qualified_name="subprocess.run",
                    is_sink=True,
                    sink_kind="subprocess",
                ),
            ],
        )

        units = enumerate_units(source, audit_mode=self._enable_units_mode())
        sink_unit = next(u for u in units if isinstance(u, SinkAuditUnit))
        # Only the path reaching the sink is in inbound_paths.
        self.assertEqual(len(sink_unit.inbound_paths), 1)
        self.assertEqual(sink_unit.inbound_paths[0].path_fingerprint, "fp::1")

    def test_sink_with_no_inbound_paths_is_skipped(self) -> None:
        # The graph has a sink Function node but no planned path
        # reaches it (e.g. dead code that calls the sink). Skip
        # — nothing for the analyzer to audit on this unit.
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="handle",
                    function_ids=("fn::handle",),
                    function_names=("handle",),
                ),
            ],
            functions=[
                _fn(function_id="fn::handle", qualified_name="myapp.handle"),
                _fn(
                    function_id="external::pickle.loads",
                    qualified_name="pickle.loads",
                    is_sink=True,
                    sink_kind="deserializer",
                ),
            ],
        )

        units = enumerate_units(source, audit_mode=self._enable_units_mode())
        # Path unit only; no SinkAuditUnit for the unreached sink.
        sink_units = [u for u in units if isinstance(u, SinkAuditUnit)]
        self.assertEqual(sink_units, [])

    def test_no_sink_labelled_functions_emits_zero_sink_units(self) -> None:
        # Pre-Commit-2 graphs (no sink labels) → no SinkAuditUnits
        # even with the flag on.
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="handle",
                    function_ids=("fn::handle",),
                    function_names=("handle",),
                ),
            ],
            functions=[_fn(function_id="fn::handle", qualified_name="myapp.handle")],
        )

        units = enumerate_units(source, audit_mode=self._enable_units_mode())
        self.assertEqual([u for u in units if isinstance(u, SinkAuditUnit)], [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
