"""Validation helpers for user-supplied repository paths and exclude patterns.

These validators are the last line of defense before a caller resolves file paths
or iterates the repository scope. They reject path traversal, absolute paths, and
symlink escapes with a typed :class:`SecurityError` that carries an actionable
message for the CLI surface.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from xauditor.errors import SecurityError


_SAFE_EXCLUDE_CHARS = re.compile(r"^[A-Za-z0-9_./*?\-\[\]]+$")


def validate_repo_path(path: Path, *, sandbox_root: Path | None = None) -> Path:
    """Return a resolved repository path or raise :class:`SecurityError`.

    Rejects non-existent paths, non-directories, and symlinks that escape
    ``sandbox_root`` when one is supplied.
    """
    if path is None:
        raise SecurityError("Repository path is required.", field="repo_path", value="")
    try:
        resolved = path.expanduser().resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as exc:
        raise SecurityError(
            f"Repository path `{path}` does not exist or cannot be resolved.",
            field="repo_path",
            value=str(path),
        ) from exc
    if not resolved.is_dir():
        raise SecurityError(
            f"Repository path `{path}` is not a directory.",
            field="repo_path",
            value=str(path),
        )
    if sandbox_root is not None:
        try:
            resolved.relative_to(sandbox_root.resolve())
        except ValueError as exc:
            raise SecurityError(
                f"Repository path `{path}` escapes sandbox root `{sandbox_root}`.",
                field="repo_path",
                value=str(path),
            ) from exc
    return resolved


def validate_exclude_pattern(pattern: str) -> str:
    """Return a safe exclude pattern or raise :class:`SecurityError`.

    Rejects empty strings, absolute paths, path traversal (``..``), and
    characters outside the safe whitelist used by the scope resolver.
    """
    if not isinstance(pattern, str) or not pattern.strip():
        raise SecurityError(
            "Exclude pattern must be a non-empty string.",
            field="exclude",
            value=str(pattern),
        )
    stripped = pattern.strip()
    if stripped.startswith("/") or stripped.startswith("~") or (len(stripped) > 1 and stripped[1] == ":"):
        raise SecurityError(
            f"Exclude pattern `{stripped}` must be repository-relative, not absolute.",
            field="exclude",
            value=stripped,
        )
    parts = stripped.replace("\\", "/").split("/")
    if any(part == ".." for part in parts):
        raise SecurityError(
            f"Exclude pattern `{stripped}` must not contain `..` traversal segments.",
            field="exclude",
            value=stripped,
        )
    if not _SAFE_EXCLUDE_CHARS.fullmatch(stripped):
        raise SecurityError(
            f"Exclude pattern `{stripped}` contains unsupported characters.",
            field="exclude",
            value=stripped,
        )
    return stripped


def validate_exclude_patterns(patterns: Iterable[str]) -> tuple[str, ...]:
    """Validate every element of *patterns* and return a normalized tuple."""
    return tuple(validate_exclude_pattern(pattern) for pattern in patterns)


__all__ = [
    "validate_exclude_pattern",
    "validate_exclude_patterns",
    "validate_repo_path",
]
