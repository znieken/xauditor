from __future__ import annotations

from typing import Any, Mapping

from xauditor.reporting.snippets import language_for_path


def render_analyzer_markdown(payload: Mapping[str, Any]) -> str:
    sections: list[str] = ["# Audit Request: Analyzer"]
    _append_overview(sections, payload)
    _append_call_chain(sections, payload.get("call_chain"))
    _append_function_definitions(sections, payload.get("function_definitions"))
    _append_referenced_symbols(sections, payload.get("referenced_symbols"))
    return "\n\n".join(sections) + "\n"


def render_exploitation_markdown(payload: Mapping[str, Any]) -> str:
    sections: list[str] = ["# Audit Request: Exploitation"]
    _append_overview(sections, payload)
    _append_analyzer_finding(sections, payload)
    _append_call_chain(sections, payload.get("call_chain"))
    _append_function_definitions(sections, payload.get("function_definitions"))
    _append_referenced_symbols(sections, payload.get("referenced_symbols"))
    return "\n\n".join(sections) + "\n"


def render_validator_markdown(payload: Mapping[str, Any]) -> str:
    sections: list[str] = ["# Audit Request: Validator"]
    _append_overview(sections, payload)
    _append_analyzer_finding(sections, payload)
    _append_exploitation(sections, payload)
    _append_call_chain(sections, payload.get("call_chain"))
    _append_function_definitions(sections, payload.get("function_definitions"))
    _append_referenced_symbols(sections, payload.get("referenced_symbols"))
    return "\n\n".join(sections) + "\n"


def _append_overview(sections: list[str], payload: Mapping[str, Any]) -> None:
    entry = payload.get("entry_function", "")
    function_names = payload.get("function_names") or ()
    sections.append("## Overview")
    sections.append(f"- Entry function: `{entry}`")
    if function_names:
        sections.append(f"- Function chain: {' -> '.join(str(name) for name in function_names)}")


def _append_analyzer_finding(sections: list[str], payload: Mapping[str, Any]) -> None:
    finding_name = payload.get("finding_name", "")
    description = payload.get("description", "")
    reason = payload.get("reason", "")
    evidence = payload.get("evidence_strength", "")
    if not any([finding_name, description, reason, evidence]):
        return
    sections.append("## Analyzer Finding")
    if finding_name:
        sections.append(f"- Name: {finding_name}")
    if evidence:
        sections.append(f"- Evidence strength: {evidence}")
    if description:
        sections.append(f"- Description: {description}")
    if reason:
        sections.append(f"- Reason: {reason}")


def _append_exploitation(sections: list[str], payload: Mapping[str, Any]) -> None:
    status = payload.get("exploitation_status", "")
    steps = payload.get("exploitation_steps", payload.get("exploitation_details", ""))
    if not status and not steps:
        return
    sections.append("## Exploitation Result")
    if status:
        sections.append(f"- Status: {status}")
    if steps:
        sections.append(f"- Steps: {steps}")


def _append_call_chain(sections: list[str], chain: Any) -> None:
    items = list(chain or ())
    if not items:
        return
    sections.append("## Call Chain")
    for index, entry in enumerate(items, start=1):
        if not isinstance(entry, Mapping):
            sections.append(f"{index}. {entry}")
            continue
        name = entry.get("qualified_name") or entry.get("function_id", "")
        file_path = entry.get("file_path", "")
        start = entry.get("start_line", "")
        end = entry.get("end_line", "")
        location = f"{file_path}:{start}-{end}" if file_path else ""
        suffix = f" ({location})" if location else ""
        sections.append(f"{index}. `{name}`{suffix}")


def _append_function_definitions(sections: list[str], definitions: Any) -> None:
    items = list(definitions or ())
    if not items:
        return
    sections.append("## Function Definitions")
    for entry in items:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("qualified_name") or entry.get("function_id", "function")
        file_path = entry.get("file_path", "")
        start = entry.get("start_line", "")
        end = entry.get("end_line", "")
        header = f"### `{name}`"
        if file_path:
            header += f" — {file_path}:{start}-{end}"
        sections.append(header)
        source = str(entry.get("source") or "").rstrip()
        if source:
            language = entry.get("language") or language_for_path(file_path)
            sections.append(f"```{language}\n{source}\n```")
        else:
            sections.append("_Source unavailable._")


def _append_referenced_symbols(sections: list[str], symbols: Any) -> None:
    items = list(symbols or ())
    if not items:
        return
    sections.append("## Referenced Symbols")
    for entry in items:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("name", "")
        kind = entry.get("kind", "")
        module_name = entry.get("module_name", "")
        file_path = entry.get("file_path", "")
        start = entry.get("start_line", "")
        end = entry.get("end_line", "")
        type_annotation = entry.get("type_annotation", "")
        value_repr = entry.get("value_repr", "")
        is_placeholder = bool(entry.get("is_placeholder", False))
        header = f"### `{name}`"
        meta_bits: list[str] = []
        if kind:
            meta_bits.append(f"kind={kind}")
        if module_name:
            meta_bits.append(f"module={module_name}")
        if file_path:
            meta_bits.append(f"{file_path}:{start}-{end}")
        if meta_bits:
            header += " — " + ", ".join(meta_bits)
        sections.append(header)
        if type_annotation:
            sections.append(f"- Type: `{type_annotation}`")
        if value_repr:
            placeholder_note = " (placeholder)" if is_placeholder else ""
            language = language_for_path(file_path) if file_path else "text"
            sections.append(f"- Value{placeholder_note}:")
            sections.append(f"```{language}\n{value_repr}\n```")
        used_by = entry.get("used_by") or ()
        if used_by:
            sections.append("- Used by:")
            for use in used_by:
                if not isinstance(use, Mapping):
                    continue
                function_id = use.get("function_id", "")
                line_number = use.get("line_number", "")
                evidence = str(use.get("evidence", "")).strip()
                line = f"  - `{function_id}` @ line {line_number}"
                if evidence:
                    line += f" — `{evidence}`"
                sections.append(line)


__all__ = [
    "render_analyzer_markdown",
    "render_exploitation_markdown",
    "render_validator_markdown",
]
