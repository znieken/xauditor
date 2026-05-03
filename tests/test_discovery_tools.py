from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.graph.tools import RepositoryTools, ToolValidationError
from xauditor.integrations.lsp import LanguageServerRegistry


class DiscoveryToolTests(unittest.TestCase):
    def test_ls_glob_and_read_are_scoped_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "src").mkdir()
            (repo_root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
            (repo_root / ".venv").mkdir()
            (repo_root / ".venv" / "hidden.py").write_text("ignored\n", encoding="utf-8")
            for index in range(105):
                (repo_root / "src" / f"file_{index}.py").write_text(
                    f"print({index})\n",
                    encoding="utf-8",
                )

            warmed: list[str] = []
            tools = RepositoryTools(repo_root=repo_root, lsp_warm_hook=warmed.append)
            listing = tools.ls("src")
            globbed = tools.glob("**/*.py")
            page = tools.read("src/app.py", offset=0, limit=1)

            self.assertEqual(listing["tool"], "ls")
            self.assertNotIn(".venv", listing["entries"])
            self.assertTrue(globbed["truncated"])
            self.assertEqual(len(globbed["matches"]), 100)
            self.assertEqual(page["lines"][0], "1: print('hello')")
            self.assertEqual(warmed, ["src/app.py"])

    def test_grep_and_rg_include_line_numbers_and_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "pkg").mkdir()
            (repo_root / "pkg" / "main.py").write_text(
                "def alpha():\n    return 'match'\n",
                encoding="utf-8",
            )
            (repo_root / "pkg" / "main.txt").write_text("match\n", encoding="utf-8")

            tools = RepositoryTools(repo_root=repo_root)
            grep_result = tools.grep("match", path="pkg", include="*.py")
            rg_result = tools.rg("alpha", path="pkg", include="*.py")

            self.assertEqual(grep_result["matches"][0]["line_number"], 2)
            self.assertEqual(grep_result["matches"][0]["path"], "pkg/main.py")
            self.assertEqual(rg_result["matches"][0]["line_number"], 1)
            self.assertEqual(rg_result["matches"][0]["path"], "pkg/main.py")

    def test_bash_guardrails_block_destructive_and_network_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            (repo_root / "README.md").write_text("repo\n", encoding="utf-8")
            tools = RepositoryTools(repo_root=repo_root)

            with self.assertRaises(ToolValidationError):
                tools.bash("rm -rf /tmp/bad", description="destructive")
            with self.assertRaises(ToolValidationError):
                tools.bash("curl https://example.com", description="network")

            result = tools.bash("pwd", description="show working directory")
            self.assertIn(str(repo_root), result["stdout"])

    def test_codesearch_is_labeled_as_supplemental_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            tools = RepositoryTools(
                repo_root=repo_root,
                codesearch_provider=lambda query, max_tokens: [
                    {"title": "subprocess.run", "snippet": f"docs for {query}"}
                ],
            )

            result = tools.codesearch("subprocess shell=True", max_tokens=64)

            self.assertEqual(result["tool"], "codesearch")
            self.assertEqual(result["source_label"], "supplemental external context")
            self.assertEqual(result["results"][0]["title"], "subprocess.run")

    def test_lsp_registry_reports_missing_servers_with_install_prompt(self) -> None:
        registry = LanguageServerRegistry(
            server_map={
                "python": {
                    "binary": "missing-pylsp",
                    "install": "pip install python-lsp-server",
                    "extensions": [".py"],
                }
            }
        )

        availability = registry.availability_for_paths([Path("app.py")])

        self.assertFalse(availability["python"].available)
        self.assertIn("pip install python-lsp-server", availability["python"].install_hint)


if __name__ == "__main__":
    unittest.main()
