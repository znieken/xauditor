"""Tests for `capture-decorators-and-registrations` Commit C
(registrations walker + graph wiring + GraphSlice population).

Covers:

- `PythonParser._extract_registrations(...)` recognises Flask
  `add_url_rule(url, view)`, Django `path(url, view)` /
  `re_path(...)`, and Starlette `add_event_handler(event,
  handler)` call-site patterns.
- `_classify_registration_call(node)` returns
  `(framework, intent, callable_name)` for known patterns or
  `("", "", "")` for everything else.
- `_build_registration_records(...)` resolves
  `callable_name` against the function table (preferring
  same-file matches), emits one `RegistrationSiteRecord` per
  distinct (file, line, expression) MERGE key and one
  `FunctionRegistrationEdge` per (site, function) pair.
- `build_graph_slice(...)` populates
  `registration_context` from the supplied
  `registrations_by_function_id` map when the v2 flag is on.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.context import build_graph_slice
from xauditor.graph.canonical import (
    DiscoveredFile,
    DiscoveredRegistration,
    _build_registration_records,
)
from xauditor.graph.parsers import (
    PythonParser,
    _classify_registration_call,
)
from xauditor.models import (
    DecoratorRecord,
    FunctionRecord,
    RegistrationSiteRecord,
)


def _file(*, path: str = "src/handlers.py") -> DiscoveredFile:
    return DiscoveredFile(path=path, module_name="handlers", language="python")


def _fn(
    *,
    function_id: str,
    name: str,
    file_path: str = "src/handlers.py",
    is_external: bool = False,
) -> FunctionRecord:
    return FunctionRecord(
        function_id=function_id,
        name=name,
        qualified_name=f"handlers.{name}",
        file_path=file_path,
        module_name="handlers",
        start_line=1,
        end_line=10,
        source="def f(): pass",
        is_external=is_external,
    )


class PythonParserRegistrationExtractionTests(unittest.TestCase):
    def test_flask_add_url_rule_two_arg_form(self) -> None:
        # Flask `add_url_rule(url, view_func)` (no explicit name).
        content = (
            "from flask import Flask\n"
            "app = Flask(__name__)\n"
            "\n"
            "def list_users():\n"
            "    return []\n"
            "\n"
            "app.add_url_rule('/users', list_users)\n"
        )
        result = PythonParser().parse(_file(), content)
        self.assertEqual(len(result.registrations), 1)
        reg = result.registrations[0]
        self.assertEqual(reg.framework, "flask")
        self.assertEqual(reg.intent, "route")
        self.assertEqual(reg.callable_name, "list_users")
        self.assertIn("add_url_rule", reg.expression)

    def test_flask_add_url_rule_three_arg_form(self) -> None:
        content = (
            "def get_user():\n"
            "    return {}\n"
            "\n"
            "app.add_url_rule('/users/<int:id>', 'get_user', get_user)\n"
        )
        result = PythonParser().parse(_file(), content)
        self.assertEqual(len(result.registrations), 1)
        self.assertEqual(result.registrations[0].callable_name, "get_user")

    def test_django_path_form(self) -> None:
        content = (
            "from django.urls import path\n"
            "\n"
            "def list_articles():\n"
            "    return []\n"
            "\n"
            "urlpatterns = [\n"
            "    path('articles/', list_articles),\n"
            "]\n"
        )
        result = PythonParser().parse(_file(), content)
        self.assertEqual(len(result.registrations), 1)
        reg = result.registrations[0]
        self.assertEqual(reg.framework, "django")
        self.assertEqual(reg.intent, "route")
        self.assertEqual(reg.callable_name, "list_articles")

    def test_django_re_path_form(self) -> None:
        content = (
            "from django.urls import re_path\n"
            "\n"
            "def detail():\n"
            "    return {}\n"
            "\n"
            "urlpatterns = [re_path(r'^d/(\\d+)/', detail)]\n"
        )
        result = PythonParser().parse(_file(), content)
        self.assertEqual(len(result.registrations), 1)
        self.assertEqual(result.registrations[0].callable_name, "detail")

    def test_starlette_add_event_handler(self) -> None:
        content = (
            "def on_startup():\n"
            "    pass\n"
            "\n"
            "app.add_event_handler('startup', on_startup)\n"
        )
        result = PythonParser().parse(_file(), content)
        self.assertEqual(len(result.registrations), 1)
        reg = result.registrations[0]
        self.assertEqual(reg.framework, "starlette")
        self.assertEqual(reg.intent, "event_handler")
        self.assertEqual(reg.callable_name, "on_startup")

    def test_django_path_with_module_attribute_view(self) -> None:
        # `path('articles/', views.list_articles)` — the second
        # arg is an `ast.Attribute`, not an `ast.Name`. Walker
        # extracts the rightmost attribute (`list_articles`)
        # since the resolver matches on bare function names.
        content = (
            "from django.urls import path\n"
            "from . import views\n"
            "\n"
            "urlpatterns = [path('articles/', views.list_articles)]\n"
        )
        result = PythonParser().parse(_file(), content)
        self.assertEqual(len(result.registrations), 1)
        self.assertEqual(result.registrations[0].callable_name, "list_articles")

    def test_unrelated_call_no_registration(self) -> None:
        # `app.config.update({...})` should NOT match.
        content = (
            "app.config.update(SECRET_KEY='x')\n"
        )
        result = PythonParser().parse(_file(), content)
        self.assertEqual(result.registrations, ())

    def test_lambda_view_dropped(self) -> None:
        # Lambdas / dynamic callables → no resolvable name → drop.
        content = (
            "from django.urls import path\n"
            "\n"
            "urlpatterns = [path('x/', lambda r: None)]\n"
        )
        result = PythonParser().parse(_file(), content)
        self.assertEqual(result.registrations, ())


class BuildRegistrationRecordsTests(unittest.TestCase):
    def test_resolves_callable_name_against_function_table(self) -> None:
        registrations = [
            DiscoveredRegistration(
                file_path="src/handlers.py",
                framework="flask",
                intent="route",
                line_number=10,
                callable_name="list_users",
                expression="app.add_url_rule('/users', list_users)",
            ),
        ]
        functions = [_fn(function_id="fn::list_users", name="list_users")]
        sites, edges = _build_registration_records(
            registrations=registrations, function_records=functions
        )
        self.assertEqual(len(sites), 1)
        self.assertEqual(len(edges), 1)
        self.assertEqual(sites[0].framework, "flask")
        self.assertEqual(sites[0].intent, "route")
        self.assertEqual(edges[0].function_id, "fn::list_users")
        # Site / edge ids match.
        self.assertEqual(sites[0].registration_id, edges[0].registration_id)

    def test_drops_registration_with_unresolvable_callable(self) -> None:
        registrations = [
            DiscoveredRegistration(
                file_path="src/handlers.py",
                framework="flask",
                intent="route",
                line_number=10,
                callable_name="ghost_view",
                expression="app.add_url_rule('/x', ghost_view)",
            ),
        ]
        functions = [_fn(function_id="fn::list_users", name="list_users")]
        sites, edges = _build_registration_records(
            registrations=registrations, function_records=functions
        )
        self.assertEqual(sites, ())
        self.assertEqual(edges, ())

    def test_prefers_same_file_match_when_ambiguous(self) -> None:
        # Two parsed functions named `list_users` in different
        # files — the one in the SAME file as the registration
        # site wins.
        registrations = [
            DiscoveredRegistration(
                file_path="src/handlers/users.py",
                framework="flask",
                intent="route",
                line_number=10,
                callable_name="list_users",
                expression="app.add_url_rule('/users', list_users)",
            ),
        ]
        functions = [
            _fn(
                function_id="fn::other",
                name="list_users",
                file_path="src/handlers/items.py",
            ),
            _fn(
                function_id="fn::correct",
                name="list_users",
                file_path="src/handlers/users.py",
            ),
        ]
        _, edges = _build_registration_records(
            registrations=registrations, function_records=functions
        )
        self.assertEqual(edges[0].function_id, "fn::correct")

    def test_excludes_external_stub_functions(self) -> None:
        # Stub Function records (`is_external=True`) shouldn't
        # match registrations even if their `name` happens to
        # collide with the callable_name.
        registrations = [
            DiscoveredRegistration(
                file_path="src/handlers.py",
                framework="flask",
                intent="route",
                line_number=10,
                callable_name="run",
                expression="app.add_url_rule('/r', run)",
            ),
        ]
        functions = [
            _fn(
                function_id="external::subprocess.run",
                name="run",
                file_path="external://subprocess.run",
                is_external=True,
            ),
        ]
        sites, edges = _build_registration_records(
            registrations=registrations, function_records=functions
        )
        # Stub `run` excluded; no resolution → drop.
        self.assertEqual(sites, ())
        self.assertEqual(edges, ())

    def test_registration_id_is_deterministic(self) -> None:
        registrations = [
            DiscoveredRegistration(
                file_path="src/handlers.py",
                framework="flask",
                intent="route",
                line_number=10,
                callable_name="list_users",
                expression="app.add_url_rule('/users', list_users)",
            ),
        ]
        functions = [_fn(function_id="fn::list_users", name="list_users")]
        sites1, _ = _build_registration_records(
            registrations=registrations, function_records=functions
        )
        sites2, _ = _build_registration_records(
            registrations=registrations, function_records=functions
        )
        self.assertEqual(sites1[0].registration_id, sites2[0].registration_id)


class GraphSliceRegistrationContextTests(unittest.TestCase):
    def test_empty_when_no_registrations_supplied(self) -> None:
        slice_obj = build_graph_slice(
            repo_root=Path("/tmp/x"),
            path_functions=[_fn(function_id="fn-1", name="handler")],
            enable_v2_fields=True,
            registrations_by_function_id={},
        )
        self.assertEqual(slice_obj.registration_context, ())

    def test_populated_with_v2_flag_on(self) -> None:
        site = RegistrationSiteRecord(
            registration_id="reg::abc123",
            framework="flask",
            intent="route",
            expression="app.add_url_rule('/users', list_users)",
            file_path="src/handlers.py",
            line_number=10,
        )
        slice_obj = build_graph_slice(
            repo_root=Path("/tmp/x"),
            path_functions=[_fn(function_id="fn::list_users", name="list_users")],
            enable_v2_fields=True,
            registrations_by_function_id={"fn::list_users": [site]},
        )
        self.assertEqual(len(slice_obj.registration_context), 1)
        ref = slice_obj.registration_context[0]
        self.assertEqual(ref.function_id, "fn::list_users")
        self.assertEqual(ref.framework, "flask")
        self.assertEqual(ref.site_line, 10)
        self.assertIn("add_url_rule", ref.expression)

    def test_v2_flag_off_keeps_empty(self) -> None:
        site = RegistrationSiteRecord(
            registration_id="reg::abc123",
            framework="flask",
            intent="route",
            expression="app.add_url_rule('/x', f)",
            file_path="src/h.py",
            line_number=1,
        )
        slice_obj = build_graph_slice(
            repo_root=Path("/tmp/x"),
            path_functions=[_fn(function_id="fn-1", name="f")],
            enable_v2_fields=False,
            registrations_by_function_id={"fn-1": [site]},
        )
        self.assertEqual(slice_obj.registration_context, ())


class ClassifyRegistrationCallTests(unittest.TestCase):
    def test_unknown_call_returns_empty(self) -> None:
        # A direct test with a synthetic ast.Call.
        import ast

        node = ast.parse("foo.bar()", mode="eval").body
        self.assertIsInstance(node, ast.Call)
        framework, intent, callable_name = _classify_registration_call(node)
        self.assertEqual((framework, intent, callable_name), ("", "", ""))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
