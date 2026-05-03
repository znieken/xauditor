"""Unit tests for the shared ``walk_projects`` helper.

The helper is the single source of truth for what counts as an
audit-able project under ``coder.workspace_root``. The same logic is
vendored into the coder microservice (see
``xauditor_coder_service._vendored.walk_projects``); the drift test in
``tests/test_coder_service_vendor_drift.py`` keeps them aligned.

Discovery contract (post-marker-removal): every directory at every
depth is a project, identified by its ``/``-joined path relative to
``root``. The walk has no depth cap. Filters: skip dotfile-prefixed
and ``__``-prefixed names; require ``entry.is_dir(follow_symlinks=
True)``; break symlink loops via ``os.path.realpath``. Bounded only by
``entry_budget`` and ``time_budget_seconds``.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from xauditor.integrations.coder.projects_walk import walk_projects


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


class WalkProjectsFlatLayoutTests(unittest.TestCase):
    def test_three_first_level_dirs_returns_all_three(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("alpha", "beta", "gamma"):
                _mkdir(root / name)

            names, info = walk_projects(str(root))

            self.assertEqual(names, ["alpha", "beta", "gamma"])
            self.assertFalse(info["truncated"])
            self.assertEqual(info["entries_visited"], 3)


class WalkProjectsNestedLayoutTests(unittest.TestCase):
    def test_every_directory_at_every_depth_is_a_project(self) -> None:
        """No marker required: every dir surfaces as its own project."""

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mkdir(root / "flat")
            _mkdir(root / "team" / "repo")
            _mkdir(root / "team" / "another")
            _mkdir(root / "org" / "team" / "sub" / "repo")

            names, info = walk_projects(str(root))

            self.assertIn("flat", names)
            self.assertIn("team", names)
            self.assertIn("team/repo", names)
            self.assertIn("team/another", names)
            self.assertIn("org", names)
            self.assertIn("org/team", names)
            self.assertIn("org/team/sub", names)
            self.assertIn("org/team/sub/repo", names)
            self.assertFalse(info["truncated"])

    def test_deeply_nested_dir_yields_full_path_no_depth_cap(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mkdir(root / "a" / "b" / "c" / "d" / "e")

            names, _ = walk_projects(str(root))

            self.assertIn("a", names)
            self.assertIn("a/b", names)
            self.assertIn("a/b/c", names)
            self.assertIn("a/b/c/d", names)
            self.assertIn("a/b/c/d/e", names)

    def test_files_are_not_yielded_only_directories(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mkdir(root / "team" / "repo")
            _touch(root / "team" / "README.md")
            _touch(root / "loose-file.txt")

            names, _ = walk_projects(str(root))

            self.assertIn("team", names)
            self.assertIn("team/repo", names)
            self.assertNotIn("team/README.md", names)
            self.assertNotIn("loose-file.txt", names)


class WalkProjectsBudgetTests(unittest.TestCase):
    def test_entry_budget_truncates_and_flags_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(20):
                _mkdir(root / f"d{i:02d}")

            names, info = walk_projects(str(root), entry_budget=5)

            self.assertTrue(info["truncated"])
            self.assertEqual(info["entries_visited"], 5)
            self.assertEqual(len(names), 5)

    def test_time_budget_truncates_descent(self) -> None:
        """A zero time budget aborts the walk before it can descend."""

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("alpha", "beta", "gamma"):
                _mkdir(root / name)
            _mkdir(root / "alpha" / "deep")

            names, info = walk_projects(str(root), time_budget_seconds=0.0)

            self.assertTrue(info["truncated"])
            # The exhausted budget aborts before any entry is yielded;
            # the contract is the truncated flag, not a partial listing.
            self.assertNotIn("alpha/deep", names)

    def test_deep_subtree_does_not_starve_sibling_top_level_dirs(self) -> None:
        """Phase 1 always completes: a deep first-subtree that exhausts
        the budget mid-recursion SHALL NOT prevent sibling top-level
        dirs from appearing in the listing.

        Regression test for an HGFS-bound user whose
        ``workspace_root=/home/znie/gitlab`` contained several large
        repos plus the audit target ``FortiAIGate`` at depth 1: a naive
        DFS would burn the budget inside the first repo's tree before
        ever scanning ``FortiAIGate``, causing pre-flight to fail with
        "no project named FortiAIGate" even though the directory
        existed.
        """

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            # First top-level dir has nested children that would burn
            # Phase 2's budget.
            _mkdir(root / "Repo1" / "src" / "a")
            _mkdir(root / "Repo1" / "src" / "b")
            _mkdir(root / "Repo1" / "tests")
            # Other top-level dirs are simple — they MUST still appear.
            _mkdir(root / "Repo2")
            _mkdir(root / "FortiAIGate")
            _mkdir(root / "ZetaProj")

            # Budget = exactly enough for Phase 1 to enumerate the four
            # top-level dirs with no room left for Phase 2.
            names, info = walk_projects(str(root), entry_budget=4)

            self.assertTrue(info["truncated"])
            for name in ("Repo1", "Repo2", "FortiAIGate", "ZetaProj"):
                self.assertIn(name, names)
            # Phase 2 had no remaining budget — no nested entries.
            self.assertNotIn("Repo1/src", names)
            self.assertNotIn("Repo1/tests", names)

    def test_time_budget_records_elapsed_seconds(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mkdir(root / "alpha")

            _, info = walk_projects(str(root))

            self.assertGreaterEqual(info["elapsed_seconds"], 0.0)


class WalkProjectsSymlinkLoopTests(unittest.TestCase):
    def test_symlink_loop_does_not_stall(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mkdir(root / "a")
            os.symlink(root / "a", root / "a" / "b")

            names, info = walk_projects(str(root))

            self.assertIn("a", names)
            self.assertFalse(info["truncated"])

    def test_two_paths_to_same_realpath_only_yields_once(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mkdir(root / "real")
            os.symlink(root / "real", root / "alias")

            names, _ = walk_projects(str(root))

            self.assertEqual(len(names), 1)
            self.assertIn(names[0], {"real", "alias"})


class WalkProjectsFilterTests(unittest.TestCase):
    def test_dotfile_prefixed_entries_excluded_at_every_depth(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mkdir(root / ".cache")
            _mkdir(root / "team" / ".hidden")
            _mkdir(root / "team" / "visible")

            names, _ = walk_projects(str(root))

            self.assertNotIn(".cache", names)
            self.assertNotIn("team/.hidden", names)
            self.assertIn("team/visible", names)

    def test_double_underscore_prefixed_entries_excluded(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mkdir(root / "__pycache__")
            _mkdir(root / "team" / "__build")
            _mkdir(root / "team" / "real")

            names, _ = walk_projects(str(root))

            self.assertNotIn("__pycache__", names)
            self.assertNotIn("team/__build", names)
            self.assertIn("team/real", names)


class WalkProjectsAbsentRootTests(unittest.TestCase):
    def test_missing_root_returns_empty_no_raise(self) -> None:
        names, info = walk_projects("/does/not/exist")

        self.assertEqual(names, [])
        self.assertFalse(info["truncated"])
        self.assertEqual(info["entries_visited"], 0)


if __name__ == "__main__":
    unittest.main()
