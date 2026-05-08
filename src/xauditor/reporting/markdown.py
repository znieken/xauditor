from __future__ import annotations

from collections.abc import Mapping

from xauditor.models import (
    CODER_STATUS_FAIL,
    CODER_STATUS_SKIPPED,
    AuditRun,
    CoderEvidence,
    CoverageInventory,
    CoverageState,
    Finding,
    ValidationStatus,
)
from xauditor.reporting.snippets import language_for_path


def _render_findings(
    findings: tuple[Finding, ...] | list[Finding],
    title: str,
    *,
    coder_enabled: bool = False,
) -> str:
    sections = [f"# {title}"]
    for finding in findings:
        sections.append(f"## {finding.finding_id} — {finding.finding_name}")
        sections.append(f"**Finding Id**: {finding.finding_id}")
        sections.append(f"**Finding Name**: {finding.finding_name}")
        sections.append(f"**Finding Description**: {finding.finding_description}")
        sections.append(f"**Confidence Level**: {finding.confidence_level.value}")
        if finding.analyzer_status:
            sections.append(f"**Analyzer Status**: {finding.analyzer_status}")
        if finding.evidence_strength:
            sections.append(f"**Evidence Strength**: {finding.evidence_strength}")
        sections.append(f"**Analysis**: {finding.analysis}")
        sections.append(f"**Reason**: {finding.reason}")
        if finding.context_notes:
            sections.append(f"**Context Notes**: {finding.context_notes}")
        if finding.suspect_function_id:
            sections.append(f"**Suspect Function Id**: {finding.suspect_function_id}")
        if finding.suspect_line:
            sections.append(f"**Suspect Line**: {finding.suspect_line}")
        sections.append(f"**Context**: {finding.context}")
        sections.append(f"**Business Context**: {finding.business_context}")
        sections.append("**Source code references**:")
        for reference in finding.source_references:
            sections.append(f"**File**: {reference.file_path}")
            sections.append("**Code**:")
            sections.append(reference.snippet)
        sections.append(f"**Exploitation Status**: {finding.exploitation_status}")
        exploitation_steps = finding.exploitation_steps or ""
        if "\n" in exploitation_steps or exploitation_steps.lstrip().startswith("#"):
            sections.append("**Exploitation Steps**:")
            sections.append(exploitation_steps)
        else:
            sections.append(f"**Exploitation Steps**: {exploitation_steps}")
        sections.append(f"**Validation Status**: {finding.validation_status.value}")
        sections.append(f"**Validation Analysis**: {finding.validation_analysis}")
        _append_referenced_symbols(sections, finding.referenced_symbols)
        _append_coder_section(sections, finding, coder_enabled=coder_enabled)
    return "\n\n".join(sections) + "\n"


def _append_coder_section(sections: list[str], finding: Finding, *, coder_enabled: bool) -> None:
    """Render the per-finding ``### Coder Verification`` block.

    Per the ``finding-reporting-and-validation`` capability the section is
    omitted only when ``coder.enabled`` was false for the run AND the
    finding's verdict is ``Skipped``. When ``coder.enabled`` was true the
    block is rendered for every finding so the audit log makes the
    absence (Pending / Skipped / Inconclusive) explicit.
    """

    is_skipped = finding.coder_status == CODER_STATUS_SKIPPED
    is_fail = finding.coder_status == CODER_STATUS_FAIL
    if is_skipped and not coder_enabled:
        return
    sections.append("### Coder Verification")
    sections.append(f"**Coder Status**: {finding.coder_status}")
    if is_fail:
        # Fail = transport-level failure. Reason carries the
        # category; analysis / evidence are intentionally absent
        # (the dispatch never reached claude).
        sections.append(
            f"**Coder Reason**: {finding.coder_reason}"
            if finding.coder_reason
            else "**Coder Reason**: _transport failure (see audit log)_"
        )
        sections.append(
            "**Coder Analysis**: _Not produced (transport failure)_"
        )
        sections.append(
            "**Coder Call-chain Evidence**: _Not produced (transport failure)_"
        )
        return
    sections.append(
        f"**Coder Analysis**: {finding.coder_analysis}"
        if finding.coder_analysis
        else "**Coder Analysis**: _Not run_"
    )
    sections.append(
        f"**Coder Reason**: {finding.coder_reason}"
        if finding.coder_reason
        else "**Coder Reason**: _Not run_"
    )
    if finding.coder_call_chain_evidence:
        sections.append("**Coder Call-chain Evidence**:")
        for index, evidence in enumerate(finding.coder_call_chain_evidence, start=1):
            sections.append(_render_coder_evidence_block(index=index, evidence=evidence))
    elif coder_enabled and not is_skipped:
        sections.append("**Coder Call-chain Evidence**: _none_")


def _render_coder_evidence_block(*, index: int, evidence: CoderEvidence) -> str:
    header_bits = [f"#### {index}. `{evidence.file_path}`"]
    extras = []
    if evidence.function_name:
        extras.append(f"function `{evidence.function_name}`")
    if evidence.role:
        extras.append(f"role={evidence.role}")
    if extras:
        header_bits.append(" — " + ", ".join(extras))
    header = "".join(header_bits)
    language = evidence.language or language_for_path(evidence.file_path) or "text"
    return header + "\n" + f"```{language}\n{evidence.snippet}\n```"


def _append_referenced_symbols(sections: list[str], symbols) -> None:
    items = list(symbols or ())
    if not items:
        return
    sections.append("### Referenced Symbols")
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
        header = f"#### `{name}`"
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


def render_findings_report(
    findings: tuple[Finding, ...] | list[Finding],
    *,
    coder_enabled: bool = False,
) -> str:
    filtered = [f for f in findings if f.validation_status != ValidationStatus.FALSE_POSITIVE]
    return _render_findings(filtered, "Findings Report", coder_enabled=coder_enabled)


def render_false_positives_report(
    findings: tuple[Finding, ...] | list[Finding],
    *,
    coder_enabled: bool = False,
    persist_false_positives: bool = True,
) -> str:
    """Render ``false-positives.md``.

    When ``persist_false_positives`` is False (the
    ``short-circuit-validator-fp`` default), the artifact SHALL still
    be emitted to preserve the "always emit both files" contract, but
    its body SHALL contain only the report header and an explanatory
    note pointing operators at the knob. FP findings are NOT rendered.
    When True, today's behavior is preserved (every FP finding rendered
    using the same per-finding structure as ``findings.md``).
    """

    if not persist_false_positives:
        return (
            "# False Positives Report\n\n"
            "_FP persistence disabled — set `audit.persist_false_positives: "
            "true` to populate this report._\n"
        )
    filtered = [f for f in findings if f.validation_status == ValidationStatus.FALSE_POSITIVE]
    return _render_findings(filtered, "False Positives Report", coder_enabled=coder_enabled)


def render_coder_results_report(
    findings: tuple[Finding, ...] | list[Finding],
    *,
    coder_enabled: bool = False,
) -> str:
    """Render the per-finding ``coder-results.md`` artifact.

    The artifact lists every finding in the run, including those whose
    coder verdict is ``Skipped`` (per the spec, the file is emitted on
    every audit run regardless of ``coder.enabled``). Skipped sections
    label the analysis / reason / evidence as "Not run" so reviewers see
    the absence explicitly.
    """

    sections = ["# Coder Verification Report"]
    if not findings:
        sections.append("_No findings were produced for this run._")
        return "\n\n".join(sections) + "\n"
    for finding in findings:
        sections.append(f"## {finding.finding_id} — {finding.finding_name}")
        sections.append(f"**Finding Id**: {finding.finding_id}")
        sections.append(f"**Path Fingerprint**: `{finding.path_fingerprint}`")
        suspect = finding.suspect_function_id or "_unresolved_"
        suspect_line = finding.suspect_line or "_unresolved_"
        sections.append(f"**Suspect**: `{suspect}` @ line {suspect_line}")
        sections.append(f"**Coder Status**: {finding.coder_status}")
        is_skipped = finding.coder_status == CODER_STATUS_SKIPPED
        is_fail = finding.coder_status == CODER_STATUS_FAIL
        if is_skipped and not coder_enabled:
            sections.append("**Coder Analysis**: _Not run (coder verification disabled for this run)_")
            sections.append("**Coder Reason**: _Not run_")
            sections.append("**Call-chain Evidence**: _Not run_")
            continue
        if is_fail:
            sections.append(
                f"**Coder Reason**: {finding.coder_reason}"
                if finding.coder_reason
                else "**Coder Reason**: _transport failure (see audit log)_"
            )
            sections.append("**Coder Analysis**: _Not produced (transport failure)_")
            sections.append("**Call-chain Evidence**: _Not produced (transport failure)_")
            continue
        sections.append(
            f"**Coder Analysis**: {finding.coder_analysis}"
            if finding.coder_analysis
            else "**Coder Analysis**: _Not run_"
        )
        sections.append(
            f"**Coder Reason**: {finding.coder_reason}"
            if finding.coder_reason
            else "**Coder Reason**: _Not run_"
        )
        if finding.coder_call_chain_evidence:
            sections.append("**Call-chain Evidence**:")
            for index, evidence in enumerate(finding.coder_call_chain_evidence, start=1):
                sections.append(_render_coder_evidence_block(index=index, evidence=evidence))
        else:
            sections.append("**Call-chain Evidence**: _none_")
    return "\n\n".join(sections) + "\n"


def render_coverage_report(coverage: CoverageInventory) -> str:
    lines = ["# Coverage Report"]
    for title, category in (
        ("Modules", "module"),
        ("Files", "file"),
        ("Functions", "function"),
        ("Paths", "path"),
    ):
        counts = coverage.counts(category)
        lines.append(
            (
                f"{title}: {coverage.percentage(category):.2f}% "
                f"({counts[CoverageState.AUDITED]} audited, "
                f"{counts[CoverageState.NOT_AUDITED]} not audited, "
                f"{counts[CoverageState.EXCLUDED]} excluded, "
                f"{counts[CoverageState.INTERRUPTED]} interrupted, "
                f"{counts[CoverageState.FAILED]} failed)"
            )
        )
    for title, category in (
        ("Audited Modules", "module"),
        ("Audited Files", "file"),
        ("Audited Functions", "function"),
        ("Unaudited Functions", "function"),
    ):
        if title == "Unaudited Functions":
            items = [record.identifier for record in coverage.items(category) if record.state == CoverageState.NOT_AUDITED]
        else:
            items = [record.identifier for record in coverage.items(category) if record.state == CoverageState.AUDITED]
        if not items:
            continue
        lines.append(f"## {title}")
        lines.extend(f"- {item}" for item in sorted(items))
    if coverage.audited_call_chains:
        lines.append("## Audited Call Chains")
        for chain in coverage.audited_call_chains:
            rendered = " -> ".join(chain.function_chain) if chain.function_chain else "(empty)"
            lines.append(f"- [{chain.path_fingerprint[:12]}] {chain.entry_function}: {rendered}")
    return "\n".join(lines) + "\n"


_STAGE_CONFIGS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "analyzer",
        "Analyzer Raw Outputs",
        (
            "status",
            "finding_name",
            "description",
            "analysis",
            "reason",
            "context_notes",
            "suspect_function_id",
            "suspect_line",
            "evidence_strength",
        ),
    ),
    ("exploitation", "Exploitation Raw Outputs", ("status", "steps")),
    ("validator", "Validator Raw Outputs", ("status", "analysis")),
)


_STAGE_ORDER = ("analyzer", "exploitation", "validator")
_STAGE_SECTION_TITLE = {
    "analyzer": "Analyzer Output",
    "exploitation": "Exploitation Output",
    "validator": "Validator Output",
}


_SAMPLING_FIELD_LABELS: tuple[str, ...] = (
    "temperature",
    "top_p",
    "top_k",
    "repetition_penalty",
)


def _sampling_value_text(value) -> str:
    if value is None:
        return "null"
    return str(value)


def _render_sampling_block(
    entry: dict,
    sections: list,
    *,
    indent: str = "",
    title: str = "Sampling",
) -> None:
    sampling = entry.get("sampling") if isinstance(entry, dict) else None
    sections.append(f"{indent}- {title}:")
    if not isinstance(sampling, dict):
        for field_name in _SAMPLING_FIELD_LABELS:
            sections.append(f"{indent}  - {field_name}: null")
        return
    for field_name in _SAMPLING_FIELD_LABELS:
        sections.append(
            f"{indent}  - {field_name}: {_sampling_value_text(sampling.get(field_name))}"
        )


def _render_stage_payload_block(stage: str, stage_payload, sections: list) -> None:
    fields = next((fields for key, _, fields in _STAGE_CONFIGS if key == stage), ())
    sections.append(f"### {_STAGE_SECTION_TITLE.get(stage, stage.title())}")
    if not isinstance(stage_payload, dict):
        sections.append(f"- Raw output: {stage_payload!r}")
        return
    for key in fields:
        if key in stage_payload:
            sections.append(f"- {key}: {stage_payload[key]}")
    extras = [key for key in stage_payload if key not in fields and key != "chain_source"]
    for key in extras:
        sections.append(f"- {key}: {stage_payload[key]}")


def _lookup_model_settings_entry(
    model_settings,
    stage: str,
):
    """Map a stage name to the model_settings entry stored in shared_state."""
    if not isinstance(model_settings, dict):
        return None
    key_map = {
        "analyzer": ("analyzer", "analyzer_subagents"),
        "exploitation": ("exploitation", "exploiter_subagents"),
        "validator": ("validator", "validator_subagents"),
    }
    for key in key_map.get(stage, (stage,)):
        if key in model_settings:
            return model_settings[key]
    return None


def _find_subagent_sampling(entry, subagent_index: int, *, finding_fp: str | None) -> dict | None:
    """Locate the model_settings entry for a specific subagent in *entry*."""
    if entry is None:
        return None
    if isinstance(entry, list):
        for item in entry:
            if isinstance(item, dict) and item.get("subagent_index") == subagent_index:
                return item
        return None
    if isinstance(entry, dict):
        if finding_fp is not None:
            finding_entries = entry.get(finding_fp)
            if isinstance(finding_entries, list):
                for item in finding_entries:
                    if isinstance(item, dict) and item.get("subagent_index") == subagent_index:
                        return item
        else:
            for finding_entries in entry.values():
                if isinstance(finding_entries, list):
                    for item in finding_entries:
                        if isinstance(item, dict) and item.get("subagent_index") == subagent_index:
                            return item
    return None


def _render_stage_sampling_entries(entry, sections: list) -> None:
    """Emit the Sampling block(s) for the given model_settings entry."""
    if entry is None:
        return
    if isinstance(entry, dict) and "sampling" in entry:
        _render_sampling_block(entry, sections)
        return
    if isinstance(entry, dict):
        for finding_fp, finding_entries in entry.items():
            if not isinstance(finding_entries, list):
                continue
            for item in finding_entries:
                if not isinstance(item, dict):
                    continue
                title = (
                    f"Sampling (finding {str(finding_fp)[-8:]} / subagent-"
                    f"{item.get('subagent_index')}-{item.get('provider_name', '')})"
                )
                _render_sampling_block(item, sections, title=title)
        return
    if isinstance(entry, list):
        for item in entry:
            if not isinstance(item, dict):
                continue
            title = (
                f"Sampling (subagent-{item.get('subagent_index')}"
                f"-{item.get('provider_name', '')})"
            )
            _render_sampling_block(item, sections, title=title)


def render_stage_report(audit_run: AuditRun, stage: str) -> str:
    title = next((label for key, label, _ in _STAGE_CONFIGS if key == stage), stage.title())
    sections = [f"# {title}", f"_Build: {audit_run.build_fingerprint}_"]
    chains_by_fp = {chain.path_fingerprint: chain for chain in audit_run.coverage.audited_call_chains}
    upstream_stages = _STAGE_ORDER[: _STAGE_ORDER.index(stage) + 1] if stage in _STAGE_ORDER else (stage,)
    for fingerprint, payload in audit_run.shared_state.items():
        stage_payload = payload.get(stage)
        if isinstance(stage_payload, dict) and stage_payload.get("status") == "skipped":
            continue
        if stage == "analyzer" and isinstance(stage_payload, dict) and stage_payload.get("status") != "candidate":
            continue
        analyzer_payload = payload.get("analyzer")
        if (
            stage != "analyzer"
            and isinstance(analyzer_payload, dict)
            and analyzer_payload.get("status") != "candidate"
        ):
            continue
        chain = chains_by_fp.get(fingerprint)
        entry = chain.entry_function if chain else "(unknown)"
        rendered_chain = " -> ".join(chain.function_chain) if chain and chain.function_chain else "(empty)"
        sections.append(f"## {entry} — {fingerprint[:12]}")
        sections.append(f"- Path fingerprint: `{fingerprint}`")
        sections.append(f"- Call chain: {rendered_chain}")
        model_settings = payload.get("model_settings") if isinstance(payload, dict) else None
        for upstream in upstream_stages:
            upstream_payload = payload.get(upstream)
            if upstream_payload is None:
                continue
            _render_stage_payload_block(upstream, upstream_payload, sections)
            entry = _lookup_model_settings_entry(model_settings, upstream)
            _render_stage_sampling_entries(entry, sections)
        if stage == "validator":
            validator_subagents = payload.get("validator_subagents") if isinstance(payload, dict) else None
            debate_map = payload.get("validator_debates") if isinstance(payload, dict) else None
            if isinstance(validator_subagents, dict) and validator_subagents:
                for finding_fp, records in validator_subagents.items():
                    if not records:
                        continue
                    sections.append(f"### Subagent Positions — Finding {finding_fp[-8:]}")
                    for record in records:
                        result = record.get("result", {}) if isinstance(record, dict) else {}
                        label = (
                            f"subagent-{record.get('subagent_index')}-{record.get('provider_name', '')}"
                            if isinstance(record, dict)
                            else "subagent-?"
                        )
                        sections.append(
                            f"- {label}: {result.get('status', '')} — {result.get('analysis', '')}"
                        )
                    debate = debate_map.get(finding_fp) if isinstance(debate_map, dict) else None
                    if debate:
                        safe_name = finding_fp.replace("/", "_").replace(":", "_")
                        sections.append(
                            f"- Debate transcript: `validator-debates/{safe_name}.md` "
                            f"(converged={debate.get('converged')}, final={debate.get('final_verdict')})"
                        )
        analyzer_for_chain = analyzer_payload if isinstance(analyzer_payload, dict) else None
        chain_source = analyzer_for_chain.get("chain_source") if analyzer_for_chain else None
        if chain_source:
            sections.append("### Call Chain Source")
            for entry in chain_source:
                if not isinstance(entry, dict):
                    continue
                label = entry.get("qualified_name") or entry.get("function_id") or "(function)"
                marker = " (suspect)" if entry.get("is_suspect") else ""
                sections.append(f"#### {label}{marker}")
                snippet = entry.get("snippet")
                if snippet:
                    sections.append(str(snippet))
    return "\n".join(sections) + "\n"


def render_subagent_stage_report(
    audit_run: AuditRun,
    stage: str,
    subagent_index: int,
) -> str:
    """Render a per-subagent view of the given stage (analyzer/validator/exploiter)."""
    stage_key_map = {
        "analyzer": ("analyzer_subagents", "Analyzer", "status finding_name description analysis reason evidence_strength suspect_function_id suspect_line".split()),
        "validator": ("validator_subagents", "Validator", "status analysis".split()),
        "exploiter": ("exploiter_subagents", "Exploiter", "status steps".split()),
    }
    if stage not in stage_key_map:
        raise ValueError(f"Unknown teaming stage `{stage}`.")
    shared_key, stage_title, fields = stage_key_map[stage]
    sections = [
        f"# {stage_title} Subagent {subagent_index}",
        f"_Build: {audit_run.build_fingerprint}_",
    ]
    chains_by_fp = {chain.path_fingerprint: chain for chain in audit_run.coverage.audited_call_chains}
    for fingerprint, payload in audit_run.shared_state.items():
        stage_payload = payload.get(shared_key)
        if not stage_payload:
            continue
        chain = chains_by_fp.get(fingerprint)
        entry_function = chain.entry_function if chain else "(unknown)"
        if stage == "analyzer":
            records = [record for record in stage_payload if record.get("subagent_index") == subagent_index]
        else:
            records = []
            for finding_fp, finding_records in stage_payload.items():
                for record in finding_records:
                    if record.get("subagent_index") == subagent_index:
                        records.append((finding_fp, record))
        if not records:
            continue
        sections.append(f"## {entry_function} — {fingerprint[:12]}")
        sections.append(f"- Path fingerprint: `{fingerprint}`")
        model_settings = payload.get("model_settings") if isinstance(payload, dict) else None
        model_entry = _lookup_model_settings_entry(model_settings, (
            "analyzer" if stage == "analyzer"
            else "exploitation" if stage == "exploiter"
            else "validator"
        ))
        if stage == "analyzer":
            subagent_sampling = _find_subagent_sampling(model_entry, subagent_index, finding_fp=None)
            for record in records:
                sections.append(
                    f"- provider: `{record.get('provider_name', '')}` / model: `{record.get('model_name', '')}`"
                )
                result = record.get("result", {}) or {}
                for key in fields:
                    if key in result:
                        sections.append(f"  - {key}: {result[key]}")
                if subagent_sampling is not None:
                    _render_sampling_block(subagent_sampling, sections)
        else:
            for finding_fp, record in records:
                sections.append(f"### Finding {finding_fp[-8:]}")
                sections.append(
                    f"- provider: `{record.get('provider_name', '')}` / model: `{record.get('model_name', '')}`"
                )
                result = record.get("result", {}) or {}
                for key in fields:
                    if key in result:
                        sections.append(f"  - {key}: {result[key]}")
                subagent_sampling = _find_subagent_sampling(
                    model_entry, subagent_index, finding_fp=finding_fp
                )
                if subagent_sampling is not None:
                    _render_sampling_block(subagent_sampling, sections)
    return "\n".join(sections) + "\n"


def render_debate_report(audit_run: AuditRun, finding_fingerprint: str) -> str:
    """Render the full debate transcript for a single finding."""
    debates = {}
    for payload in audit_run.shared_state.values():
        debate_map = payload.get("validator_debates") if isinstance(payload, dict) else None
        if isinstance(debate_map, dict):
            debates.update(debate_map)
    debate = debates.get(finding_fingerprint)
    if not debate:
        return f"# Debate Transcript — {finding_fingerprint}\n\n_(no debate recorded)_\n"
    sections = [
        f"# Debate Transcript — {finding_fingerprint}",
        f"- Configured max rounds: {debate.get('max_rounds')}",
        f"- Final verdict: {debate.get('final_verdict')}",
        f"- Converged: {debate.get('converged')}",
    ]
    initial = debate.get("initial_verdicts") or {}
    if initial:
        sections.append("## Initial Verdicts")
        for subagent_index, verdict in sorted(initial.items(), key=lambda item: str(item[0])):
            sections.append(f"- subagent-{subagent_index}: {verdict}")
    turns = debate.get("turns") or []
    rounds: dict[int, list[dict]] = {}
    for turn in turns:
        rounds.setdefault(int(turn.get("round_index", 0)), []).append(turn)
    for round_index in sorted(rounds):
        sections.append(f"## Round {round_index}")
        for turn in rounds[round_index]:
            label = f"subagent-{turn.get('subagent_index')}-{turn.get('provider_name', '')}"
            sections.append(f"### {label}")
            sections.append("**System prompt**")
            sections.append("```text")
            sections.append(str(turn.get("system_prompt", "")))
            sections.append("```")
            sections.append("**User message**")
            sections.append("```text")
            sections.append(str(turn.get("user_message", "")))
            sections.append("```")
            sections.append("**Raw response**")
            sections.append("```text")
            sections.append(str(turn.get("raw_response", "")))
            sections.append("```")
            sections.append(f"- verdict: `{turn.get('verdict', '')}`")
            sections.append(f"- rebuttal: {turn.get('rebuttal', '')}")
    return "\n".join(sections) + "\n"


def teaming_subagent_indices(audit_run: AuditRun, stage: str) -> list[int]:
    key_map = {
        "analyzer": "analyzer_subagents",
        "validator": "validator_subagents",
        "exploiter": "exploiter_subagents",
    }
    shared_key = key_map.get(stage)
    if shared_key is None:
        return []
    seen: set[int] = set()
    for payload in audit_run.shared_state.values():
        shared = payload.get(shared_key) if isinstance(payload, dict) else None
        if shared is None:
            continue
        if stage == "analyzer" and isinstance(shared, list):
            for record in shared:
                if isinstance(record, dict) and "subagent_index" in record:
                    seen.add(int(record["subagent_index"]))
        elif isinstance(shared, dict):
            for record_list in shared.values():
                if not isinstance(record_list, list):
                    continue
                for record in record_list:
                    if isinstance(record, dict) and "subagent_index" in record:
                        seen.add(int(record["subagent_index"]))
    return sorted(seen)


def teaming_debate_fingerprints(audit_run: AuditRun) -> list[str]:
    fingerprints: list[str] = []
    for payload in audit_run.shared_state.values():
        if not isinstance(payload, dict):
            continue
        debates = payload.get("validator_debates")
        if isinstance(debates, dict):
            fingerprints.extend(debates.keys())
    return fingerprints


def render_no_findings_report(audit_run: AuditRun) -> str:
    """Render paths that analyzer cleared (no candidate finding, downstream agents not run)."""
    sections = ["# No-Finding Paths", f"_Build: {audit_run.build_fingerprint}_"]
    chains_by_fp = {chain.path_fingerprint: chain for chain in audit_run.coverage.audited_call_chains}
    for fingerprint, payload in audit_run.shared_state.items():
        exploitation = payload.get("exploitation")
        validator = payload.get("validator")
        exploitation_skipped = isinstance(exploitation, dict) and exploitation.get("status") == "skipped"
        validator_skipped = isinstance(validator, dict) and validator.get("status") == "skipped"
        if not (exploitation_skipped or validator_skipped):
            continue
        chain = chains_by_fp.get(fingerprint)
        entry = chain.entry_function if chain else "(unknown)"
        rendered_chain = " -> ".join(chain.function_chain) if chain and chain.function_chain else "(empty)"
        analyzer = payload.get("analyzer") if isinstance(payload.get("analyzer"), dict) else {}
        sections.append(f"## {entry} — {fingerprint[:12]}")
        sections.append(f"- Path fingerprint: `{fingerprint}`")
        sections.append(f"- Call chain: {rendered_chain}")
        sections.append(f"- Analyzer status: {analyzer.get('status', '(unknown)')}")
        if analyzer.get("reason"):
            sections.append(f"- Analyzer reason: {analyzer['reason']}")
        skipped_stages = []
        if exploitation_skipped:
            skipped_stages.append("exploitation")
        if validator_skipped:
            skipped_stages.append("validator")
        sections.append(f"- Skipped stages: {', '.join(skipped_stages)}")
    return "\n".join(sections) + "\n"
