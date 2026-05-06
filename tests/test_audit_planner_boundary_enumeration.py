"""Tests for `enumerate_units` BoundaryAuditUnit emission.

`capture-decorators-and-registrations` Commit E. A
BoundaryAuditUnit emerges when a planned path passes through a
Function whose `sink_kind ∈ {http_client, subprocess}`. The
producer is the in-process caller (function preceding the sink
in the path); the consumer is the sink stub. Gated on
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
)
from xauditor.config import AuditExperimentalConfig, AuditModeConfig
from xauditor.models import (
    DecoratorRecord,
    FunctionRecord,
    PathRecord,
    RegistrationSiteRecord,
)


def _fn(
    *,
    function_id: str,
    qualified_name: str,
    is_external: bool = False,
    is_sink: bool = False,
    sink_kind: str = "",
) -> FunctionRecord:
    return FunctionRecord(
        function_id=function_id,
        name=qualified_name.rsplit(".", 1)[-1],
        qualified_name=qualified_name,
        file_path="src/" + qualified_name.replace(".", "/") + ".py",
        module_name=qualified_name.rsplit(".", 1)[0] if "." in qualified_name else "",
        start_line=1,
        end_line=10,
        source="def f(): pass",
        is_external=is_external,
        is_well_known_sink=is_sink,
        sink_kind=sink_kind,
    )


def _path(
    *,
    fingerprint: str,
    entry: str,
    function_ids: tuple[str, ...],
    function_names: tuple[str, ...] | None = None,
) -> PathRecord:
    names = function_names or (entry,)
    return PathRecord(
        entry_function=entry,
        function_names=names,
        file_paths=tuple(f"src/{n}.py" for n in names),
        path_fingerprint=fingerprint,
        function_ids=function_ids,
    )


class _StubGraphSource:
    """Minimal AuditGraphSource for boundary planner tests."""

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


def _enable_units() -> AuditModeConfig:
    return AuditModeConfig(experimental=AuditExperimentalConfig(units=True))


class FlagOffEmitsNoBoundaryUnitsTests(unittest.TestCase):
    def test_default_audit_mode_emits_no_boundary_units(self) -> None:
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="run_cmd",
                    function_ids=("fn::run_cmd", "external::subprocess.run"),
                ),
            ],
            functions=[
                _fn(function_id="fn::run_cmd", qualified_name="myapp.run_cmd"),
                _fn(
                    function_id="external::subprocess.run",
                    qualified_name="subprocess.run",
                    is_external=True,
                    is_sink=True,
                    sink_kind="subprocess",
                ),
            ],
        )
        units = enumerate_units(source, audit_mode=AuditModeConfig())
        self.assertEqual([u for u in units if isinstance(u, BoundaryAuditUnit)], [])


class FlagOnEmitsBoundaryUnitsTests(unittest.TestCase):
    def test_subprocess_sink_emits_subprocess_boundary(self) -> None:
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="run_cmd",
                    function_ids=("fn::run_cmd", "external::subprocess.run"),
                ),
            ],
            functions=[
                _fn(function_id="fn::run_cmd", qualified_name="myapp.run_cmd"),
                _fn(
                    function_id="external::subprocess.run",
                    qualified_name="subprocess.run",
                    is_external=True,
                    is_sink=True,
                    sink_kind="subprocess",
                ),
            ],
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        boundaries = [u for u in units if isinstance(u, BoundaryAuditUnit)]
        self.assertEqual(len(boundaries), 1)
        self.assertEqual(boundaries[0].boundary_kind, "subprocess")
        self.assertEqual(boundaries[0].producer_function_id, "fn::run_cmd")
        self.assertEqual(
            boundaries[0].consumer_function_ids, ("external::subprocess.run",)
        )

    def test_http_client_sink_emits_rpc_boundary(self) -> None:
        # http_client maps to "rpc" boundary kind — every HTTP
        # request crosses into a remote RPC equivalent.
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="fetch_user",
                    function_ids=("fn::fetch_user", "external::requests.get"),
                ),
            ],
            functions=[
                _fn(function_id="fn::fetch_user", qualified_name="myapp.fetch_user"),
                _fn(
                    function_id="external::requests.get",
                    qualified_name="requests.get",
                    is_external=True,
                    is_sink=True,
                    sink_kind="http_client",
                ),
            ],
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        boundaries = [u for u in units if isinstance(u, BoundaryAuditUnit)]
        self.assertEqual(len(boundaries), 1)
        self.assertEqual(boundaries[0].boundary_kind, "rpc")
        self.assertEqual(boundaries[0].producer_function_id, "fn::fetch_user")

    def test_non_boundary_sink_kind_skipped(self) -> None:
        # `pickle.loads` is `is_well_known_sink=True` but its
        # sink_kind="deserializer" is NOT a boundary kind. Skip.
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="load",
                    function_ids=("fn::load", "external::pickle.loads"),
                ),
            ],
            functions=[
                _fn(function_id="fn::load", qualified_name="myapp.load"),
                _fn(
                    function_id="external::pickle.loads",
                    qualified_name="pickle.loads",
                    is_external=True,
                    is_sink=True,
                    sink_kind="deserializer",
                ),
            ],
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        self.assertEqual([u for u in units if isinstance(u, BoundaryAuditUnit)], [])

    def test_producer_is_function_preceding_sink_in_chain(self) -> None:
        # 3-hop path: entry → mid → subprocess.run. Producer
        # should be `mid`, not `entry` — the in-process function
        # actually wiring data into the boundary call.
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="handle",
                    function_ids=(
                        "fn::handle",
                        "fn::sanitize",
                        "external::subprocess.run",
                    ),
                    function_names=("handle", "sanitize"),
                ),
            ],
            functions=[
                _fn(function_id="fn::handle", qualified_name="myapp.handle"),
                _fn(function_id="fn::sanitize", qualified_name="myapp.sanitize"),
                _fn(
                    function_id="external::subprocess.run",
                    qualified_name="subprocess.run",
                    is_external=True,
                    is_sink=True,
                    sink_kind="subprocess",
                ),
            ],
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        boundaries = [u for u in units if isinstance(u, BoundaryAuditUnit)]
        self.assertEqual(len(boundaries), 1)
        self.assertEqual(boundaries[0].producer_function_id, "fn::sanitize")

    def test_dedup_collapses_repeated_producer_consumer_pair(self) -> None:
        # Two paths share the same producer→consumer pair (entry
        # → subprocess.run vs different_entry → subprocess.run
        # with same caller). Only the first emits.
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="route_a",
                    function_ids=("fn::caller", "external::subprocess.run"),
                ),
                _path(
                    fingerprint="fp::2",
                    entry="route_b",
                    function_ids=("fn::caller", "external::subprocess.run"),
                ),
            ],
            functions=[
                _fn(function_id="fn::caller", qualified_name="myapp.caller"),
                _fn(
                    function_id="external::subprocess.run",
                    qualified_name="subprocess.run",
                    is_external=True,
                    is_sink=True,
                    sink_kind="subprocess",
                ),
            ],
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        boundaries = [u for u in units if isinstance(u, BoundaryAuditUnit)]
        self.assertEqual(len(boundaries), 1)

    def test_two_boundary_kinds_in_same_path_emits_two_units(self) -> None:
        # entry → http_client_call → subprocess_call: two
        # distinct boundaries (rpc + subprocess), two units.
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="webhook",
                    function_ids=(
                        "fn::webhook",
                        "external::requests.get",
                        "external::subprocess.run",
                    ),
                ),
            ],
            functions=[
                _fn(function_id="fn::webhook", qualified_name="myapp.webhook"),
                _fn(
                    function_id="external::requests.get",
                    qualified_name="requests.get",
                    is_external=True,
                    is_sink=True,
                    sink_kind="http_client",
                ),
                _fn(
                    function_id="external::subprocess.run",
                    qualified_name="subprocess.run",
                    is_external=True,
                    is_sink=True,
                    sink_kind="subprocess",
                ),
            ],
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        boundaries = [u for u in units if isinstance(u, BoundaryAuditUnit)]
        self.assertEqual(len(boundaries), 2)
        kinds = {b.boundary_kind for b in boundaries}
        self.assertEqual(kinds, {"rpc", "subprocess"})

    def test_no_boundary_sinks_in_graph_emits_zero_units(self) -> None:
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="handle",
                    function_ids=("fn::handle",),
                ),
            ],
            functions=[_fn(function_id="fn::handle", qualified_name="myapp.handle")],
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        self.assertEqual([u for u in units if isinstance(u, BoundaryAuditUnit)], [])

    def test_path_without_boundary_sink_emits_zero_units(self) -> None:
        # The graph HAS a boundary sink, but no planned path
        # reaches it. Skip.
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="handle",
                    function_ids=("fn::handle",),
                ),
            ],
            functions=[
                _fn(function_id="fn::handle", qualified_name="myapp.handle"),
                _fn(
                    function_id="external::subprocess.run",
                    qualified_name="subprocess.run",
                    is_external=True,
                    is_sink=True,
                    sink_kind="subprocess",
                ),
            ],
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        self.assertEqual([u for u in units if isinstance(u, BoundaryAuditUnit)], [])

    def test_boundary_units_coexist_with_path_sink_entry(self) -> None:
        # All four kinds emit together when applicable.
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="run_cmd",
                    function_ids=("fn::run_cmd", "external::subprocess.run"),
                ),
            ],
            functions=[
                _fn(function_id="fn::run_cmd", qualified_name="myapp.run_cmd"),
                _fn(
                    function_id="external::subprocess.run",
                    qualified_name="subprocess.run",
                    is_external=True,
                    is_sink=True,
                    sink_kind="subprocess",
                ),
            ],
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        kinds = {type(u) for u in units}
        # Path + Sink + Boundary; no Entry (no decorators).
        self.assertIn(PathAuditUnit, kinds)
        self.assertIn(SinkAuditUnit, kinds)
        self.assertIn(BoundaryAuditUnit, kinds)
        self.assertNotIn(EntryAuditUnit, kinds)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
