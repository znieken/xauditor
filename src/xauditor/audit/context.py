from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Sequence

from xauditor.models import (
    FunctionRecord,
    FunctionSymbolUseEdge,
    ModuleSymbolRecord,
)


def build_path_context(
    *,
    repo_root: Path,
    path_functions: list[FunctionRecord],
    module_symbols: Sequence[ModuleSymbolRecord] = (),
    function_symbol_uses: Sequence[FunctionSymbolUseEdge] = (),
) -> dict[str, object]:
    source_by_function_id = {
        function.function_id: _function_source(repo_root, function) for function in path_functions
    }

    path_function_ids = {function.function_id for function in path_functions}
    symbols_by_id = {symbol.symbol_id: symbol for symbol in module_symbols}
    referenced_symbol_ids: list[str] = []
    seen_symbols: set[str] = set()
    uses_by_symbol: dict[str, list[dict[str, object]]] = {}
    for use in function_symbol_uses:
        if use.function_id not in path_function_ids:
            continue
        if use.symbol_id not in symbols_by_id:
            continue
        if use.symbol_id not in seen_symbols:
            seen_symbols.add(use.symbol_id)
            referenced_symbol_ids.append(use.symbol_id)
        uses_by_symbol.setdefault(use.symbol_id, []).append(
            {
                "function_id": use.function_id,
                "line_number": use.line_number,
                "evidence": use.evidence,
            }
        )

    referenced_symbols = []
    for symbol_id in referenced_symbol_ids:
        symbol = symbols_by_id[symbol_id]
        referenced_symbols.append(
            {
                "symbol_id": symbol.symbol_id,
                "name": symbol.name,
                "kind": symbol.kind,
                "module_name": symbol.module_name,
                "file_path": symbol.file_path,
                "start_line": symbol.start_line,
                "end_line": symbol.end_line,
                "type_annotation": symbol.type_annotation,
                "value_repr": symbol.value_repr,
                "is_placeholder": symbol.is_placeholder,
                "used_by": uses_by_symbol[symbol_id],
            }
        )

    return {
        "call_chain": [
            {
                "function_id": function.function_id,
                "qualified_name": function.qualified_name,
                "file_path": function.file_path,
                "start_line": function.start_line,
                "end_line": function.end_line,
            }
            for function in path_functions
        ],
        "function_definitions": [
            {
                "function_id": function.function_id,
                "qualified_name": function.qualified_name,
                "file_path": function.file_path,
                "start_line": function.start_line,
                "end_line": function.end_line,
                "source": source_by_function_id[function.function_id],
            }
            for function in path_functions
        ],
        "referenced_symbols": referenced_symbols,
    }


def _function_source(repo_root: Path, function: FunctionRecord) -> str:
    if function.source:
        return function.source
    text = _read_text(repo_root / function.file_path)
    if not text:
        return ""
    lines = text.splitlines()
    start = max(function.start_line - 1, 0)
    end = max(function.end_line, start)
    return "\n".join(lines[start:end])


@lru_cache(maxsize=256)
def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return ""
