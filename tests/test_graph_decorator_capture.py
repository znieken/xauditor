"""Tests for `capture-decorators-and-registrations` Phase 1.2.

Covers the full decorator-capture pipeline:

- `PythonParser` extracts `@decorator` chains via
  `_build_function`'s new decorators capture, position-indexed
  bottom-up (closest-to-function = position 0).
- `framework_heuristics.classify_decorator(expression)` returns
  `(framework, intent)` for known patterns.
- `canonical._build_decorator_records(...)` translates each
  `DiscoveredFunction.decorators` tuple into `DecoratorRecord`
  graph nodes + `FunctionDecoratorEdge` edges, using
  classify_decorator(...) for framework + intent labels.

The Neo4j writer/reader round-trip lives in the existing
in-memory adapter tests (graph build → repository → audit
read flow) — not exercised here separately because the data
shape is simply pass-through dataclasses.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.graph.canonical import (
    DiscoveredFile,
    DiscoveredFunction,
    _build_decorator_records,
)
from xauditor.graph.framework_heuristics import (
    INTENT_VALUES,
    classify_decorator,
)
from xauditor.graph.parsers import PythonParser, _ParsedDecorator


def _file(*, path: str = "src/handlers.py", module_name: str = "handlers") -> DiscoveredFile:
    return DiscoveredFile(path=path, module_name=module_name, language="python")


class PythonParserDecoratorExtractionTests(unittest.TestCase):
    def test_function_with_no_decorators_emits_empty_tuple(self) -> None:
        content = "def handle_request():\n    return 42\n"
        result = PythonParser().parse(_file(), content)
        self.assertEqual(len(result.functions), 1)
        self.assertEqual(result.functions[0].decorators, ())

    def test_single_decorator_captured(self) -> None:
        content = (
            'from flask import Flask\n'
            'app = Flask(__name__)\n'
            '\n'
            '@app.route("/users")\n'
            'def list_users():\n'
            '    return []\n'
        )
        result = PythonParser().parse(_file(), content)
        fn = next(f for f in result.functions if f.name == "list_users")
        self.assertEqual(len(fn.decorators), 1)
        self.assertEqual(fn.decorators[0].position, 0)
        self.assertIn("app.route", fn.decorators[0].expression)
        self.assertIn('"/users"', fn.decorators[0].expression)

    def test_multiple_decorators_position_bottom_up(self) -> None:
        # In source order: @login_required is FARTHEST from the
        # function definition; @rate_limit is CLOSEST. Per Python
        # application semantics (bottom-up), `rate_limit` runs
        # first, then `app.route`, then `login_required`. Our
        # `position` mirrors this — position 0 = closest to fn.
        content = (
            "@login_required\n"
            '@app.route("/users")\n'
            "@rate_limit(per_minute=60)\n"
            "def list_users():\n"
            "    return []\n"
        )
        result = PythonParser().parse(_file(), content)
        fn = next(f for f in result.functions if f.name == "list_users")
        self.assertEqual(len(fn.decorators), 3)
        # position 0 = closest to function = rate_limit
        self.assertIn("rate_limit", fn.decorators[0].expression)
        # position 1 = middle = app.route
        self.assertIn("app.route", fn.decorators[1].expression)
        # position 2 = farthest = login_required
        self.assertIn("login_required", fn.decorators[2].expression)

    def test_async_function_decorators_captured(self) -> None:
        content = (
            '@app.get("/items")\n'
            'async def get_items():\n'
            '    return []\n'
        )
        result = PythonParser().parse(_file(), content)
        fn = next(f for f in result.functions if f.name == "get_items")
        self.assertEqual(len(fn.decorators), 1)
        self.assertIn("app.get", fn.decorators[0].expression)

    def test_class_method_decorators_captured(self) -> None:
        content = (
            "class MyClass:\n"
            "    @staticmethod\n"
            "    def helper():\n"
            "        return 1\n"
            "\n"
            "    @classmethod\n"
            "    @cached_property\n"
            "    def computed(cls):\n"
            "        return 2\n"
        )
        result = PythonParser().parse(_file(), content)
        helper = next(f for f in result.functions if f.name == "helper")
        self.assertEqual(len(helper.decorators), 1)
        self.assertEqual(helper.decorators[0].expression, "staticmethod")

        computed = next(f for f in result.functions if f.name == "computed")
        self.assertEqual(len(computed.decorators), 2)
        # bottom-up: cached_property closest, classmethod farther
        self.assertEqual(computed.decorators[0].expression, "cached_property")
        self.assertEqual(computed.decorators[1].expression, "classmethod")


class FrameworkHeuristicsTests(unittest.TestCase):
    def test_intent_values_closed_set(self) -> None:
        # Defensive — every (framework, intent) emission MUST land
        # in this closed set so downstream code (entry
        # classification, GraphSlice population) can branch
        # safely.
        for expr in [
            "app.route('/users')",
            "router.post('/items')",
            "celery.task",
            "click.command",
            "login_required",
            "csrf_exempt",
            "staticmethod",
            "classmethod",
            "property",
            "cached_property",
            "dataclass",
        ]:
            framework, intent = classify_decorator(expr)
            if intent:
                self.assertIn(intent, INTENT_VALUES, msg=f"{expr!r} → {intent!r}")

    def test_route_patterns(self) -> None:
        # Flask-style
        self.assertEqual(
            classify_decorator("app.route('/users')"),
            ("flask_or_fastapi", "route"),
        )
        # FastAPI per-method decorators
        self.assertEqual(
            classify_decorator("router.post('/items')"),
            ("fastapi", "route"),
        )
        self.assertEqual(
            classify_decorator("app.get('/health')"),
            ("fastapi", "route"),
        )

    def test_task_handler_patterns(self) -> None:
        self.assertEqual(
            classify_decorator("celery.task"),
            ("celery", "task_handler"),
        )
        self.assertEqual(
            classify_decorator("shared_task"),
            ("celery", "task_handler"),
        )

    def test_auth_required_patterns(self) -> None:
        self.assertEqual(
            classify_decorator("login_required"),
            ("auth", "auth_required"),
        )
        self.assertEqual(
            classify_decorator("require_admin"),
            ("auth", "auth_required"),
        )
        self.assertEqual(
            classify_decorator("permission_required('users.add')"),
            ("auth", "auth_required"),
        )

    def test_unrecognised_pattern_returns_empty_strings(self) -> None:
        self.assertEqual(
            classify_decorator("my_company.bespoke_decorator()"),
            ("", ""),
        )
        self.assertEqual(classify_decorator(""), ("", ""))
        self.assertEqual(classify_decorator("   "), ("", ""))

    def test_python_builtin_decorators(self) -> None:
        self.assertEqual(
            classify_decorator("staticmethod"),
            ("python", "staticmethod"),
        )
        self.assertEqual(
            classify_decorator("classmethod"),
            ("python", "classmethod"),
        )
        self.assertEqual(
            classify_decorator("property"),
            ("python", "property"),
        )


class BuildDecoratorRecordsTests(unittest.TestCase):
    def test_no_decorators_returns_empty_tuples(self) -> None:
        functions = [
            DiscoveredFunction(
                function_id="fn-1",
                name="handler",
                qualified_name="myapp.handler",
                file_path="src/myapp.py",
                module_name="myapp",
                start_line=1,
                end_line=5,
            ),
        ]
        decos, edges = _build_decorator_records(functions)
        self.assertEqual(decos, ())
        self.assertEqual(edges, ())

    def test_emits_one_record_per_distinct_decorator_one_edge_per_position(self) -> None:
        # Two functions; one shares a decorator location with the
        # other (unrealistic in real code but useful for asserting
        # the dedup MERGE-key semantics).
        functions = [
            DiscoveredFunction(
                function_id="fn-1",
                name="list_users",
                qualified_name="myapp.list_users",
                file_path="src/myapp.py",
                module_name="myapp",
                start_line=10,
                end_line=15,
                decorators=(
                    _ParsedDecorator(
                        expression='app.route("/users")', line=9, position=0
                    ),
                    _ParsedDecorator(
                        expression="login_required", line=8, position=1
                    ),
                ),
            ),
            DiscoveredFunction(
                function_id="fn-2",
                name="get_user",
                qualified_name="myapp.get_user",
                file_path="src/myapp.py",
                module_name="myapp",
                start_line=20,
                end_line=24,
                decorators=(
                    _ParsedDecorator(
                        expression='app.get("/users/{id}")', line=19, position=0
                    ),
                ),
            ),
        ]
        decos, edges = _build_decorator_records(functions)
        self.assertEqual(len(decos), 3)  # 3 distinct (file, line, expr)
        self.assertEqual(len(edges), 3)

        # Edge → function mapping correct.
        fn1_edges = [e for e in edges if e.function_id == "fn-1"]
        fn2_edges = [e for e in edges if e.function_id == "fn-2"]
        self.assertEqual(len(fn1_edges), 2)
        self.assertEqual(len(fn2_edges), 1)

    def test_decorator_record_carries_framework_intent_from_classifier(self) -> None:
        functions = [
            DiscoveredFunction(
                function_id="fn-1",
                name="handler",
                qualified_name="myapp.handler",
                file_path="src/myapp.py",
                module_name="myapp",
                start_line=10,
                end_line=15,
                decorators=(
                    _ParsedDecorator(
                        expression='app.route("/users")', line=9, position=0
                    ),
                ),
            ),
        ]
        decos, _ = _build_decorator_records(functions)
        self.assertEqual(len(decos), 1)
        self.assertEqual(decos[0].framework, "flask_or_fastapi")
        self.assertEqual(decos[0].intent, "route")
        self.assertEqual(decos[0].file_path, "src/myapp.py")
        self.assertEqual(decos[0].line_number, 9)

    def test_unrecognised_decorator_keeps_empty_framework_intent(self) -> None:
        functions = [
            DiscoveredFunction(
                function_id="fn-1",
                name="handler",
                qualified_name="myapp.handler",
                file_path="src/myapp.py",
                module_name="myapp",
                start_line=10,
                end_line=15,
                decorators=(
                    _ParsedDecorator(
                        expression="my_custom_decorator", line=9, position=0
                    ),
                ),
            ),
        ]
        decos, _ = _build_decorator_records(functions)
        self.assertEqual(decos[0].framework, "")
        self.assertEqual(decos[0].intent, "")

    def test_decorator_id_is_deterministic_idempotent(self) -> None:
        # Same decorator (file, line, expression) → same id across
        # invocations. Re-graph-build relies on this for MERGE
        # idempotency.
        functions = [
            DiscoveredFunction(
                function_id="fn-1",
                name="h",
                qualified_name="x.h",
                file_path="src/x.py",
                module_name="x",
                start_line=10,
                end_line=12,
                decorators=(
                    _ParsedDecorator(expression="login_required", line=9, position=0),
                ),
            ),
        ]
        decos1, _ = _build_decorator_records(functions)
        decos2, _ = _build_decorator_records(functions)
        self.assertEqual(decos1[0].decorator_id, decos2[0].decorator_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
