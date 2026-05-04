"""Language-server availability checks and lightweight Python introspection.

:data:`DEFAULT_SERVER_MAP` declares one entry per language xauditor knows
about, with the binary name and a one-line install hint. The graph builder
calls :meth:`LanguageServerRegistry.availability_for_paths` to surface
"missing language server" warnings into the LLM's context, and calls
:meth:`hover` / :meth:`diagnostics` per file during graph enrichment.

Note: :meth:`hover` and :meth:`diagnostics` currently produce values only for
Python (via the stdlib ``ast`` module). For every other language they
short-circuit to ``None`` / ``[]``. Real LSP-protocol integration for the
non-Python languages is future work; for now ``DEFAULT_SERVER_MAP`` mostly
drives the capability surface seen by the LLM.
"""

from __future__ import annotations

import ast
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LSPAvailability:
    language: str
    binary: str
    available: bool
    install_hint: str


DEFAULT_SERVER_MAP = {
    "python": {
        "binary": "pylsp",
        "install": "pip install python-lsp-server",
        "extensions": [".py"],
    },
    "typescript": {
        "binary": "typescript-language-server",
        "install": "npm install -g typescript typescript-language-server",
        "extensions": [".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"],
    },
    "go": {
        "binary": "gopls",
        "install": "go install golang.org/x/tools/gopls@latest",
        "extensions": [".go"],
    },
    "c": {
        "binary": "clangd",
        "install": "Install clangd (e.g. apt install clangd or brew install llvm)",
        "extensions": [".c", ".h"],
    },
    "cpp": {
        "binary": "clangd",
        "install": "Install clangd (e.g. apt install clangd or brew install llvm)",
        "extensions": [".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".ipp"],
    },
    "csharp": {
        "binary": "csharp-ls",
        "install": "dotnet tool install --global csharp-ls",
        "extensions": [".cs"],
    },
    "java": {
        "binary": "jdtls",
        "install": "Install Eclipse JDT Language Server (jdtls)",
        "extensions": [".java"],
    },
    "kotlin": {
        "binary": "kotlin-language-server",
        "install": "brew install kotlin-language-server (macOS) or download from https://github.com/fwcd/kotlin-language-server/releases",
        "extensions": [".kt", ".kts"],
    },
    "lua": {
        "binary": "lua-language-server",
        "install": "apt/dnf/pacman/brew/winget install lua-language-server (see https://luals.github.io/)",
        "extensions": [".lua"],
    },
    "rust": {
        "binary": "rust-analyzer",
        "install": "rustup component add rust-analyzer",
        "extensions": [".rs"],
    },
    "swift": {
        "binary": "sourcekit-lsp",
        "install": "Install the Swift toolchain from https://www.swift.org/install/ (sourcekit-lsp ships with the toolchain)",
        "extensions": [".swift"],
    },
}


class LanguageServerRegistry:
    def __init__(self, server_map: dict[str, dict[str, object]] | None = None, which=shutil.which) -> None:
        self.server_map = server_map or DEFAULT_SERVER_MAP
        self.which = which

    def language_for_path(self, path: Path) -> str | None:
        for language, data in self.server_map.items():
            if path.suffix in data["extensions"]:
                return language
        return None

    def availability_for_paths(self, paths: list[Path] | tuple[Path, ...]) -> dict[str, LSPAvailability]:
        languages = sorted({language for path in paths if (language := self.language_for_path(path))})
        result: dict[str, LSPAvailability] = {}
        for language in languages:
            data = self.server_map[language]
            binary = str(data["binary"])
            result[language] = LSPAvailability(
                language=language,
                binary=binary,
                available=self.which(binary) is not None,
                install_hint=str(data["install"]),
            )
        return result

    def hover(self, path: Path, symbol: str) -> str | None:
        if self.language_for_path(path) != "python":
            return None
        tree = ast.parse(path.read_text(encoding="utf-8"))
        class_name, _, member_name = symbol.partition(".")
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                if node.name == symbol or (member_name and node.name == class_name):
                    if not member_name:
                        return ast.get_docstring(node) or f"Python class {symbol}"
                    for child in node.body:
                        if isinstance(child, ast.FunctionDef) and child.name == member_name:
                            return ast.get_docstring(child) or f"Python function {symbol}"
            if isinstance(node, ast.FunctionDef) and node.name == symbol:
                return ast.get_docstring(node) or f"Python function {symbol}"
        return None

    def diagnostics(self, path: Path) -> list[str]:
        if self.language_for_path(path) != "python":
            return []
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            return [f"{path}:{exc.lineno}: {exc.msg}"]
        return []
