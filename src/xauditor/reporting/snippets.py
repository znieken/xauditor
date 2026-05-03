from __future__ import annotations

from pathlib import Path

_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
}


def language_for_path(path: str | Path) -> str:
    suffix = Path(path).suffix
    return _LANGUAGE_BY_SUFFIX.get(suffix, suffix.lstrip(".") or "text")


def render_source_snippet(
    *,
    file_path: str,
    start_line: int,
    end_line: int,
    focus_lines: tuple[int, ...],
    language: str,
    annotations: dict[int, str] | None = None,
) -> str:
    path = Path(file_path)
    lines = path.read_text(encoding="utf-8").splitlines()
    annotations = annotations or {}
    rendered_lines = []
    for number in range(start_line, end_line + 1):
        line = lines[number - 1]
        if number in annotations:
            marker = f" <==== [{annotations[number]}]"
        elif number in focus_lines:
            marker = " <===="
        else:
            marker = ""
        rendered_lines.append(f"{number}: {line}{marker}")
    body = "\n".join(rendered_lines)
    return f"{file_path}\n```{language}\n{body}\n```"
