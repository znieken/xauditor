"""Tests for `xauditor.graph.sink_labelling`.

Covers `capture-decorators-and-registrations` Phase 1.4 algorithmic
core: the default sink table + `resolve_sink_set(...)` config
overlay + `label_sinks(...)` Function-node matching pass.

The graph-build wiring + Neo4j writer extension + planner emission
land in subsequent commits and have their own tests.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.graph.sink_labelling import (
    SINK_KINDS,
    _DEFAULT_SINK_TABLE,
    label_sinks,
    resolve_sink_set,
)
from xauditor.models import FunctionRecord


def _fn(*, function_id: str, qualified_name: str) -> FunctionRecord:
    """Minimal FunctionRecord builder for sink-labelling tests."""

    return FunctionRecord(
        function_id=function_id,
        name=qualified_name.rsplit(".", 1)[-1],
        qualified_name=qualified_name,
        file_path="external://" + qualified_name,
        module_name=qualified_name.rsplit(".", 1)[0] if "." in qualified_name else "",
        start_line=0,
        end_line=0,
        source="",
    )


class DefaultSinkTableTests(unittest.TestCase):
    def test_every_default_table_sink_kind_is_known(self) -> None:
        for fqn, kind in _DEFAULT_SINK_TABLE.items():
            self.assertIn(
                kind,
                SINK_KINDS,
                f"Sink {fqn!r} has kind {kind!r} not in SINK_KINDS",
            )

    def test_subprocess_family_present(self) -> None:
        # The "subprocess injection" class needs full coverage of the
        # Python subprocess + os.* family so PR reviewers don't have
        # to add them on first dogfooding.
        for fqn in (
            "subprocess.run",
            "subprocess.Popen",
            "subprocess.call",
            "subprocess.check_call",
            "subprocess.check_output",
            "os.system",
            "os.popen",
        ):
            self.assertEqual(_DEFAULT_SINK_TABLE.get(fqn), "subprocess")

    def test_command_class_present(self) -> None:
        for fqn in ("eval", "exec", "compile"):
            self.assertEqual(_DEFAULT_SINK_TABLE.get(fqn), "command")

    def test_deserialization_class_present(self) -> None:
        for fqn in ("pickle.loads", "yaml.load", "yaml.unsafe_load"):
            self.assertEqual(_DEFAULT_SINK_TABLE.get(fqn), "deserializer")

    def test_http_client_class_present(self) -> None:
        for fqn in ("requests.get", "httpx.post", "urllib.request.urlopen"):
            self.assertEqual(_DEFAULT_SINK_TABLE.get(fqn), "http_client")

    def test_multilanguage_coverage_baseline(self) -> None:
        # The default table covers Python (heavy) + a representative
        # cross-language subset so deep + multi-language audits get
        # something out of the box. Operators extend per-language
        # depth via `audit.sinks.custom`.
        self.assertEqual(_DEFAULT_SINK_TABLE.get("os/exec.Command"), "subprocess")  # Go
        self.assertEqual(_DEFAULT_SINK_TABLE.get("java.lang.Runtime.exec"), "subprocess")  # Java
        self.assertEqual(_DEFAULT_SINK_TABLE.get("child_process.exec"), "subprocess")  # JS
        self.assertEqual(
            _DEFAULT_SINK_TABLE.get("std::process::Command"), "subprocess"
        )  # Rust
        self.assertEqual(_DEFAULT_SINK_TABLE.get("system"), "subprocess")  # C / C++


class ResolveSinkSetTests(unittest.TestCase):
    def test_empty_well_known_falls_back_to_full_default_table(self) -> None:
        resolved = resolve_sink_set(well_known=(), custom=())
        self.assertEqual(resolved, _DEFAULT_SINK_TABLE)

    def test_non_empty_well_known_restricts_to_listed_fqns(self) -> None:
        # Operator pruned the default list to suppress noise from
        # sinks they know aren't relevant in their codebase.
        resolved = resolve_sink_set(
            well_known=("subprocess.run", "yaml.load"),
        )
        self.assertEqual(
            resolved,
            {"subprocess.run": "subprocess", "yaml.load": "deserializer"},
        )

    def test_well_known_entry_not_in_default_table_is_dropped(self) -> None:
        # Operator typo'd "subprocess.runn" in their well_known
        # config — silently dropped (would have no sink_kind to
        # assign). Operators add such FQNs via `custom` instead.
        resolved = resolve_sink_set(
            well_known=("subprocess.run", "subprocess.runn"),
        )
        self.assertNotIn("subprocess.runn", resolved)
        self.assertEqual(resolved.get("subprocess.run"), "subprocess")

    def test_custom_adds_operator_defined_fqns_with_empty_kind(self) -> None:
        # Operator added a codebase-specific sink to the union;
        # sink_kind is "" since the config shape doesn't carry
        # per-FQN sink_kind today.
        resolved = resolve_sink_set(custom=("myapp.utils.run_shell",))
        self.assertEqual(resolved.get("myapp.utils.run_shell"), "")
        # Default table entries still present.
        self.assertEqual(resolved.get("subprocess.run"), "subprocess")

    def test_custom_does_not_overwrite_default_sink_kind(self) -> None:
        # Operator listed a default-table FQN under `custom` (perhaps
        # by accident). The default-table kind takes precedence —
        # `custom`'s "" doesn't overwrite "subprocess".
        resolved = resolve_sink_set(custom=("subprocess.run",))
        self.assertEqual(resolved.get("subprocess.run"), "subprocess")

    def test_custom_dedupes_against_well_known(self) -> None:
        # Operator listed "subprocess.run" in BOTH well_known AND
        # custom. The well_known's default-table kind wins; custom's
        # "" doesn't overwrite.
        resolved = resolve_sink_set(
            well_known=("subprocess.run",),
            custom=("subprocess.run", "myapp.run_shell"),
        )
        self.assertEqual(resolved.get("subprocess.run"), "subprocess")
        self.assertEqual(resolved.get("myapp.run_shell"), "")
        # No spurious entries.
        self.assertEqual(len(resolved), 2)

    def test_empty_string_custom_entries_skipped(self) -> None:
        # Defensive: comma-separated env-var parsing might produce
        # empty strings; resolve_sink_set drops them.
        resolved = resolve_sink_set(custom=("", "myapp.sink", ""))
        self.assertEqual(resolved.get("myapp.sink"), "")
        self.assertNotIn("", resolved)


class LabelSinksTests(unittest.TestCase):
    def test_matches_function_qualified_name_exactly(self) -> None:
        functions = [
            _fn(function_id="fn-1", qualified_name="subprocess.run"),
            _fn(function_id="fn-2", qualified_name="myapp.handlers.handle_request"),
            _fn(function_id="fn-3", qualified_name="os.system"),
        ]
        sink_set = resolve_sink_set()
        labels = label_sinks(functions, sink_set)
        self.assertEqual(
            labels, {"fn-1": "subprocess", "fn-3": "subprocess"}
        )
        # Non-sink function NOT in the result.
        self.assertNotIn("fn-2", labels)

    def test_unmatched_functions_absent_from_result(self) -> None:
        # Caller treats absence as `is_well_known_sink: false`.
        functions = [
            _fn(function_id="fn-1", qualified_name="myapp.foo"),
            _fn(function_id="fn-2", qualified_name="myapp.bar"),
        ]
        labels = label_sinks(functions, resolve_sink_set())
        self.assertEqual(labels, {})

    def test_empty_sink_set_returns_empty_labels(self) -> None:
        # Defensive: caller passed sink_set={} (e.g. operator pruned
        # everything). Nothing matches.
        functions = [
            _fn(function_id="fn-1", qualified_name="subprocess.run"),
        ]
        self.assertEqual(label_sinks(functions, {}), {})

    def test_custom_sink_with_empty_kind_is_labelled(self) -> None:
        # Custom-only sinks land with sink_kind="" — the labelling
        # pass still records them so SinkAuditUnit enumeration can
        # walk them.
        functions = [
            _fn(function_id="fn-1", qualified_name="myapp.run_shell"),
        ]
        sink_set = resolve_sink_set(custom=("myapp.run_shell",))
        labels = label_sinks(functions, sink_set)
        self.assertEqual(labels, {"fn-1": ""})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
