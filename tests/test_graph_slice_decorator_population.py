"""Integration tests for GraphSlice `decorator_chain` population.

`capture-decorators-and-registrations` Commit B (Phase 2.1):

- `build_graph_slice(decorators_by_function_id=...)` populates
  `decorator_chain: tuple[DecoratorRef, ...]` when the v2 flag
  is on.
- `_classify_entry(...)` becomes deterministic when the chain
  has a recognized intent — `intent="route"` + path containing
  `/admin/` → `admin_http`; `intent="route"` (no admin) →
  `public_http`; `intent="task_handler"` → `internal_rpc`;
  `intent="cli_entry"` → `cli`.
- Pre-Commit-A graphs (no decorator data) keep today's
  name-pattern heuristic — empty
  `decorators_by_function_id={}` → `decorator_chain=()` and
  classification falls back to the existing logic.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.context import build_graph_slice, _classify_entry
from xauditor.models import DecoratorRecord, FunctionRecord


def _fn(*, function_id: str, qualified_name: str, file_path: str = "src/app.py") -> FunctionRecord:
    return FunctionRecord(
        function_id=function_id,
        name=qualified_name.rsplit(".", 1)[-1],
        qualified_name=qualified_name,
        file_path=file_path,
        module_name=qualified_name.rsplit(".", 1)[0] if "." in qualified_name else "",
        start_line=1,
        end_line=10,
        source="def f(): pass",
    )


def _decorator(
    *, expression: str, framework: str = "", intent: str = "", line: int = 1
) -> DecoratorRecord:
    return DecoratorRecord(
        decorator_id=f"deco::{abs(hash(expression)):x}",
        expression=expression,
        framework=framework,
        intent=intent,
        file_path="src/app.py",
        line_number=line,
    )


class DecoratorChainPopulationTests(unittest.TestCase):
    def test_empty_when_v2_flag_off(self) -> None:
        # `enable_v2_fields=False` → slice fields are empty
        # regardless of `decorators_by_function_id` content.
        slice_obj = build_graph_slice(
            repo_root=Path("/tmp/x"),
            path_functions=[_fn(function_id="fn-1", qualified_name="myapp.handler")],
            enable_v2_fields=False,
            decorators_by_function_id={
                "fn-1": [_decorator(expression='app.route("/x")', intent="route")],
            },
        )
        self.assertEqual(slice_obj.decorator_chain, ())

    def test_empty_when_no_decorators_supplied(self) -> None:
        slice_obj = build_graph_slice(
            repo_root=Path("/tmp/x"),
            path_functions=[_fn(function_id="fn-1", qualified_name="myapp.handler")],
            enable_v2_fields=True,
            decorators_by_function_id={},
        )
        self.assertEqual(slice_obj.decorator_chain, ())

    def test_chain_populated_with_v2_flag_on(self) -> None:
        slice_obj = build_graph_slice(
            repo_root=Path("/tmp/x"),
            path_functions=[
                _fn(function_id="fn-1", qualified_name="myapp.list_users"),
            ],
            enable_v2_fields=True,
            decorators_by_function_id={
                "fn-1": [
                    _decorator(
                        expression="login_required",
                        framework="auth",
                        intent="auth_required",
                        line=8,
                    ),
                    _decorator(
                        expression='app.route("/users")',
                        framework="flask_or_fastapi",
                        intent="route",
                        line=9,
                    ),
                ],
            },
        )
        self.assertEqual(len(slice_obj.decorator_chain), 2)
        # Both DecoratorRef instances anchor to the same function
        # (the call chain has only one function in this test).
        for ref in slice_obj.decorator_chain:
            self.assertEqual(ref.function_id, "fn-1")
        # framework + intent labels propagate from DecoratorRecord.
        intents = {ref.intent for ref in slice_obj.decorator_chain}
        self.assertEqual(intents, {"auth_required", "route"})

    def test_chain_walks_all_path_functions(self) -> None:
        # Multiple functions on the path → chain accumulates
        # decorators from each.
        slice_obj = build_graph_slice(
            repo_root=Path("/tmp/x"),
            path_functions=[
                _fn(function_id="fn-entry", qualified_name="myapp.handle"),
                _fn(function_id="fn-helper", qualified_name="myapp.helper"),
            ],
            enable_v2_fields=True,
            decorators_by_function_id={
                "fn-entry": [_decorator(expression="login_required", intent="auth_required")],
                "fn-helper": [_decorator(expression="cached_property", intent="cached_property")],
            },
        )
        self.assertEqual(len(slice_obj.decorator_chain), 2)
        per_fn = {ref.function_id: ref for ref in slice_obj.decorator_chain}
        self.assertIn("fn-entry", per_fn)
        self.assertIn("fn-helper", per_fn)


class EntryClassificationTests(unittest.TestCase):
    def test_route_decorator_with_admin_path_classifies_admin_http(self) -> None:
        kind = _classify_entry(
            entry_function_id="fn-1",
            function_definitions=[
                {"function_id": "fn-1", "qualified_name": "myapp.delete", "file_path": "src/app.py"},
            ],
            decorator_chain=[
                _decorator(
                    expression='app.delete("/admin/users/{id}")',
                    framework="fastapi",
                    intent="route",
                ).__class__(  # quick conversion to DecoratorRef-equivalent
                    decorator_id="d-1",
                    expression='app.delete("/admin/users/{id}")',
                    framework="fastapi",
                    intent="route",
                    file_path="src/app.py",
                    line_number=1,
                ),
            ],
        )
        # Note: _classify_entry duck-types `decorator_chain` items —
        # any object with `function_id` + `intent` + `expression`
        # works. Pass DecoratorRefs in production.
        # We pass DecoratorRecord here (which has expression /
        # intent) — the classifier only reads getattr names.
        self.assertEqual(kind, "admin_http")

    def test_route_decorator_no_admin_classifies_public_http(self) -> None:
        # The classifier checks the entry's own decorators (matched
        # by function_id). Route intent → public_http when no
        # /admin/ marker.
        from xauditor.audit.context import DecoratorRef

        kind = _classify_entry(
            entry_function_id="fn-1",
            function_definitions=[
                {"function_id": "fn-1", "qualified_name": "myapp.list_users", "file_path": "src/app.py"},
            ],
            decorator_chain=[
                DecoratorRef(
                    function_id="fn-1",
                    expression='app.route("/users")',
                    framework="flask_or_fastapi",
                    intent="route",
                ),
            ],
        )
        self.assertEqual(kind, "public_http")

    def test_task_handler_decorator_classifies_internal_rpc(self) -> None:
        from xauditor.audit.context import DecoratorRef

        kind = _classify_entry(
            entry_function_id="fn-1",
            function_definitions=[
                {"function_id": "fn-1", "qualified_name": "tasks.process", "file_path": "src/tasks.py"},
            ],
            decorator_chain=[
                DecoratorRef(
                    function_id="fn-1",
                    expression="celery.task",
                    framework="celery",
                    intent="task_handler",
                ),
            ],
        )
        self.assertEqual(kind, "internal_rpc")

    def test_cli_entry_decorator_classifies_cli(self) -> None:
        from xauditor.audit.context import DecoratorRef

        kind = _classify_entry(
            entry_function_id="fn-1",
            function_definitions=[
                {"function_id": "fn-1", "qualified_name": "cli.deploy", "file_path": "src/cli.py"},
            ],
            decorator_chain=[
                DecoratorRef(
                    function_id="fn-1",
                    expression="click.command",
                    framework="click",
                    intent="cli_entry",
                ),
            ],
        )
        self.assertEqual(kind, "cli")

    def test_no_decorator_chain_falls_back_to_name_heuristic(self) -> None:
        # Pre-Commit-A graphs (no decorator data) → classifier
        # consults the name pattern table verbatim. Existing
        # behaviour preserved.
        kind = _classify_entry(
            entry_function_id="fn-1",
            function_definitions=[
                {"function_id": "fn-1", "qualified_name": "api.handler", "file_path": "src/admin/views.py"},
            ],
            decorator_chain=(),
        )
        # File path contains `/admin/` so the existing heuristic
        # matches.
        self.assertEqual(kind, "admin_http")

    def test_decorator_for_other_function_ignored(self) -> None:
        # The classifier only considers decorators whose
        # function_id matches the entry. A decorator on a helper
        # function in the same chain doesn't affect the entry's
        # classification.
        from xauditor.audit.context import DecoratorRef

        kind = _classify_entry(
            entry_function_id="fn-entry",
            function_definitions=[
                {"function_id": "fn-entry", "qualified_name": "x.entry", "file_path": "src/x.py"},
            ],
            decorator_chain=[
                DecoratorRef(
                    function_id="fn-other",
                    expression='app.route("/users")',
                    framework="flask_or_fastapi",
                    intent="route",
                ),
            ],
        )
        # Falls back to name heuristic since the route decorator
        # is on `fn-other`, not the entry. Name pattern doesn't
        # match → unknown.
        self.assertEqual(kind, "unknown")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
