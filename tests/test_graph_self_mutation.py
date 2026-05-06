"""Tests for `PythonParser._detect_self_mutation` and the
`mutates_self` flag propagation from parser → DiscoveredFunction
→ FunctionRecord.

`capture-decorators-and-registrations` Commit F. The flag drives
`StateAuditUnit` emission in the planner.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.graph.canonical import DiscoveredFile
from xauditor.graph.parsers import PythonParser


def _file() -> DiscoveredFile:
    return DiscoveredFile(path="src/x.py", module_name="x", language="python")


def _parse(content: str):
    return PythonParser().parse(_file(), content)


def _func_by_name(parsed_file, name: str):
    if parsed_file.classes:
        for cls in parsed_file.classes:
            for method in cls.methods:
                if method.name == name:
                    return method
    for fn in parsed_file.functions:
        if fn.name == name:
            return fn
    raise KeyError(name)


class DetectSelfMutationTests(unittest.TestCase):
    def test_direct_self_attribute_assign_marks_true(self) -> None:
        content = (
            "class Counter:\n"
            "    def increment(self):\n"
            "        self.value = 1\n"
        )
        method = _func_by_name(_parse(content), "increment")
        self.assertTrue(method.mutates_self)

    def test_annotated_self_assign_marks_true(self) -> None:
        content = (
            "class Counter:\n"
            "    def setup(self):\n"
            "        self.value: int = 0\n"
        )
        method = _func_by_name(_parse(content), "setup")
        self.assertTrue(method.mutates_self)

    def test_augmented_self_assign_marks_true(self) -> None:
        content = (
            "class Counter:\n"
            "    def bump(self):\n"
            "        self.value += 1\n"
        )
        method = _func_by_name(_parse(content), "bump")
        self.assertTrue(method.mutates_self)

    def test_pure_read_does_not_mark(self) -> None:
        content = (
            "class Counter:\n"
            "    def get(self):\n"
            "        return self.value\n"
        )
        method = _func_by_name(_parse(content), "get")
        self.assertFalse(method.mutates_self)

    def test_self_method_call_alone_does_not_mark(self) -> None:
        # `self.foo()` is NOT an assign — only mutates the
        # callee's state, which we conservatively don't track
        # (the called method is detected on its own merit).
        content = (
            "class Service:\n"
            "    def run(self):\n"
            "        self.foo()\n"
            "    def foo(self):\n"
            "        return None\n"
        )
        method = _func_by_name(_parse(content), "run")
        self.assertFalse(method.mutates_self)

    def test_module_level_function_never_mutates(self) -> None:
        content = "def f(self):\n    self.x = 1\n"
        # Even if a non-method takes a `self` arg, it's not
        # bound to a class, so we don't track it as a state
        # mutation.
        fn = _func_by_name(_parse(content), "f")
        self.assertFalse(fn.mutates_self)

    def test_nested_function_does_not_leak_into_outer(self) -> None:
        # Outer method only contains a nested function that
        # mutates `self`. Outer doesn't itself assign to
        # `self.x`, so outer mutates_self is False.
        content = (
            "class Service:\n"
            "    def outer(self):\n"
            "        def inner():\n"
            "            self.x = 1\n"
            "        return inner\n"
        )
        outer = _func_by_name(_parse(content), "outer")
        # `inner` has its own scope — but it accesses `self`
        # via closure. Detection scopes by AST containment
        # only — so inner's self.x = 1 is technically inside
        # outer's AST. Today we treat this as outer mutating
        # state too, since the closure makes it so.
        # Document the actual behaviour: detection walks the
        # outer body and sees the nested `self.x = 1`.
        # However, our implementation skips ast.FunctionDef
        # descendants — so outer is False here.
        self.assertFalse(outer.mutates_self)

    def test_init_with_assignment_marks_true(self) -> None:
        # `__init__` mutates `self` by definition — common
        # case; should be detected.
        content = (
            "class Service:\n"
            "    def __init__(self, x):\n"
            "        self.x = x\n"
        )
        init = _func_by_name(_parse(content), "__init__")
        self.assertTrue(init.mutates_self)

    def test_multiple_attribute_writes_mark_true(self) -> None:
        content = (
            "class Service:\n"
            "    def setup(self):\n"
            "        self.x = 1\n"
            "        self.y = 2\n"
            "        self.z = 3\n"
        )
        method = _func_by_name(_parse(content), "setup")
        self.assertTrue(method.mutates_self)

    def test_local_variable_assign_does_not_mark(self) -> None:
        content = (
            "class Service:\n"
            "    def calc(self):\n"
            "        x = 1\n"
            "        return x\n"
        )
        method = _func_by_name(_parse(content), "calc")
        self.assertFalse(method.mutates_self)

    def test_assign_to_self_subscript_does_not_mark(self) -> None:
        # `self[k] = v` — calls `__setitem__`. We conservatively
        # don't treat this as a mutates_self case (rare and
        # ambiguous).
        content = (
            "class Service:\n"
            "    def store(self, k, v):\n"
            "        self[k] = v\n"
        )
        method = _func_by_name(_parse(content), "store")
        self.assertFalse(method.mutates_self)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
