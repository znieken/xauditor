"""Tests for `canonical.py`'s sink-stub synthesis pass.

Covers `capture-decorators-and-registrations` Commit 2:

- `_synthesize_sink_stubs(...)` creates a stub `FunctionRecord`
  (with `is_external=True, is_well_known_sink=True, sink_kind=<kind>`)
  for each external sink FQN referenced by `discovered.calls`
  but not present in the parsed function set.
- `_with_sink_label(...)` populates sink labels on parsed
  `FunctionRecord`s whose `qualified_name` matches the resolved
  sink_set.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.graph.canonical import (
    DiscoveredCall,
    _synthesize_sink_stubs,
    _with_sink_label,
)
from xauditor.graph.sink_labelling import resolve_sink_set
from xauditor.models import FunctionRecord


def _call(*, callee_qualified_name: str | None) -> DiscoveredCall:
    return DiscoveredCall(
        caller_function_id="fn-caller",
        callee_name=(callee_qualified_name or "unknown").rsplit(".", 1)[-1],
        callee_qualified_name=callee_qualified_name,
        file_path="src/handler.py",
        line_number=42,
        evidence="subprocess.run(user_input, shell=True)",
    )


def _fn(*, function_id: str, qualified_name: str) -> FunctionRecord:
    return FunctionRecord(
        function_id=function_id,
        name=qualified_name.rsplit(".", 1)[-1],
        qualified_name=qualified_name,
        file_path="src/" + qualified_name.replace(".", "/") + ".py",
        module_name=qualified_name.rsplit(".", 1)[0] if "." in qualified_name else "",
        start_line=1,
        end_line=10,
        source="def f(): pass",
    )


class SynthesizeSinkStubsTests(unittest.TestCase):
    def test_creates_stub_for_external_sink_call(self) -> None:
        sink_set = resolve_sink_set()
        calls = [_call(callee_qualified_name="subprocess.run")]
        stubs = _synthesize_sink_stubs(
            calls=calls,
            parsed_qualified_names=set(),
            sink_set=sink_set,
        )
        self.assertEqual(len(stubs), 1)
        stub = stubs[0]
        self.assertEqual(stub.qualified_name, "subprocess.run")
        self.assertEqual(stub.function_id, "external::subprocess.run")
        self.assertTrue(stub.is_external)
        self.assertTrue(stub.is_well_known_sink)
        self.assertEqual(stub.sink_kind, "subprocess")
        self.assertEqual(stub.source, "")
        self.assertEqual(stub.file_path, "external://subprocess.run")

    def test_skips_call_with_parsed_target(self) -> None:
        # The audit target re-defined `subprocess.run` (unlikely but
        # legal). We DON'T create a stub when the parser already
        # produced a FunctionRecord for that FQN — the parsed
        # version wins (sink labelling on parsed records is handled
        # separately by `_with_sink_label`).
        sink_set = resolve_sink_set()
        calls = [_call(callee_qualified_name="subprocess.run")]
        stubs = _synthesize_sink_stubs(
            calls=calls,
            parsed_qualified_names={"subprocess.run"},
            sink_set=sink_set,
        )
        self.assertEqual(stubs, ())

    def test_skips_non_sink_external_calls(self) -> None:
        sink_set = resolve_sink_set()
        calls = [
            _call(callee_qualified_name="json.dumps"),
            _call(callee_qualified_name="logging.info"),
        ]
        stubs = _synthesize_sink_stubs(
            calls=calls,
            parsed_qualified_names=set(),
            sink_set=sink_set,
        )
        self.assertEqual(stubs, ())

    def test_dedupes_by_fqn(self) -> None:
        # Two calls to the same sink → one stub.
        sink_set = resolve_sink_set()
        calls = [
            _call(callee_qualified_name="subprocess.run"),
            _call(callee_qualified_name="subprocess.run"),
            _call(callee_qualified_name="os.system"),
        ]
        stubs = _synthesize_sink_stubs(
            calls=calls,
            parsed_qualified_names=set(),
            sink_set=sink_set,
        )
        self.assertEqual(len(stubs), 2)
        fqns = {stub.qualified_name for stub in stubs}
        self.assertEqual(fqns, {"subprocess.run", "os.system"})

    def test_skips_call_with_none_callee_qualified_name(self) -> None:
        # Calls where the parser couldn't resolve the FQN at all
        # (e.g. dynamic dispatch like `getattr(obj, 'method')()`).
        # Without a target FQN there's nothing to match against the
        # sink table.
        sink_set = resolve_sink_set()
        calls = [_call(callee_qualified_name=None)]
        stubs = _synthesize_sink_stubs(
            calls=calls,
            parsed_qualified_names=set(),
            sink_set=sink_set,
        )
        self.assertEqual(stubs, ())

    def test_empty_sink_set_returns_no_stubs(self) -> None:
        # Defensive: operator pruned the sink list to empty.
        calls = [_call(callee_qualified_name="subprocess.run")]
        stubs = _synthesize_sink_stubs(
            calls=calls,
            parsed_qualified_names=set(),
            sink_set={},
        )
        self.assertEqual(stubs, ())

    def test_custom_sink_with_empty_kind_lands_in_stub(self) -> None:
        # Operator added an FQN via `audit.sinks.custom` — sink_kind
        # is "" since the config shape doesn't carry per-FQN kind
        # today. The stub still gets `is_well_known_sink=True` so
        # SinkAuditUnit enumeration walks it.
        sink_set = resolve_sink_set(custom=("myapp.utils.run_shell",))
        calls = [_call(callee_qualified_name="myapp.utils.run_shell")]
        stubs = _synthesize_sink_stubs(
            calls=calls,
            parsed_qualified_names=set(),
            sink_set=sink_set,
        )
        self.assertEqual(len(stubs), 1)
        self.assertTrue(stubs[0].is_well_known_sink)
        self.assertEqual(stubs[0].sink_kind, "")


class WithSinkLabelTests(unittest.TestCase):
    def test_returns_record_unchanged_when_sink_kind_is_none(self) -> None:
        record = _fn(function_id="fn-1", qualified_name="myapp.foo")
        result = _with_sink_label(record, None)
        self.assertIs(result, record)

    def test_populates_sink_label_fields(self) -> None:
        record = _fn(function_id="fn-1", qualified_name="myapp.run_shell")
        result = _with_sink_label(record, "subprocess")
        self.assertNotEqual(result.function_id, "")
        self.assertTrue(result.is_well_known_sink)
        self.assertEqual(result.sink_kind, "subprocess")
        # is_external NOT flipped by labelling — labelling applies
        # to parsed records too (this test).
        self.assertFalse(result.is_external)
        # All other fields preserved.
        self.assertEqual(result.qualified_name, "myapp.run_shell")
        self.assertEqual(result.source, "def f(): pass")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
