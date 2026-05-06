"""Regression tests for promoting `audit.graph_slice` from
`audit.experimental.graph_slice` to a top-level audit field with
default `true`.

`audit.experimental.graph_slice` is accepted for one minor release
with a `DeprecationWarning`; remove it from `xauditor.yml`.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.config import load_config


def _write(yaml_text: str, path: Path) -> Path:
    cfg_path = path / "xauditor.yml"
    cfg_path.write_text(yaml_text)
    return cfg_path


class GraphSlicePromotionTests(unittest.TestCase):
    def test_default_is_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write("graph:\n  db:\n    password: x\n", Path(tmp))
            cfg = load_config(repo_root=Path(tmp))
        self.assertIs(cfg.audit_mode.graph_slice, True)

    def test_top_level_false_disables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                "graph:\n  db:\n    password: x\n"
                "audit:\n  graph_slice: false\n",
                Path(tmp),
            )
            cfg = load_config(repo_root=Path(tmp))
        self.assertIs(cfg.audit_mode.graph_slice, False)

    def test_legacy_experimental_path_still_works_with_deprecation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                "graph:\n  db:\n    password: x\n"
                "audit:\n  experimental:\n    graph_slice: false\n",
                Path(tmp),
            )
            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always")
                cfg = load_config(repo_root=Path(tmp))
        self.assertIs(cfg.audit_mode.graph_slice, False)
        deprecation_msgs = [
            str(w.message)
            for w in captured
            if issubclass(w.category, DeprecationWarning)
            and "graph_slice" in str(w.message)
        ]
        self.assertEqual(len(deprecation_msgs), 1)
        self.assertIn("audit.graph_slice", deprecation_msgs[0])

    def test_top_level_wins_when_both_set(self) -> None:
        # Operator sets BOTH the new top-level and the legacy
        # experimental path. New path wins; legacy is silently
        # ignored (no deprecation since the new path is also set,
        # so the operator already knows about the new shape).
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                "graph:\n  db:\n    password: x\n"
                "audit:\n"
                "  graph_slice: true\n"
                "  experimental:\n    graph_slice: false\n",
                Path(tmp),
            )
            cfg = load_config(repo_root=Path(tmp))
        self.assertIs(cfg.audit_mode.graph_slice, True)

    def test_experimental_block_still_holds_units(self) -> None:
        # `units` is still experimental; the block didn't get
        # removed entirely.
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                "graph:\n  db:\n    password: x\n"
                "audit:\n  experimental:\n    units: true\n",
                Path(tmp),
            )
            cfg = load_config(repo_root=Path(tmp))
        self.assertIs(cfg.audit_mode.experimental.units, True)
        self.assertIs(cfg.audit_mode.graph_slice, True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
