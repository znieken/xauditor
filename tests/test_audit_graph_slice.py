"""Tests for `build_graph_slice` and the `GraphSlice` dataclass.

Covers `restructure-audit-modes-and-coverage` Phase 2A:

- Legacy three fields (`call_chain`, `function_definitions`,
  `referenced_symbols`) are populated unconditionally.
- New v2 fields (`decorator_chain`, `registration_context`,
  `entry_classification`, `type_context`,
  `cross_path_definitions`) are emitted as empty / `unknown`
  when the `audit.experimental.graph_slice` flag is OFF, and
  populated when the flag is ON.
- The heuristic `entry_classification` matches the documented
  name-pattern table.
- `cross_path_definitions` surfaces symbols read on the path
  whose declaring function is OUTSIDE the path.
- `type_context` filters out no-info entries and tags ORM-shaped
  annotations.
- `to_payload_dict` projects to the legacy dict shape with the
  new `graph_slice` sub-key, so existing stage payload consumers
  keep working.
- `build_path_context` legacy alias still returns the historical
  three-key dict (no `graph_slice` sub-key).

Phase 2A intentionally does NOT exercise `decorator_chain` or
`registration_context` — those fields are reserved for a future
graph-builder enhancement and stay empty regardless of the
flag.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.context import (
    DefSite,
    GraphSlice,
    TypeRef,
    build_graph_slice,
    build_path_context,
)
from xauditor.models import (
    FunctionRecord,
    FunctionSymbolUseEdge,
    ModuleSymbolRecord,
)


def _function(
    function_id: str,
    qualified_name: str,
    file_path: str = "app.py",
    start: int = 1,
    end: int = 5,
) -> FunctionRecord:
    return FunctionRecord(
        function_id=function_id,
        name=qualified_name.split(".")[-1],
        qualified_name=qualified_name,
        file_path=file_path,
        module_name="app",
        start_line=start,
        end_line=end,
        source=f"def {qualified_name.split('.')[-1]}(): pass",
    )


def _symbol(
    symbol_id: str,
    name: str,
    *,
    type_annotation: str = "",
    value_repr: str = "",
    file_path: str = "app.py",
    line: int = 1,
) -> ModuleSymbolRecord:
    return ModuleSymbolRecord(
        symbol_id=symbol_id,
        name=name,
        kind="variable",
        module_name="app",
        file_path=file_path,
        start_line=line,
        end_line=line,
        type_annotation=type_annotation,
        value_repr=value_repr,
    )


class GraphSliceLegacyShapeTests(unittest.TestCase):
    """Legacy three fields stay shape-stable across the flag flip."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)
        (self.repo / "app.py").write_text(
            "def main():\n    return 1\n", encoding="utf-8"
        )

    def test_call_chain_function_definitions_referenced_symbols_present(self) -> None:
        path_functions = [
            _function("fn::main", "main"),
            _function("fn::helper", "helper", start=10, end=12),
        ]
        symbol = _symbol("sym::cfg", "CFG", type_annotation="dict[str, str]")
        uses = [
            FunctionSymbolUseEdge(
                function_id="fn::main",
                symbol_id="sym::cfg",
                line_number=2,
                evidence="CFG['secret']",
            ),
        ]

        for flag in (False, True):
            with self.subTest(flag=flag):
                slice_obj = build_graph_slice(
                    repo_root=self.repo,
                    path_functions=path_functions,
                    module_symbols=[symbol],
                    function_symbol_uses=uses,
                    enable_v2_fields=flag,
                )
                self.assertEqual(len(slice_obj.call_chain), 2)
                self.assertEqual(slice_obj.call_chain[0]["function_id"], "fn::main")
                self.assertEqual(len(slice_obj.function_definitions), 2)
                self.assertEqual(len(slice_obj.referenced_symbols), 1)
                self.assertEqual(
                    slice_obj.referenced_symbols[0]["name"], "CFG"
                )

    def test_to_payload_dict_includes_graph_slice_subkey(self) -> None:
        slice_obj = build_graph_slice(
            repo_root=self.repo,
            path_functions=[_function("fn::main", "main")],
            enable_v2_fields=True,
        )
        payload = slice_obj.to_payload_dict()
        # Legacy three keys are at the top level (existing consumers
        # read them directly).
        self.assertIn("call_chain", payload)
        self.assertIn("function_definitions", payload)
        self.assertIn("referenced_symbols", payload)
        # New fields go under `graph_slice` so existing prompts that
        # do `payload["call_chain"]` keep working.
        self.assertIn("graph_slice", payload)
        gs = payload["graph_slice"]
        for new_key in (
            "decorator_chain",
            "registration_context",
            "entry_classification",
            "type_context",
            "cross_path_definitions",
        ):
            self.assertIn(new_key, gs)


class GraphSliceFlagOffTests(unittest.TestCase):
    """When the flag is off, all v2 fields are empty/`unknown`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def test_v2_fields_empty_when_flag_off(self) -> None:
        slice_obj = build_graph_slice(
            repo_root=self.repo,
            path_functions=[_function("fn::main", "main")],
            module_symbols=[
                _symbol("sym::1", "X", type_annotation="Mapped[str]")
            ],
            function_symbol_uses=[],
            enable_v2_fields=False,
        )
        self.assertEqual(slice_obj.decorator_chain, ())
        self.assertEqual(slice_obj.registration_context, ())
        self.assertEqual(slice_obj.entry_classification, "unknown")
        self.assertEqual(slice_obj.type_context, ())
        self.assertEqual(slice_obj.cross_path_definitions, ())

    def test_legacy_alias_returns_three_key_dict(self) -> None:
        result = build_path_context(
            repo_root=self.repo,
            path_functions=[_function("fn::main", "main")],
            module_symbols=[],
            function_symbol_uses=[],
        )
        self.assertEqual(
            set(result.keys()),
            {"call_chain", "function_definitions", "referenced_symbols"},
        )


class EntryClassificationHeuristicTests(unittest.TestCase):
    """The name-pattern heuristic returns the documented `EntryKind`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def _classify(self, *, file_path: str, qualified_name: str) -> str:
        slice_obj = build_graph_slice(
            repo_root=self.repo,
            path_functions=[
                _function("fn::entry", qualified_name, file_path=file_path)
            ],
            enable_v2_fields=True,
        )
        return slice_obj.entry_classification

    def test_admin_http(self) -> None:
        self.assertEqual(
            self._classify(
                file_path="app/admin/users.py", qualified_name="list_users"
            ),
            "admin_http",
        )

    def test_cron(self) -> None:
        self.assertEqual(
            self._classify(file_path="jobs/sweep.py", qualified_name="cron_sweep"),
            "cron",
        )

    def test_cli(self) -> None:
        self.assertEqual(
            self._classify(file_path="src/cli.py", qualified_name="main"),
            "cli",
        )

    def test_test_only_takes_priority_over_admin(self) -> None:
        # `tests/admin/...` is test code, not admin handler code.
        self.assertEqual(
            self._classify(
                file_path="tests/admin/test_routes.py",
                qualified_name="test_admin_users",
            ),
            "test_only",
        )

    def test_internal_rpc(self) -> None:
        self.assertEqual(
            self._classify(
                file_path="src/rpc/payments.py",
                qualified_name="charge_card",
            ),
            "internal_rpc",
        )

    def test_public_http_default_for_handler_pattern(self) -> None:
        self.assertEqual(
            self._classify(
                file_path="src/api/users.py",
                qualified_name="get_user_profile",
            ),
            "public_http",
        )

    def test_unknown_for_unrecognised_pattern(self) -> None:
        self.assertEqual(
            self._classify(
                file_path="lib/util.py",
                qualified_name="compute_hash",
            ),
            "unknown",
        )


class TypeContextTests(unittest.TestCase):
    """`type_context` filters no-info entries and flags ORM annotations."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def test_no_info_symbols_filtered(self) -> None:
        slice_obj = build_graph_slice(
            repo_root=self.repo,
            path_functions=[_function("fn::main", "main")],
            module_symbols=[
                _symbol("sym::a", "A"),  # no annotation, no value_repr
                _symbol("sym::b", "B", type_annotation="int"),
                _symbol("sym::c", "C", value_repr="DEBUG = True"),
            ],
            function_symbol_uses=[
                FunctionSymbolUseEdge(
                    function_id="fn::main",
                    symbol_id=sid,
                    line_number=1,
                    evidence="ref",
                )
                for sid in ("sym::a", "sym::b", "sym::c")
            ],
            enable_v2_fields=True,
        )
        type_names = {t.name for t in slice_obj.type_context}
        self.assertEqual(type_names, {"B", "C"})

    def test_orm_column_detection(self) -> None:
        slice_obj = build_graph_slice(
            repo_root=self.repo,
            path_functions=[_function("fn::main", "main")],
            module_symbols=[
                _symbol(
                    "sym::orm",
                    "users_id",
                    type_annotation="Mapped[int]",
                ),
                _symbol(
                    "sym::plain",
                    "name",
                    type_annotation="str",
                ),
            ],
            function_symbol_uses=[
                FunctionSymbolUseEdge(
                    function_id="fn::main",
                    symbol_id=sid,
                    line_number=1,
                    evidence="ref",
                )
                for sid in ("sym::orm", "sym::plain")
            ],
            enable_v2_fields=True,
        )
        by_name = {t.name: t for t in slice_obj.type_context}
        self.assertTrue(by_name["users_id"].is_orm_column)
        self.assertFalse(by_name["name"].is_orm_column)


class CrossPathDefinitionsTests(unittest.TestCase):
    """Symbols read on the path but defined outside it."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def test_external_definition_surfaces(self) -> None:
        # Path: fn::main → fn::handler. Symbol `cfg` is referenced
        # by fn::main (in-path) AND by fn::sanitize (out-of-path).
        # `cross_path_definitions` should include the out-of-path use
        # so the analyzer can see "this symbol was also touched off
        # the audit unit".
        slice_obj = build_graph_slice(
            repo_root=self.repo,
            path_functions=[
                _function("fn::main", "main"),
                _function("fn::handler", "handler"),
            ],
            module_symbols=[
                _symbol("sym::cfg", "CFG", file_path="app/conf.py"),
            ],
            function_symbol_uses=[
                FunctionSymbolUseEdge(
                    function_id="fn::main",
                    symbol_id="sym::cfg",
                    line_number=2,
                    evidence="CFG['secret']",
                ),
                FunctionSymbolUseEdge(
                    function_id="fn::sanitize",  # NOT on path
                    symbol_id="sym::cfg",
                    line_number=42,
                    evidence="CFG = sanitize(CFG)",
                ),
            ],
            enable_v2_fields=True,
        )
        self.assertEqual(len(slice_obj.cross_path_definitions), 1)
        defsite = slice_obj.cross_path_definitions[0]
        self.assertEqual(defsite.name, "CFG")
        self.assertEqual(defsite.defining_function_id, "fn::sanitize")

    def test_only_in_path_symbols_have_no_cross_path_defs(self) -> None:
        # Symbol referenced only by in-path functions → no
        # cross-path entry.
        slice_obj = build_graph_slice(
            repo_root=self.repo,
            path_functions=[_function("fn::main", "main")],
            module_symbols=[_symbol("sym::cfg", "CFG")],
            function_symbol_uses=[
                FunctionSymbolUseEdge(
                    function_id="fn::main",
                    symbol_id="sym::cfg",
                    line_number=2,
                    evidence="CFG",
                ),
            ],
            enable_v2_fields=True,
        )
        self.assertEqual(slice_obj.cross_path_definitions, ())


class FlagOnPayloadShapeRegressionTests(unittest.TestCase):
    """Snapshot test: payload's new `graph_slice` block contains the
    expected keys when the flag is on."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name)

    def test_payload_contains_v2_block_with_real_data(self) -> None:
        slice_obj = build_graph_slice(
            repo_root=self.repo,
            path_functions=[
                _function(
                    "fn::admin_users",
                    "admin_users.list_admins",
                    file_path="app/admin/users.py",
                )
            ],
            module_symbols=[
                _symbol("sym::1", "ROLES", type_annotation="list[str]"),
            ],
            function_symbol_uses=[
                FunctionSymbolUseEdge(
                    function_id="fn::admin_users",
                    symbol_id="sym::1",
                    line_number=2,
                    evidence="ROLES",
                ),
            ],
            enable_v2_fields=True,
        )
        payload = slice_obj.to_payload_dict()
        gs = payload["graph_slice"]
        self.assertEqual(gs["entry_classification"], "admin_http")
        self.assertEqual(len(gs["type_context"]), 1)
        self.assertEqual(gs["type_context"][0]["name"], "ROLES")
        # Phase 2A reserves these for a future graph-builder change.
        self.assertEqual(gs["decorator_chain"], [])
        self.assertEqual(gs["registration_context"], [])


if __name__ == "__main__":
    unittest.main()
