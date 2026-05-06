"""Tests for `enumerate_units` StateAuditUnit emission.

`capture-decorators-and-registrations` Commit F. A
StateAuditUnit emerges for each Class whose methods include
`>= 2` with `mutates_self=True`. Gated on
`audit.experimental.units: true`.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Iterator, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.planner import enumerate_units
from xauditor.audit.units import (
    BoundaryAuditUnit,
    EntryAuditUnit,
    PathAuditUnit,
    SinkAuditUnit,
    StateAuditUnit,
)
from xauditor.config import AuditExperimentalConfig, AuditModeConfig
from xauditor.models import (
    ClassRecord,
    FunctionRecord,
    PathRecord,
)


def _fn(
    *,
    function_id: str,
    qualified_name: str,
    class_id: str | None = None,
    mutates_self: bool = False,
    is_external: bool = False,
) -> FunctionRecord:
    return FunctionRecord(
        function_id=function_id,
        name=qualified_name.rsplit(".", 1)[-1],
        qualified_name=qualified_name,
        file_path="src/x.py",
        module_name=qualified_name.rsplit(".", 1)[0] if "." in qualified_name else "",
        start_line=1,
        end_line=10,
        source="def f(): pass",
        class_id=class_id,
        mutates_self=mutates_self,
        is_external=is_external,
    )


def _cls(
    *, class_id: str, name: str, module_name: str = "myapp"
) -> ClassRecord:
    return ClassRecord(
        class_id=class_id,
        name=name,
        file_path="src/x.py",
        module_name=module_name,
        start_line=1,
        end_line=20,
    )


def _path_record(
    *, fingerprint: str, entry: str, function_ids: tuple[str, ...]
) -> PathRecord:
    return PathRecord(
        entry_function=entry,
        function_names=(entry,),
        file_paths=("src/x.py",),
        path_fingerprint=fingerprint,
        function_ids=function_ids,
    )


class _StubGraphSource:
    build_fingerprint = "bf::test"
    repo_root = Path("/tmp/x")

    def __init__(
        self,
        *,
        paths: list[PathRecord],
        functions: list[FunctionRecord],
        classes: list[ClassRecord] | None = None,
    ) -> None:
        self._paths = paths
        self._functions = functions
        self._classes = classes or []

    def iter_paths(self, *, page_size: int = 100) -> Iterator[PathRecord]:
        del page_size
        yield from self._paths

    def path_count(self) -> int:
        return len(self._paths)

    def iter_functions(self, *, page_size: int = 1000) -> Iterator[FunctionRecord]:
        del page_size
        yield from self._functions

    def fetch_decorators_for(self, function_ids: Sequence[str]):
        return {}

    def fetch_registrations_for(self, function_ids: Sequence[str]):
        return {}

    def load_path_functions(self, function_ids):
        return []

    def load_path_symbols(self, function_ids):
        return ((), ())

    def build_coverage(self, *, plan, skipped_paths, failed_paths=frozenset()):
        raise NotImplementedError

    def iter_classes(self, *, page_size=1000):
        return iter(self._classes)

    def iter_edges(self, *, page_size=1000):
        return iter([])

    def iter_module_symbols(self, *, page_size=1000):
        return iter([])

    def lookup_function_by_id(self, function_id):
        return next(
            (fn for fn in self._functions if fn.function_id == function_id), None
        )

    def lookup_class_by_id(self, class_id):
        return next(
            (cls for cls in self._classes if cls.class_id == class_id), None
        )


def _enable_units() -> AuditModeConfig:
    return AuditModeConfig(experimental=AuditExperimentalConfig(units=True))


class FlagOffEmitsNoStateUnitsTests(unittest.TestCase):
    def test_default_audit_mode_emits_no_state_units(self) -> None:
        source = _StubGraphSource(
            paths=[],
            functions=[
                _fn(
                    function_id="fn::a",
                    qualified_name="myapp.S.a",
                    class_id="cls::S",
                    mutates_self=True,
                ),
                _fn(
                    function_id="fn::b",
                    qualified_name="myapp.S.b",
                    class_id="cls::S",
                    mutates_self=True,
                ),
            ],
            classes=[_cls(class_id="cls::S", name="S")],
        )
        units = enumerate_units(source, audit_mode=AuditModeConfig())
        self.assertEqual([u for u in units if isinstance(u, StateAuditUnit)], [])


class FlagOnEmitsStateUnitsTests(unittest.TestCase):
    def test_class_with_two_mutating_methods_emits_one_unit(self) -> None:
        source = _StubGraphSource(
            paths=[],
            functions=[
                _fn(
                    function_id="fn::a",
                    qualified_name="myapp.Counter.inc",
                    class_id="cls::Counter",
                    mutates_self=True,
                ),
                _fn(
                    function_id="fn::b",
                    qualified_name="myapp.Counter.reset",
                    class_id="cls::Counter",
                    mutates_self=True,
                ),
            ],
            classes=[_cls(class_id="cls::Counter", name="Counter")],
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        state_units = [u for u in units if isinstance(u, StateAuditUnit)]
        self.assertEqual(len(state_units), 1)
        self.assertEqual(state_units[0].class_id, "cls::Counter")
        self.assertEqual(state_units[0].class_qualified_name, "myapp.Counter")
        self.assertEqual(set(state_units[0].method_ids), {"fn::a", "fn::b"})

    def test_class_with_three_mutating_methods_groups_all(self) -> None:
        source = _StubGraphSource(
            paths=[],
            functions=[
                _fn(
                    function_id=f"fn::{name}",
                    qualified_name=f"myapp.S.{name}",
                    class_id="cls::S",
                    mutates_self=True,
                )
                for name in ("a", "b", "c")
            ],
            classes=[_cls(class_id="cls::S", name="S")],
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        state_units = [u for u in units if isinstance(u, StateAuditUnit)]
        self.assertEqual(len(state_units), 1)
        self.assertEqual(set(state_units[0].method_ids), {"fn::a", "fn::b", "fn::c"})

    def test_class_with_one_mutating_method_does_not_emit(self) -> None:
        # Threshold = 2. A single mutating method is not a
        # "shared mutable state" anchor on its own.
        source = _StubGraphSource(
            paths=[],
            functions=[
                _fn(
                    function_id="fn::a",
                    qualified_name="myapp.S.a",
                    class_id="cls::S",
                    mutates_self=True,
                ),
                _fn(
                    function_id="fn::b",
                    qualified_name="myapp.S.b",
                    class_id="cls::S",
                    mutates_self=False,
                ),
            ],
            classes=[_cls(class_id="cls::S", name="S")],
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        self.assertEqual([u for u in units if isinstance(u, StateAuditUnit)], [])

    def test_class_with_zero_mutating_methods_does_not_emit(self) -> None:
        source = _StubGraphSource(
            paths=[],
            functions=[
                _fn(
                    function_id="fn::a",
                    qualified_name="myapp.S.a",
                    class_id="cls::S",
                    mutates_self=False,
                ),
                _fn(
                    function_id="fn::b",
                    qualified_name="myapp.S.b",
                    class_id="cls::S",
                    mutates_self=False,
                ),
            ],
            classes=[_cls(class_id="cls::S", name="S")],
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        self.assertEqual([u for u in units if isinstance(u, StateAuditUnit)], [])

    def test_module_level_function_with_mutates_self_ignored(self) -> None:
        # `class_id is None` (module-level fn) is skipped — even
        # if the mutates_self flag is True, there's no class to
        # anchor to.
        source = _StubGraphSource(
            paths=[],
            functions=[
                _fn(
                    function_id="fn::a",
                    qualified_name="myapp.helper",
                    class_id=None,
                    mutates_self=True,
                ),
                _fn(
                    function_id="fn::b",
                    qualified_name="myapp.helper2",
                    class_id=None,
                    mutates_self=True,
                ),
            ],
            classes=[],
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        self.assertEqual([u for u in units if isinstance(u, StateAuditUnit)], [])

    def test_external_stub_function_skipped(self) -> None:
        source = _StubGraphSource(
            paths=[],
            functions=[
                _fn(
                    function_id="external::A.a",
                    qualified_name="A.a",
                    class_id="cls::A",
                    mutates_self=True,
                    is_external=True,
                ),
                _fn(
                    function_id="external::A.b",
                    qualified_name="A.b",
                    class_id="cls::A",
                    mutates_self=True,
                    is_external=True,
                ),
            ],
            classes=[_cls(class_id="cls::A", name="A")],
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        self.assertEqual([u for u in units if isinstance(u, StateAuditUnit)], [])

    def test_multiple_classes_only_qualifying_emit(self) -> None:
        source = _StubGraphSource(
            paths=[],
            functions=[
                # Class A: 2 mutators → emits.
                _fn(
                    function_id="fn::a1",
                    qualified_name="myapp.A.x",
                    class_id="cls::A",
                    mutates_self=True,
                ),
                _fn(
                    function_id="fn::a2",
                    qualified_name="myapp.A.y",
                    class_id="cls::A",
                    mutates_self=True,
                ),
                # Class B: 1 mutator → skip.
                _fn(
                    function_id="fn::b1",
                    qualified_name="myapp.B.x",
                    class_id="cls::B",
                    mutates_self=True,
                ),
                # Class C: 0 mutators (only readers) → skip.
                _fn(
                    function_id="fn::c1",
                    qualified_name="myapp.C.x",
                    class_id="cls::C",
                    mutates_self=False,
                ),
                _fn(
                    function_id="fn::c2",
                    qualified_name="myapp.C.y",
                    class_id="cls::C",
                    mutates_self=False,
                ),
            ],
            classes=[
                _cls(class_id="cls::A", name="A"),
                _cls(class_id="cls::B", name="B"),
                _cls(class_id="cls::C", name="C"),
            ],
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        state_units = [u for u in units if isinstance(u, StateAuditUnit)]
        self.assertEqual(len(state_units), 1)
        self.assertEqual(state_units[0].class_id, "cls::A")

    def test_class_record_missing_falls_back_to_qualified_name(self) -> None:
        # If iter_classes / lookup_class_by_id doesn't find the
        # class (e.g. the class node is missing for some reason),
        # derive the qualified name from a method's qualified
        # name minus the trailing method segment.
        source = _StubGraphSource(
            paths=[],
            functions=[
                _fn(
                    function_id="fn::a",
                    qualified_name="myapp.Orphan.x",
                    class_id="cls::Orphan",
                    mutates_self=True,
                ),
                _fn(
                    function_id="fn::b",
                    qualified_name="myapp.Orphan.y",
                    class_id="cls::Orphan",
                    mutates_self=True,
                ),
            ],
            classes=[],  # no class record present
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        state_units = [u for u in units if isinstance(u, StateAuditUnit)]
        self.assertEqual(len(state_units), 1)
        self.assertEqual(state_units[0].class_qualified_name, "myapp.Orphan")

    def test_state_units_coexist_with_path_kind(self) -> None:
        source = _StubGraphSource(
            paths=[
                _path_record(
                    fingerprint="fp::1",
                    entry="handler",
                    function_ids=("fn::handler",),
                ),
            ],
            functions=[
                _fn(
                    function_id="fn::handler",
                    qualified_name="myapp.handler",
                    class_id=None,
                ),
                _fn(
                    function_id="fn::a",
                    qualified_name="myapp.S.a",
                    class_id="cls::S",
                    mutates_self=True,
                ),
                _fn(
                    function_id="fn::b",
                    qualified_name="myapp.S.b",
                    class_id="cls::S",
                    mutates_self=True,
                ),
            ],
            classes=[_cls(class_id="cls::S", name="S")],
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        kinds = {type(u) for u in units}
        self.assertIn(PathAuditUnit, kinds)
        self.assertIn(StateAuditUnit, kinds)
        # No sinks/entries/boundaries in this fixture.
        self.assertNotIn(SinkAuditUnit, kinds)
        self.assertNotIn(EntryAuditUnit, kinds)
        self.assertNotIn(BoundaryAuditUnit, kinds)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
