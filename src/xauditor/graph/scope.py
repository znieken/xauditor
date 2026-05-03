from __future__ import annotations

import os
from fnmatch import fnmatch
from pathlib import Path

from xauditor.models import RepositoryScope
from xauditor.security import validate_exclude_patterns, validate_repo_path


CODE_SUFFIXES = {
    ".py",
    ".js", ".jsx", ".mjs", ".cjs",
    ".ts", ".tsx",
    ".go",
    ".java",
    ".rb",
    ".php",
    ".c", ".h",
    ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx",
    ".cs",
}
DEFAULT_IGNORED_PARTS = {".git", ".venv", "__pycache__", "node_modules", ".xauditor"}


def resolve_repository_scope(repo_root: Path, excludes: tuple[str, ...]) -> RepositoryScope:
    repo_root = validate_repo_path(repo_root)
    excludes = validate_exclude_patterns(excludes)
    included: list[Path] = []
    excluded: list[Path] = []
    for dir_path, dir_names, file_names in os.walk(repo_root):
        dir_names[:] = [name for name in dir_names if name not in DEFAULT_IGNORED_PARTS]
        current_dir = Path(dir_path)
        for name in file_names:
            suffix = os.path.splitext(name)[1]
            if suffix not in CODE_SUFFIXES:
                continue
            path = current_dir / name
            rel = path.relative_to(repo_root)
            if _is_excluded(rel, excludes):
                excluded.append(path)
                continue
            included.append(path)
    return RepositoryScope(
        repo_root=repo_root,
        excludes=tuple(excludes),
        included_files=tuple(sorted(included)),
        excluded_files=tuple(sorted(excluded)),
    )


def module_name_for_path(repo_root: Path, path: Path) -> str:
    rel = path.relative_to(repo_root)
    if rel.parent == Path("."):
        return rel.stem
    return ".".join(rel.parent.parts)


def _is_excluded(relative_path: Path, excludes: tuple[str, ...]) -> bool:
    rel_text = relative_path.as_posix()
    module_text = ".".join(relative_path.with_suffix("").parts)
    for pattern in excludes:
        if not pattern:
            continue
        if pattern in relative_path.parts:
            return True
        if rel_text == pattern or rel_text.startswith(pattern.rstrip("/") + "/"):
            return True
        if fnmatch(rel_text, pattern) or fnmatch(module_text, pattern):
            return True
    return False
