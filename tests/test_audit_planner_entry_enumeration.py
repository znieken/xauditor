"""Tests for `enumerate_units` EntryAuditUnit emission.

`capture-decorators-and-registrations` Commit D. An EntryAuditUnit
emerges when a parsed function has a decorator with intent ∈
{route, task_handler, cli_entry} OR has an inbound REGISTERS edge.
Gated on `audit.experimental.units: true`.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Iterator, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.planner import enumerate_units
from xauditor.audit.units import EntryAuditUnit, PathAuditUnit, SinkAuditUnit
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
    )


def _path(
    *, fingerprint: str, entry: str, function_ids: tuple[str, ...]
) -> PathRecord:
    return PathRecord(
        entry_function=entry,
        function_names=(entry,),
        file_paths=(f"src/{entry}.py",),
        path_fingerprint=fingerprint,
        function_ids=function_ids,
    )


class _StubGraphSource:
    """Minimal AuditGraphSource for planner tests with decorator +
    registration support.
    """

    build_fingerprint = "bf::test"
    repo_root = Path("/tmp/x")

    def __init__(
        self,
        *,
        paths: list[PathRecord],
        functions: list[FunctionRecord],
        decorators_by_fn: dict[str, list[DecoratorRecord]] | None = None,
        registrations_by_fn: dict[str, list[RegistrationSiteRecord]] | None = None,
    ) -> None:
        self._paths = paths
        self._functions = functions
        self._decorators_by_fn = decorators_by_fn or {}
        self._registrations_by_fn = registrations_by_fn or {}

    def iter_paths(self, *, page_size: int = 100) -> Iterator[PathRecord]:
        del page_size
        yield from self._paths

    def path_count(self) -> int:
        return len(self._paths)

    def iter_functions(self, *, page_size: int = 1000) -> Iterator[FunctionRecord]:
        del page_size
        yield from self._functions

    def fetch_decorators_for(self, function_ids: Sequence[str]):
        return {
            fid: self._decorators_by_fn[fid]
            for fid in function_ids
            if fid in self._decorators_by_fn
        }

    def fetch_registrations_for(self, function_ids: Sequence[str]):
        return {
            fid: self._registrations_by_fn[fid]
            for fid in function_ids
            if fid in self._registrations_by_fn
        }

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


def _deco(*, intent: str = "", framework: str = "") -> DecoratorRecord:
    return DecoratorRecord(
        decorator_id="d::1",
        expression=f"@{framework}.{intent}",
        framework=framework,
        intent=intent,
        file_path="src/x.py",
        line_number=1,
    )


def _reg(*, framework: str = "flask", intent: str = "route") -> RegistrationSiteRecord:
    return RegistrationSiteRecord(
        registration_id="reg::1",
        framework=framework,
        intent=intent,
        expression="app.add_url_rule('/x', f)",
        file_path="src/x.py",
        line_number=1,
    )


def _enable_units() -> AuditModeConfig:
    return AuditModeConfig(experimental=AuditExperimentalConfig(units=True))


class FlagOffEmitsNoEntryUnitsTests(unittest.TestCase):
    def test_default_audit_mode_emits_no_entry_units(self) -> None:
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="handle",
                    function_ids=("fn::handle",),
                ),
            ],
            functions=[_fn(function_id="fn::handle", qualified_name="myapp.handle")],
            decorators_by_fn={
                "fn::handle": [_deco(intent="route", framework="flask")],
            },
        )
        units = enumerate_units(source, audit_mode=AuditModeConfig())
        # Default flag off → only path units.
        self.assertEqual([u for u in units if isinstance(u, EntryAuditUnit)], [])


class FlagOnEmitsEntryUnitsTests(unittest.TestCase):
    def test_route_decorator_marks_function_as_entry(self) -> None:
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="list_users",
                    function_ids=("fn::list_users",),
                ),
            ],
            functions=[_fn(function_id="fn::list_users", qualified_name="api.list_users")],
            decorators_by_fn={
                "fn::list_users": [_deco(intent="route", framework="flask_or_fastapi")],
            },
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        entry_units = [u for u in units if isinstance(u, EntryAuditUnit)]
        self.assertEqual(len(entry_units), 1)
        self.assertEqual(entry_units[0].entry_function_id, "fn::list_users")
        self.assertEqual(entry_units[0].entry_qualified_name, "api.list_users")

    def test_task_handler_decorator_marks_entry(self) -> None:
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="process_email",
                    function_ids=("fn::process_email",),
                ),
            ],
            functions=[_fn(function_id="fn::process_email", qualified_name="tasks.process_email")],
            decorators_by_fn={
                "fn::process_email": [_deco(intent="task_handler", framework="celery")],
            },
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        entry_units = [u for u in units if isinstance(u, EntryAuditUnit)]
        self.assertEqual(len(entry_units), 1)

    def test_cli_entry_decorator_marks_entry(self) -> None:
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="deploy",
                    function_ids=("fn::deploy",),
                ),
            ],
            functions=[_fn(function_id="fn::deploy", qualified_name="cli.deploy")],
            decorators_by_fn={
                "fn::deploy": [_deco(intent="cli_entry", framework="click")],
            },
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        entry_units = [u for u in units if isinstance(u, EntryAuditUnit)]
        self.assertEqual(len(entry_units), 1)

    def test_inbound_registration_marks_entry_even_without_decorator(self) -> None:
        # Flask `app.add_url_rule(url, view)` doesn't put a
        # decorator on the function, but the REGISTERS edge does.
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="handle",
                    function_ids=("fn::handle",),
                ),
            ],
            functions=[_fn(function_id="fn::handle", qualified_name="api.handle")],
            registrations_by_fn={"fn::handle": [_reg(framework="flask")]},
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        entry_units = [u for u in units if isinstance(u, EntryAuditUnit)]
        self.assertEqual(len(entry_units), 1)

    def test_non_entry_intent_does_not_mark(self) -> None:
        # Decorators with intents NOT in {route, task_handler,
        # cli_entry} don't make the function an entry. Examples:
        # @login_required, @staticmethod, @property.
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="helper",
                    function_ids=("fn::helper",),
                ),
            ],
            functions=[_fn(function_id="fn::helper", qualified_name="utils.helper")],
            decorators_by_fn={
                "fn::helper": [
                    _deco(intent="auth_required", framework="auth"),
                    _deco(intent="staticmethod", framework="python"),
                ],
            },
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        entry_units = [u for u in units if isinstance(u, EntryAuditUnit)]
        self.assertEqual(entry_units, [])

    def test_external_stub_function_skipped(self) -> None:
        # Stub Function records (`is_external=True`) are not
        # entries — they're sinks. Even if the registration data
        # somehow points at one, the planner skips them.
        source = _StubGraphSource(
            paths=[],
            functions=[
                _fn(
                    function_id="external::subprocess.run",
                    qualified_name="subprocess.run",
                    is_external=True,
                    is_sink=True,
                ),
            ],
            decorators_by_fn={
                "external::subprocess.run": [_deco(intent="route", framework="flask")],
            },
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        entry_units = [u for u in units if isinstance(u, EntryAuditUnit)]
        self.assertEqual(entry_units, [])

    def test_entry_units_emerge_alongside_path_and_sink_units(self) -> None:
        # Full integration: a fixture with one path reaching one
        # sink, where the entry function is also decorated with
        # @app.route. enumerate_units emits PathAuditUnit (1) +
        # SinkAuditUnit (1) + EntryAuditUnit (1).
        source = _StubGraphSource(
            paths=[
                _path(
                    fingerprint="fp::1",
                    entry="list_users",
                    function_ids=("fn::list_users", "external::subprocess.run"),
                ),
            ],
            functions=[
                _fn(function_id="fn::list_users", qualified_name="api.list_users"),
                _fn(
                    function_id="external::subprocess.run",
                    qualified_name="subprocess.run",
                    is_external=True,
                    is_sink=True,
                ),
            ],
            decorators_by_fn={
                "fn::list_users": [_deco(intent="route", framework="flask_or_fastapi")],
            },
        )
        units = enumerate_units(source, audit_mode=_enable_units())
        kinds = {type(u) for u in units}
        self.assertIn(PathAuditUnit, kinds)
        self.assertIn(SinkAuditUnit, kinds)
        self.assertIn(EntryAuditUnit, kinds)
        # Exactly one entry, sink, path each.
        self.assertEqual(len([u for u in units if isinstance(u, EntryAuditUnit)]), 1)
        self.assertEqual(len([u for u in units if isinstance(u, SinkAuditUnit)]), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
