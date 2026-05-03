from __future__ import annotations

import unittest

from xauditor.graph.canonical import DiscoveredFile
from xauditor.graph.parsers import get_parser, set_parser_logger


def _make_file(path: str, language: str, content: str = "") -> DiscoveredFile:
    del content
    return DiscoveredFile(path=path, module_name="m", language=language)


def _parse(language: str, path: str, content: str):
    return get_parser(language).parse(_make_file(path, language), content)


class CParserTest(unittest.TestCase):
    def test_extracts_top_level_functions_and_calls(self) -> None:
        source = (
            "int helper(int x) { return x + 1; }\n"
            "int entry(int y) {\n"
            "    int z = helper(y);\n"
            "    return z;\n"
            "}\n"
        )
        parsed = _parse("c", "src/util.c", source)
        names = {fn.name for fn in parsed.functions}
        self.assertEqual(names, {"helper", "entry"})
        entry = next(fn for fn in parsed.functions if fn.name == "entry")
        self.assertEqual(entry.qualified_name, "entry")
        self.assertIsNone(entry.class_id)
        call_pairs = {(call.caller_function_id, call.callee_name) for call in parsed.calls}
        self.assertEqual(call_pairs, {(entry.function_id, "helper")})

    def test_ignores_function_prototypes(self) -> None:
        source = "int predeclared(void);\nint real(void) { return 1; }\n"
        parsed = _parse("c", "src/a.c", source)
        names = {fn.name for fn in parsed.functions}
        self.assertEqual(names, {"real"})

    def test_pointer_return_type_declarators(self) -> None:
        source = "char *dup(const char *s) { return g(s); }\n"
        parsed = _parse("c", "src/a.c", source)
        self.assertEqual({fn.name for fn in parsed.functions}, {"dup"})
        self.assertEqual({call.callee_name for call in parsed.calls}, {"g"})


class CppParserTest(unittest.TestCase):
    def test_namespaced_method_qualified_name(self) -> None:
        source = (
            "namespace outer {\n"
            "namespace inner {\n"
            "class Svc {\n"
            "public:\n"
            "    int field_a;\n"
            "    int run() { return helper(); }\n"
            "};\n"
            "}\n"
            "}\n"
            "int outer::inner::Svc::standalone() { return 7; }\n"
        )
        parsed = _parse("cpp", "src/svc.cpp", source)
        qualified = {fn.qualified_name for fn in parsed.functions}
        self.assertIn("outer::inner::Svc::run", qualified)
        self.assertIn("outer::inner::Svc::standalone", qualified)

        svc_class = next(cls for cls in parsed.classes if cls.name == "Svc")
        self.assertEqual({m.name for m in svc_class.methods}, {"run"})
        self.assertEqual({m.name for m in svc_class.members}, {"field_a"})
        run_fn = next(fn for fn in parsed.functions if fn.name == "run")
        self.assertEqual(run_fn.class_id, svc_class.class_id)

        call_names = {(call.callee_name, call.callee_qualified_name) for call in parsed.calls}
        self.assertIn(("helper", None), call_names)

    def test_qualified_callee_is_captured(self) -> None:
        source = (
            "namespace sec { int check(int x) { return x; } }\n"
            "int entry(int v) { return sec::check(v); }\n"
        )
        parsed = _parse("cpp", "src/a.cpp", source)
        qualified_calls = {
            call.callee_qualified_name for call in parsed.calls if call.callee_qualified_name
        }
        self.assertIn("sec::check", qualified_calls)

    def test_method_calls_on_object(self) -> None:
        source = (
            "struct Api { int run(); };\n"
            "int driver(Api *api) { return api->run(); }\n"
        )
        parsed = _parse("cpp", "src/a.cpp", source)
        driver_fn = next(fn for fn in parsed.functions if fn.name == "driver")
        call = next(c for c in parsed.calls if c.caller_function_id == driver_fn.function_id)
        self.assertEqual(call.callee_name, "run")
        self.assertEqual(call.callee_qualified_name, "api.run")


class GoParserTest(unittest.TestCase):
    def test_function_and_method_with_receiver(self) -> None:
        source = (
            "package svc\n"
            "func helper(x int) int { return x + 1 }\n"
            "func (s *Svc) Run() int {\n"
            "    return s.inner() + helper(2)\n"
            "}\n"
        )
        parsed = _parse("go", "svc.go", source)
        names = {fn.qualified_name for fn in parsed.functions}
        self.assertEqual(names, {"helper", "Svc.Run"})
        run_fn = next(fn for fn in parsed.functions if fn.name == "Run")
        self.assertEqual(run_fn.class_name, "Svc")
        pairs = {(c.callee_name, c.callee_qualified_name) for c in parsed.calls}
        self.assertIn(("inner", "s.inner"), pairs)
        self.assertIn(("helper", None), pairs)


class JavaParserTest(unittest.TestCase):
    def test_class_with_methods_and_invocations(self) -> None:
        source = (
            "class Svc {\n"
            "    int count;\n"
            "    int run(Api api) { return api.call(count); }\n"
            "    int helper() { return 1; }\n"
            "}\n"
        )
        parsed = _parse("java", "Svc.java", source)
        self.assertEqual({c.name for c in parsed.classes}, {"Svc"})
        self.assertEqual(
            {fn.qualified_name for fn in parsed.functions}, {"Svc.run", "Svc.helper"}
        )
        run_fn = next(fn for fn in parsed.functions if fn.name == "run")
        self.assertEqual(run_fn.class_name, "Svc")
        calls = {(c.callee_name, c.callee_qualified_name) for c in parsed.calls}
        self.assertIn(("call", "api.call"), calls)
        svc_cls = parsed.classes[0]
        self.assertEqual({m.name for m in svc_cls.members}, {"count"})

    def test_nested_class_qualified_name(self) -> None:
        source = (
            "class Outer {\n"
            "    class Inner {\n"
            "        int doIt() { return 7; }\n"
            "    }\n"
            "}\n"
        )
        parsed = _parse("java", "Outer.java", source)
        qualifieds = {fn.qualified_name for fn in parsed.functions}
        self.assertIn("Outer.Inner.doIt", qualifieds)


class JavaScriptParserTest(unittest.TestCase):
    def test_class_methods_and_top_level_function(self) -> None:
        source = (
            "class Svc {\n"
            "    run() { return this.helper(); }\n"
            "    helper() { return 1; }\n"
            "}\n"
            "function top() { obj.m(); }\n"
        )
        parsed = _parse("javascript", "svc.js", source)
        self.assertEqual(
            {fn.qualified_name for fn in parsed.functions}, {"Svc.run", "Svc.helper", "top"}
        )
        calls = {(c.callee_name, c.callee_qualified_name) for c in parsed.calls}
        self.assertIn(("helper", "this.helper"), calls)
        self.assertIn(("m", "obj.m"), calls)


class PythonParserFaultToleranceTest(unittest.TestCase):
    def tearDown(self) -> None:
        set_parser_logger(None)

    def test_syntax_error_returns_empty_parsed_file_and_logs_warning(self) -> None:
        warnings: list[str] = []

        class _CaptureLogger:
            def warning(self, message: str) -> None:
                warnings.append(message)

        set_parser_logger(_CaptureLogger())
        bad_source = "def broken():\n\treturn 1\n        return 2\n"
        parsed = _parse("python", "src/bad.py", bad_source)

        self.assertEqual(parsed.functions, ())
        self.assertEqual(parsed.classes, ())
        self.assertEqual(parsed.calls, ())
        self.assertEqual(parsed.module_symbols, ())
        self.assertEqual(parsed.symbol_uses, ())
        self.assertTrue(warnings, "parser must emit a warning for unparseable files")
        message = warnings[0]
        self.assertIn("src/bad.py", message)
        self.assertTrue(
            "SyntaxError" in message or "TabError" in message,
            f"warning must name the exception type, got: {message!r}",
        )

    def test_skip_does_not_require_a_logger(self) -> None:
        set_parser_logger(None)
        bad_source = "def broken(:\n    return 1\n"
        parsed = _parse("python", "src/bad.py", bad_source)
        self.assertEqual(parsed.functions, ())


class TypeScriptParserTest(unittest.TestCase):
    def test_class_with_type_annotations(self) -> None:
        source = (
            "class Svc {\n"
            "    run(): number { return this.helper(); }\n"
            "    helper(): number { return 1; }\n"
            "}\n"
            "export function gate(): void { Svc.call(); }\n"
        )
        parsed = _parse("typescript", "svc.ts", source)
        qualifieds = {fn.qualified_name for fn in parsed.functions}
        self.assertIn("Svc.run", qualifieds)
        self.assertIn("gate", qualifieds)
        calls = {(c.callee_name, c.callee_qualified_name) for c in parsed.calls}
        self.assertIn(("call", "Svc.call"), calls)


if __name__ == "__main__":
    unittest.main()
