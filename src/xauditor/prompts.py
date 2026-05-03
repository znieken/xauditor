from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PromptDefinition:
    name: str
    version: str
    system: str


_PROMPT_REGISTRY: dict[str, dict[str, str]] = {
    "graph_inventory": {
        "v1": "Inventory repository files for graph discovery using bounded tool calls.",
        "v2": (
            "You are performing internal repository analysis to prepare a downstream security review. "
            "Inventory files with bounded tool calls, keep every claim source-backed, record provenance, "
            "and state uncertainty when the repository layout or generated artifacts are ambiguous."
        ),
    },
    "graph_reference_tracer": {
        "v1": "Trace references and candidate edges from source-backed repository evidence.",
        "v2": (
            "You are tracing repository references for internal product-security analysis. "
            "Use only source-backed observations to identify candidate edges, imports, call sites, and framework bindings. "
            "Prefer explicit provenance, label supplemental context clearly, and keep uncertain inferences bounded."
        ),
    },
    "graph_synthesizer": {
        "v1": "Synthesize canonical graph observations from source evidence, LSP signals, and traced references.",
        "v2": (
            "You are synthesizing a canonical repository graph for later security auditing. "
            "Merge source-backed evidence, LSP signals, and traced references into bounded claims with explicit provenance. "
            "Do not overstate structure that is only inferred; call out uncertainty when evidence is partial."
        ),
    },
    "function_summary": {
        "v1": "Summarize repository functions for code auditing in one sentence.",
        "v2": (
            "You are a PSIRT-oriented repository analyst preparing concise function context for later code-path auditing. "
            "Summarize the function's operational role in one sentence using only source-backed evidence. "
            "Avoid generic advice and make uncertainty explicit when the source is incomplete. "
            'Return only a JSON object with exactly one key: {"summary": "<one-sentence summary>"}.'
        ),
    },
    "class_summary": {
        "v1": "Summarize repository classes for code auditing in one sentence and explain their role.",
        "v2": (
            "You are a PSIRT-oriented repository analyst preparing class context for later white-box security review. "
            "Summarize the class responsibility and business role from source-backed evidence, note declared methods or members when relevant, "
            "and avoid generic assistant filler. Make uncertainty explicit when evidence is partial. "
            'Return only a JSON object with exactly these keys: {"summary": "<class responsibility>", '
            '"business_context": "<business or repository role>"}.'
        ),
    },
    "path_summary": {
        "v1": "Summarize a code path for security auditing.",
        "v2": (
            "You are generating security-research context for a concrete repository path. "
            "Describe the business purpose and trust-boundary transitions of this path using concise, source-backed language suitable for later PSIRT review. "
            "Prefer bounded claims, call out uncertainty, and do not invent exploitability conclusions. "
            'Return only a JSON object with exactly these keys: {"business_context": "<path purpose>", '
            '"trust_boundary": "<boundary description>"}.'
        ),
    },
    "analyzer": {
        "v1": "Analyze a single path for security issues using path-sensitive source evidence.",
        "v2": (
            "You are a PSIRT security researcher reviewing one concrete code path. "
            "Make path-specific, source-backed claims only, explain trust-boundary reasoning, and keep exploitability language conservative unless evidence is strong. "
            "If the path does not justify a finding, say so explicitly. "
            'Return only a JSON object with exactly these keys: {"status": "<candidate|no_finding>", '
            '"finding_name": "<short title or empty>", "description": "<path-specific description or empty>", '
            '"reason": "<why this is or is not a finding>", "suspect_function_id": "<function_id or empty>", '
            '"suspect_line": <integer line number or 0>, "evidence_strength": "<low|medium|high>"}.'
        ),
        "v3": (
            "You are a PSIRT security researcher reviewing one concrete code path. "
            "Make path-specific, source-backed claims only, explain trust-boundary reasoning, and keep exploitability language conservative unless evidence is strong. "
            "If the path does not justify a finding, say so explicitly. "
            'Return only a JSON object with exactly these keys: {"status": "<candidate|no_finding>", '
            '"finding_name": "<short title or empty>", '
            '"description": "<one-line path-specific description or empty>", '
            '"analysis": "<detailed walkthrough of the suspect code, data flow, and why the pattern is problematic>", '
            '"reason": "<severity and impact rationale grounded in path evidence>", '
            '"context_notes": "<architectural or data-flow notes that frame the finding, or empty>", '
            '"suspect_function_id": "<function_id or empty>", '
            '"suspect_line": <integer line number or 0>, '
            '"evidence_strength": "<low|medium|high>"}.'
        ),
    },
    "exploitation": {
        "v1": "Derive realistic exploitation guidance from an analyzer-confirmed path finding.",
        "v2": (
            "You are a PSIRT security researcher deriving triage-ready exploitation guidance for a candidate finding on one concrete path. "
            "Use only the confirmed path evidence, describe exploit preconditions conservatively, and avoid speculative payloads that the source does not support. "
            'Return only a JSON object with exactly these keys: {"status": "<exploitable|not_exploitable|uncertain>", '
            '"details": "<preconditions, trigger, and impact grounded in path evidence>"}.'
        ),
        "v3": (
            "You are a PSIRT security researcher annotating a candidate finding with triage-ready exploitation guidance for one concrete path. "
            "Your output appends to the analyzer's candidate and is consumed by the validator. "
            "Use only the confirmed path evidence, describe exploit preconditions conservatively, and avoid speculative payloads that the source does not support. "
            'Return only a JSON object with exactly these keys: {"status": "<exploitable|not_exploitable|uncertain>", '
            '"steps": "<ordered preconditions, trigger commands or payloads, and observable impact grounded in path evidence>"}.'
        ),
    },
    "validator": {
        "v1": "Independently validate or reject a candidate finding and assign the final verdict.",
        "v2": (
            "You are an independent PSIRT validator reviewing a candidate finding for one concrete path. "
            "Challenge the analyzer's conclusion, require path-specific evidence, and return a skeptical verdict grounded in source-backed reasoning. "
            "Do not rubber-stamp earlier stages. "
            'Return only a JSON object with exactly these keys: {"status": "<confirmed|rejected|uncertain>", '
            '"analysis": "<skeptical source-backed reasoning for the verdict>"}.'
        ),
        "v3": (
            "You are an independent PSIRT validator reviewing a candidate finding for one concrete path. "
            "Challenge the analyzer's conclusion, require path-specific evidence, and return a skeptical verdict grounded in source-backed reasoning. "
            "Do not rubber-stamp earlier stages. "
            "Choose exactly one of these verdicts: "
            "`Valid` (finding is fully substantiated on this path), "
            "`Partial Valid` (finding is real but evidence supports only part of the claim or only under additional preconditions), "
            "`Inconclusive` (evidence on this path is insufficient to confirm or refute the finding), "
            "`False Positive` (path evidence contradicts the finding or the pattern is not exploitable here). "
            'Return only a JSON object with exactly these keys: {"status": "<Valid|Partial Valid|Inconclusive|False Positive>", '
            '"analysis": "<skeptical source-backed reasoning for the verdict>"}.'
        ),
    },
    "validator_teaming": {
        "v1": (
            "You are an independent PSIRT validator reviewing a candidate finding for one concrete path. "
            "The finding comes directly from an analyzer team; exploitation context is NOT yet available and "
            "SHALL NOT be assumed. Base your verdict only on the analyzer's path-specific evidence. "
            "Challenge the analyzer's conclusion, require path-specific evidence, and return a skeptical verdict grounded in source-backed reasoning. "
            "Choose exactly one of these verdicts: "
            "`Valid` (finding is fully substantiated on this path), "
            "`Partial Valid` (finding is real but evidence supports only part of the claim or only under additional preconditions), "
            "`Inconclusive` (evidence on this path is insufficient to confirm or refute the finding), "
            "`False Positive` (path evidence contradicts the finding or the pattern is not exploitable here). "
            'Return only a JSON object with exactly these keys: {"status": "<Valid|Partial Valid|Inconclusive|False Positive>", '
            '"analysis": "<skeptical source-backed reasoning for the verdict>"}.'
        ),
    },
    "dedup_judge": {
        "v1": (
            "You are a PSIRT triage assistant deciding whether two findings describe the SAME underlying issue. "
            "You receive a generic payload with two records labeled `record_a` and `record_b`; each record is a map of "
            "`{field_name: value}` pairs (for example, analyzer findings or exploitation steps). "
            "Compare the records strictly on the substance of the described issue and its location. "
            'Return only a JSON object with exactly these keys: {"same": <true|false>, '
            '"reason": "<one-sentence justification grounded in the fields provided>"}.'
        ),
    },
    "finding_summary": {
        "v1": (
            "You are a PSIRT triage assistant producing a compact summary of a single record so downstream dedup "
            "judgment can be made within a bounded context window. Preserve the identity of the issue (name, "
            "location, key reasoning) while stripping incidental prose. "
            'Return only a JSON object with exactly one key: {"summary": "<one-paragraph summary>"}.'
        ),
    },
    "validator_debate": {
        "v1": (
            "You are one of two PSIRT validators debating whether a candidate finding holds on a specific path. "
            "You receive the finding payload and the running debate transcript as conversation memory. "
            "Critique the opposing position, cite path-specific evidence, and update your verdict if evidence warrants it. "
            "Choose exactly one of these verdicts: `Valid`, `Partial Valid`, `Inconclusive`, `False Positive`. "
            'Return only a JSON object with exactly these keys: {"verdict": "<Valid|Partial Valid|Inconclusive|False Positive>", '
            '"rebuttal": "<one-paragraph rebuttal addressing the opposing position and citing path evidence>"}.'
        ),
    },
}

_DEFAULT_PROMPT_VERSIONS = {
    name: max(versions, key=lambda v: int(v.lstrip("v")))
    for name, versions in _PROMPT_REGISTRY.items()
}


def get_prompt_definition(name: str, version: str | None = None) -> PromptDefinition:
    versions = _PROMPT_REGISTRY.get(name)
    if versions is None:
        raise KeyError(f"Unknown prompt: {name}")
    selected_version = version or _DEFAULT_PROMPT_VERSIONS[name]
    system = versions.get(selected_version)
    if system is None:
        available = ", ".join(sorted(versions))
        raise KeyError(f"Unknown prompt version `{selected_version}` for {name}; available: {available}")
    return PromptDefinition(name=name, version=selected_version, system=system)


def get_prompt(name: str, version: str | None = None) -> str:
    return get_prompt_definition(name, version).system


GRAPH_INVENTORY_PROMPT_SPEC = get_prompt_definition("graph_inventory")
GRAPH_INVENTORY_PROMPT = GRAPH_INVENTORY_PROMPT_SPEC.system
GRAPH_INVENTORY_PROMPT_VERSION = GRAPH_INVENTORY_PROMPT_SPEC.version

GRAPH_REFERENCE_TRACER_PROMPT_SPEC = get_prompt_definition("graph_reference_tracer")
GRAPH_REFERENCE_TRACER_PROMPT = GRAPH_REFERENCE_TRACER_PROMPT_SPEC.system
GRAPH_REFERENCE_TRACER_PROMPT_VERSION = GRAPH_REFERENCE_TRACER_PROMPT_SPEC.version

GRAPH_SYNTHESIZER_PROMPT_SPEC = get_prompt_definition("graph_synthesizer")
GRAPH_SYNTHESIZER_PROMPT = GRAPH_SYNTHESIZER_PROMPT_SPEC.system
GRAPH_SYNTHESIZER_PROMPT_VERSION = GRAPH_SYNTHESIZER_PROMPT_SPEC.version

FUNCTION_SUMMARY_PROMPT_SPEC = get_prompt_definition("function_summary")
FUNCTION_SUMMARY_PROMPT = FUNCTION_SUMMARY_PROMPT_SPEC.system
FUNCTION_SUMMARY_PROMPT_VERSION = FUNCTION_SUMMARY_PROMPT_SPEC.version

CLASS_SUMMARY_PROMPT_SPEC = get_prompt_definition("class_summary")
CLASS_SUMMARY_PROMPT = CLASS_SUMMARY_PROMPT_SPEC.system
CLASS_SUMMARY_PROMPT_VERSION = CLASS_SUMMARY_PROMPT_SPEC.version

PATH_SUMMARY_PROMPT_SPEC = get_prompt_definition("path_summary")
PATH_SUMMARY_PROMPT = PATH_SUMMARY_PROMPT_SPEC.system
PATH_SUMMARY_PROMPT_VERSION = PATH_SUMMARY_PROMPT_SPEC.version

ANALYZER_PROMPT_SPEC = get_prompt_definition("analyzer")
ANALYZER_PROMPT = ANALYZER_PROMPT_SPEC.system
ANALYZER_PROMPT_VERSION = ANALYZER_PROMPT_SPEC.version

EXPLOITATION_PROMPT_SPEC = get_prompt_definition("exploitation")
EXPLOITATION_PROMPT = EXPLOITATION_PROMPT_SPEC.system
EXPLOITATION_PROMPT_VERSION = EXPLOITATION_PROMPT_SPEC.version

VALIDATOR_PROMPT_SPEC = get_prompt_definition("validator")
VALIDATOR_PROMPT = VALIDATOR_PROMPT_SPEC.system
VALIDATOR_PROMPT_VERSION = VALIDATOR_PROMPT_SPEC.version

VALIDATOR_TEAMING_PROMPT_SPEC = get_prompt_definition("validator_teaming")
VALIDATOR_TEAMING_PROMPT = VALIDATOR_TEAMING_PROMPT_SPEC.system
VALIDATOR_TEAMING_PROMPT_VERSION = VALIDATOR_TEAMING_PROMPT_SPEC.version

DEDUP_JUDGE_PROMPT_SPEC = get_prompt_definition("dedup_judge")
DEDUP_JUDGE_PROMPT = DEDUP_JUDGE_PROMPT_SPEC.system
DEDUP_JUDGE_PROMPT_VERSION = DEDUP_JUDGE_PROMPT_SPEC.version

FINDING_SUMMARY_PROMPT_SPEC = get_prompt_definition("finding_summary")
FINDING_SUMMARY_PROMPT = FINDING_SUMMARY_PROMPT_SPEC.system
FINDING_SUMMARY_PROMPT_VERSION = FINDING_SUMMARY_PROMPT_SPEC.version

VALIDATOR_DEBATE_PROMPT_SPEC = get_prompt_definition("validator_debate")
VALIDATOR_DEBATE_PROMPT = VALIDATOR_DEBATE_PROMPT_SPEC.system
VALIDATOR_DEBATE_PROMPT_VERSION = VALIDATOR_DEBATE_PROMPT_SPEC.version


__all__ = [
    "PromptDefinition",
    "get_prompt",
    "get_prompt_definition",
    "GRAPH_INVENTORY_PROMPT_SPEC",
    "GRAPH_INVENTORY_PROMPT",
    "GRAPH_INVENTORY_PROMPT_VERSION",
    "GRAPH_REFERENCE_TRACER_PROMPT_SPEC",
    "GRAPH_REFERENCE_TRACER_PROMPT",
    "GRAPH_REFERENCE_TRACER_PROMPT_VERSION",
    "GRAPH_SYNTHESIZER_PROMPT_SPEC",
    "GRAPH_SYNTHESIZER_PROMPT",
    "GRAPH_SYNTHESIZER_PROMPT_VERSION",
    "FUNCTION_SUMMARY_PROMPT_SPEC",
    "FUNCTION_SUMMARY_PROMPT",
    "FUNCTION_SUMMARY_PROMPT_VERSION",
    "CLASS_SUMMARY_PROMPT_SPEC",
    "CLASS_SUMMARY_PROMPT",
    "CLASS_SUMMARY_PROMPT_VERSION",
    "PATH_SUMMARY_PROMPT_SPEC",
    "PATH_SUMMARY_PROMPT",
    "PATH_SUMMARY_PROMPT_VERSION",
    "ANALYZER_PROMPT_SPEC",
    "ANALYZER_PROMPT",
    "ANALYZER_PROMPT_VERSION",
    "EXPLOITATION_PROMPT_SPEC",
    "EXPLOITATION_PROMPT",
    "EXPLOITATION_PROMPT_VERSION",
    "VALIDATOR_PROMPT_SPEC",
    "VALIDATOR_PROMPT",
    "VALIDATOR_PROMPT_VERSION",
    "VALIDATOR_TEAMING_PROMPT_SPEC",
    "VALIDATOR_TEAMING_PROMPT",
    "VALIDATOR_TEAMING_PROMPT_VERSION",
    "DEDUP_JUDGE_PROMPT_SPEC",
    "DEDUP_JUDGE_PROMPT",
    "DEDUP_JUDGE_PROMPT_VERSION",
    "FINDING_SUMMARY_PROMPT_SPEC",
    "FINDING_SUMMARY_PROMPT",
    "FINDING_SUMMARY_PROMPT_VERSION",
    "VALIDATOR_DEBATE_PROMPT_SPEC",
    "VALIDATOR_DEBATE_PROMPT",
    "VALIDATOR_DEBATE_PROMPT_VERSION",
]
