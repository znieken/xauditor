from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.errors import SecurityError
from xauditor.graph.scope import resolve_repository_scope
from xauditor.security import (
    validate_exclude_pattern,
    validate_exclude_patterns,
    validate_repo_path,
)


class SecurityInputValidationTests(unittest.TestCase):
    def test_validate_repo_path_accepts_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            resolved = validate_repo_path(Path(tmp))
            self.assertTrue(resolved.is_dir())

    def test_validate_repo_path_rejects_missing_path(self) -> None:
        with self.assertRaises(SecurityError) as ctx:
            validate_repo_path(Path("/no/such/xauditor/path"))
        self.assertIn("does not exist", str(ctx.exception))
        self.assertEqual(ctx.exception.field, "repo_path")

    def test_validate_repo_path_rejects_file(self) -> None:
        with tempfile.NamedTemporaryFile() as fp:
            with self.assertRaises(SecurityError) as ctx:
                validate_repo_path(Path(fp.name))
            self.assertIn("not a directory", str(ctx.exception))

    def test_validate_repo_path_rejects_symlink_escaping_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as sandbox, tempfile.TemporaryDirectory() as outside:
            link_path = Path(sandbox) / "escape"
            os.symlink(outside, link_path, target_is_directory=True)
            with self.assertRaises(SecurityError) as ctx:
                validate_repo_path(link_path, sandbox_root=Path(sandbox))
            self.assertIn("escapes sandbox", str(ctx.exception))

    def test_validate_exclude_pattern_accepts_safe_glob(self) -> None:
        self.assertEqual(validate_exclude_pattern("tests/*.py"), "tests/*.py")
        self.assertEqual(validate_exclude_pattern("vendor"), "vendor")
        self.assertEqual(validate_exclude_pattern("app/**/legacy_*.py"), "app/**/legacy_*.py")

    def test_validate_exclude_pattern_rejects_absolute_path(self) -> None:
        with self.assertRaises(SecurityError):
            validate_exclude_pattern("/etc/passwd")

    def test_validate_exclude_pattern_rejects_home_expansion(self) -> None:
        with self.assertRaises(SecurityError):
            validate_exclude_pattern("~/private")

    def test_validate_exclude_pattern_rejects_traversal(self) -> None:
        with self.assertRaises(SecurityError):
            validate_exclude_pattern("../etc")
        with self.assertRaises(SecurityError):
            validate_exclude_pattern("app/../etc")

    def test_validate_exclude_pattern_rejects_shell_metacharacters(self) -> None:
        with self.assertRaises(SecurityError):
            validate_exclude_pattern("vendor;rm -rf /")
        with self.assertRaises(SecurityError):
            validate_exclude_pattern("vendor $HOME")
        with self.assertRaises(SecurityError):
            validate_exclude_pattern("`whoami`")

    def test_validate_exclude_patterns_rejects_empty_entry(self) -> None:
        with self.assertRaises(SecurityError):
            validate_exclude_patterns(("ok", ""))

    def test_resolve_repository_scope_rejects_invalid_exclude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "app.py").write_text("print('x')\n", encoding="utf-8")
            with self.assertRaises(SecurityError) as ctx:
                resolve_repository_scope(Path(tmp), ("../etc",))
            self.assertEqual(ctx.exception.field, "exclude")

    def test_resolve_repository_scope_rejects_missing_repo(self) -> None:
        with self.assertRaises(SecurityError):
            resolve_repository_scope(Path("/no/such/xauditor/repo"), ())


if __name__ == "__main__":
    unittest.main()
