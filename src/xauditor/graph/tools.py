from __future__ import annotations

import os
import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path, PurePath
from typing import Callable, Iterator

from xauditor.errors import ToolValidationError


DEFAULT_IGNORED_PARTS = {".git", ".venv", "__pycache__", "node_modules", ".xauditor"}


class RepositoryTools:
    def __init__(
        self,
        *,
        repo_root: Path,
        lsp_warm_hook: Callable[[str], None] | None = None,
        codesearch_provider: Callable[[str, int], list[dict[str, str]]] | None = None,
        max_results: int = 100,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.lsp_warm_hook = lsp_warm_hook
        self.codesearch_provider = codesearch_provider
        self.max_results = max_results

    def glob(self, pattern: str, path: str = ".") -> dict[str, object]:
        """Return files under `path` whose relative path matches `pattern`.

        Traversal prunes `DEFAULT_IGNORED_PARTS` during descent so huge
        ignored trees (`.git`, `.venv`, `node_modules`, …) are never
        enumerated. Results are capped at `self.max_results` and the walk
        stops as soon as the cap is reached.

        When `truncated=True` the caller has reached `max_results` and the
        walk stopped mid-traversal, so `total` equals `len(matches)` —
        it is NOT a full subtree count. When `truncated=False`, `total`
        is the exact number of matching files in the subtree.
        """

        base = self._resolve_path(path)
        matches: list[str] = []
        truncated = False
        for current_dir, rel_files in self._walk_kept(base):
            for name in rel_files:
                item = current_dir / name
                rel = item.relative_to(self.repo_root).as_posix()
                if not self._pattern_match(rel, pattern):
                    continue
                matches.append(rel)
                if len(matches) >= self.max_results:
                    truncated = True
                    break
            if truncated:
                break
        return {
            "tool": "glob",
            "matches": matches,
            "truncated": truncated,
            "total": len(matches),
        }

    def ls(self, path: str = ".", ignore: list[str] | None = None) -> dict[str, object]:
        """Return files under `path` excluding `DEFAULT_IGNORED_PARTS` and `ignore` patterns.

        Traversal prunes `DEFAULT_IGNORED_PARTS` during descent (never
        descends into them). Results are capped at `self.max_results`;
        when the cap is hit the walk stops and `truncated=True`.

        When `truncated=True`, `total` equals `len(entries)` — it is NOT
        a full subtree count. When `truncated=False`, `total` is the
        exact number of kept files in the subtree.
        """

        base = self._resolve_path(path)
        ignore_patterns = ignore or []
        entries: list[str] = []
        truncated = False
        for current_dir, rel_files in self._walk_kept(base):
            for name in rel_files:
                item = current_dir / name
                rel = item.relative_to(self.repo_root).as_posix()
                if any(fnmatch(rel, pattern) for pattern in ignore_patterns):
                    continue
                entries.append(rel)
                if len(entries) >= self.max_results:
                    truncated = True
                    break
            if truncated:
                break
        return {
            "tool": "ls",
            "entries": entries,
            "truncated": truncated,
            "total": len(entries),
        }

    def read(self, file_path: str, offset: int = 0, limit: int = 200) -> dict[str, object]:
        path = self._resolve_path(file_path)
        raw = path.read_bytes()
        if b"\0" in raw:
            raise ToolValidationError(f"Binary file reads are not allowed: {file_path}")
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        rendered = [f"{index + 1}: {line}" for index, line in enumerate(lines[offset : offset + limit], start=offset)]
        rel = path.relative_to(self.repo_root).as_posix()
        if self.lsp_warm_hook is not None:
            self.lsp_warm_hook(rel)
        return {
            "tool": "read",
            "path": rel,
            "offset": offset,
            "limit": limit,
            "lines": rendered,
            "truncated": offset + limit < len(lines),
        }

    def grep(self, pattern: str, path: str = ".", include: str | None = None) -> dict[str, object]:
        return self._search("grep", pattern, path, include)

    def rg(self, pattern: str, path: str = ".", include: str | None = None) -> dict[str, object]:
        return self._search("rg", pattern, path, include)

    def bash(
        self,
        command: str,
        *,
        workdir: str = ".",
        timeout: int = 5,
        description: str = "",
    ) -> dict[str, object]:
        lowered = command.lower()
        forbidden = (" rm ", "rm -", "curl ", "wget ", "ssh ", "nc ", "docker ", "sudo ", "mkfs", "shutdown")
        if any(token in f" {lowered}" for token in forbidden):
            raise ToolValidationError(f"Rejected unsafe bash command for {description or 'graph discovery'}")
        cwd = self._resolve_path(workdir)
        result = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdout = result.stdout[:4000]
        stderr = result.stderr[:4000]
        return {
            "tool": "bash",
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": result.returncode,
            "truncated": len(result.stdout) > len(stdout) or len(result.stderr) > len(stderr),
        }

    def codesearch(self, query: str, max_tokens: int = 400) -> dict[str, object]:
        provider = self.codesearch_provider or (lambda _query, _max_tokens: [])
        return {
            "tool": "codesearch",
            "query": query,
            "max_tokens": max_tokens,
            "source_label": "supplemental external context",
            "results": provider(query, max_tokens),
        }

    def _search(self, tool_name: str, pattern: str, path: str, include: str | None) -> dict[str, object]:
        """Search files under `path` for `pattern`, with prune-on-descent.

        `_iter_files` yields files lazily from an `os.walk` traversal that
        skips `DEFAULT_IGNORED_PARTS` during descent. The search stops as
        soon as `self.max_results` matches have been collected.
        """

        base = self._resolve_path(path)
        expression = re.compile(pattern)
        matches: list[dict[str, object]] = []
        for file_path in self._iter_files(base, include):
            for line_number, line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), start=1):
                if expression.search(line):
                    matches.append(
                        {
                            "path": file_path.relative_to(self.repo_root).as_posix(),
                            "line_number": line_number,
                            "line": line,
                        }
                    )
                    if len(matches) >= self.max_results:
                        return {
                            "tool": tool_name,
                            "matches": matches,
                            "truncated": True,
                            "warning": f"Search results truncated at {self.max_results} matches",
                        }
        return {
            "tool": tool_name,
            "matches": matches,
            "truncated": False,
        }

    def _iter_files(self, base: Path, include: str | None) -> Iterator[Path]:
        """Yield files under `base` lazily, pruning ignored directories during descent.

        Generator — callers that only need the first N matches can stop
        iterating without paying for the rest of the subtree.
        """

        for current_dir, rel_files in self._walk_kept(base):
            for name in rel_files:
                if include is not None and not fnmatch(name, include):
                    continue
                yield current_dir / name

    def _walk_kept(self, base: Path) -> Iterator[tuple[Path, list[str]]]:
        """Walk `base` with `os.walk`, pruning `DEFAULT_IGNORED_PARTS` in place.

        Yields `(current_dir_path, sorted_kept_file_names)` tuples. The
        `os.walk` caller mutates `dir_names` before descent so ignored
        subtrees are never entered.
        """

        for dir_path, dir_names, file_names in os.walk(base):
            dir_names[:] = sorted(
                name for name in dir_names if name not in DEFAULT_IGNORED_PARTS
            )
            yield Path(dir_path), sorted(file_names)

    @staticmethod
    def _pattern_match(rel: str, pattern: str) -> bool:
        """Match a posix relative path against a glob pattern.

        Supports `**` (recursive wildcard) via `PurePath.match`, which
        matches from the right and handles cross-segment `**`. Falls
        back to `fnmatch` for simple basename-style patterns.
        """

        if "**" in pattern or "/" in pattern:
            return PurePath(rel).match(pattern)
        return fnmatch(rel, pattern) or fnmatch(PurePath(rel).name, pattern)

    def _resolve_path(self, value: str) -> Path:
        target = (self.repo_root / value).resolve()
        if target != self.repo_root and self.repo_root not in target.parents:
            raise ToolValidationError(f"Path escapes repository root: {value}")
        if not target.exists():
            raise ToolValidationError(f"Path does not exist: {value}")
        return target

    def _ignored(self, path: Path) -> bool:
        rel = path.relative_to(self.repo_root)
        return any(part in DEFAULT_IGNORED_PARTS for part in rel.parts)


__all__ = ["RepositoryTools", "ToolValidationError"]
