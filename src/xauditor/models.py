from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ConfidenceLevel(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class CoverageState(str, Enum):
    AUDITED = "audited"
    NOT_AUDITED = "not_audited"
    EXCLUDED = "excluded"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class ValidationStatus(str, Enum):
    VALID = "Valid"
    PARTIAL_VALID = "Partial Valid"
    INCONCLUSIVE = "Inconclusive"
    FALSE_POSITIVE = "False Positive"


class GraphBuildStatus(str, Enum):
    COMPLETED = "completed"
    RESUMED = "resumed from checkpoint"
    REUSED = "reused existing index"


@dataclass(frozen=True)
class RepositoryScope:
    repo_root: Path
    excludes: tuple[str, ...]
    included_files: tuple[Path, ...]
    excluded_files: tuple[Path, ...]


@dataclass(frozen=True)
class GraphProvenance:
    file_path: str
    line_number: int
    evidence: str
    kind: str = "source"


@dataclass(frozen=True)
class ModuleRecord:
    name: str
    file_paths: tuple[str, ...]


@dataclass(frozen=True)
class FileRecord:
    path: str
    module_name: str
    language: str
    content: str


@dataclass(frozen=True)
class ClassRecord:
    class_id: str
    name: str
    file_path: str
    module_name: str
    start_line: int
    end_line: int
    summary: str = ""
    business_context: str = ""


@dataclass(frozen=True)
class ClassMemberRecord:
    member_id: str
    name: str
    class_id: str
    file_path: str
    module_name: str
    line_number: int
    source: str
    summary: str = ""


@dataclass(frozen=True)
class FunctionRecord:
    function_id: str
    name: str
    qualified_name: str
    file_path: str
    module_name: str
    start_line: int
    end_line: int
    source: str
    summary: str = ""
    business_context: str = ""
    trust_boundary: str = ""
    class_id: str | None = None
    # `capture-decorators-and-registrations` Commit 2 — stub
    # FunctionRecords for external (stdlib / third-party) calls
    # that the parser couldn't resolve to a parsed function. Set
    # by `canonical.py`'s sink-stub synthesis pass when the
    # call's target qualified_name is in the resolved
    # `audit.sinks.{well_known,custom}` set. Stub records carry
    # `source=""`, `summary=""`, etc.; their only purpose is to
    # give the sink a Function-node identity so SinkAuditUnit
    # enumeration can anchor on it.
    is_external: bool = False
    is_well_known_sink: bool = False
    # `capture-decorators-and-registrations` Commit F. True when
    # the method body assigns to `self.<attr>` (direct, annotated,
    # or augmented). Computed at parse time by Python's
    # `_detect_self_mutation`. Used by the planner's
    # StateAuditUnit emission to anchor on classes whose methods
    # collectively mutate instance state. Default False keeps
    # non-Python parsers and module-level functions inert.
    mutates_self: bool = False
    # `sink_kind` matches the sink_labelling.SINK_KINDS literal set
    # (`"subprocess"` / `"command"` / `"deserializer"` /
    # `"http_client"` / `"sql"` / `"filesystem"` / `"rendering"`),
    # or `""` for operator-defined custom sinks that didn't carry
    # an explicit kind.
    sink_kind: str = ""


@dataclass(frozen=True)
class ModuleSymbolRecord:
    symbol_id: str
    name: str
    kind: str
    module_name: str
    file_path: str
    start_line: int
    end_line: int
    type_annotation: str = ""
    value_repr: str = ""
    is_placeholder: bool = False


@dataclass(frozen=True)
class FunctionSymbolUseEdge:
    function_id: str
    symbol_id: str
    line_number: int
    evidence: str


@dataclass(frozen=True)
class DecoratorRecord:
    """One `Decorator` graph node — `capture-decorators-and-registrations`.

    Decorators are syntactic markers, not callable actors — the
    node carries source-text + framework + intent labels.
    Multiple `Function` nodes can share a `Decorator` node when
    the source uses the same expression at the same source
    location, but in practice each function declaration has its
    own decorator instances; the MERGE key is `(file_path,
    line_number, expression)` to keep re-graph-build idempotent.

    `framework` and `intent` come from
    `xauditor.graph.framework_heuristics.classify_decorator(...)`
    pattern matching against `expression`. Both are empty
    strings when no pattern matches.
    """

    decorator_id: str
    expression: str
    framework: str
    intent: str
    file_path: str
    line_number: int


@dataclass(frozen=True)
class RegistrationSiteRecord:
    """One framework registration call site —
    `capture-decorators-and-registrations` Phase 1.3.

    Holds the source-text + framework + intent labels for a
    `<callable>.add_url_rule(...)` / `path(...)` /
    `add_event_handler(...)` etc. call site. The
    `(:RegistrationSite)-[:REGISTERS]->(:Function)` edge
    (`FunctionRegistrationEdge`) connects the site to the
    function it registers.

    `framework` and `intent` mirror the values used by
    `DecoratorRecord` so downstream code (entry classification,
    GraphSlice population) can branch uniformly on either
    source. MERGE key is `(file_path, line_number, expression)`
    keeping re-graph-build idempotent.
    """

    registration_id: str
    framework: str
    intent: str
    expression: str
    file_path: str
    line_number: int


@dataclass(frozen=True)
class FunctionRegistrationEdge:
    """`(:RegistrationSite)-[:REGISTERS]->(:Function)` edge.

    Every registration site emits one edge per registered
    function. A single site that registers multiple functions
    (rare; e.g. `path('users/', views.handle_users)` only
    registers one) emits one edge per resolved function.
    """

    registration_id: str
    function_id: str


@dataclass(frozen=True)
class FunctionDecoratorEdge:
    """`(:Function)-[:HAS_DECORATOR {position}]->(:Decorator)` edge.

    `position` is 0-indexed bottom-up: position 0 is the
    decorator closest to the function definition, applied
    FIRST at runtime. Matches Python's actual application
    order so a chain like::

        @login_required          # position 2
        @app.route("/users")     # position 1
        @rate_limit(per_minute=60)  # position 0
        def list_users(): ...

    surfaces the entry-binding decorator (`@app.route`) at
    position 1 and the rate-limit at position 0 (closest to
    the function body, applied first).
    """

    function_id: str
    decorator_id: str
    position: int


@dataclass(frozen=True)
class EdgeRecord:
    source_function: str
    target_function: str
    edge_type: str
    provenance: GraphProvenance
    confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM


@dataclass(frozen=True)
class EnrichmentRecord:
    fingerprint: str
    summary: str
    business_context: str
    trust_boundary: str
    entity_kind: str = ""
    entity_id: str | None = None
    function_id: str | None = None
    class_id: str | None = None
    class_member_id: str | None = None
    path_fingerprint: str | None = None


@dataclass(frozen=True)
class PathRecord:
    entry_function: str
    function_names: tuple[str, ...]
    file_paths: tuple[str, ...]
    path_fingerprint: str
    function_ids: tuple[str, ...] = ()
    business_context: str = ""
    trust_boundary: str = ""


@dataclass(frozen=True)
class AuditUnit:
    path: PathRecord
    function_ids: tuple[str, ...]


@dataclass(frozen=True)
class AuditPlan:
    audit_units: tuple[AuditUnit, ...]


@dataclass(frozen=True)
class SourceReference:
    file_path: str
    start_line: int
    end_line: int
    focus_lines: tuple[int, ...]
    language: str
    snippet: str


# ---------------------------------------------------------------------------
# Coder verification stage data model
# ---------------------------------------------------------------------------

CODER_STATUS_VERIFIED = "Verified"
CODER_STATUS_NOT_VERIFIED = "Not Verified"
CODER_STATUS_INCONCLUSIVE = "Inconclusive"
CODER_STATUS_SKIPPED = "Skipped"
CODER_STATUS_PENDING = "Pending"
CODER_STATUS_FAIL = "Fail"

CODER_STATUSES: tuple[str, ...] = (
    CODER_STATUS_VERIFIED,
    CODER_STATUS_NOT_VERIFIED,
    CODER_STATUS_INCONCLUSIVE,
    CODER_STATUS_SKIPPED,
    CODER_STATUS_PENDING,
    CODER_STATUS_FAIL,
)

# Synonyms accepted from the Claude Code CLI response, normalised to the
# canonical CODER_STATUSES values. Keys are matched case-insensitively after
# stripping whitespace and replacing internal whitespace runs with a single
# space (so e.g. "false  positive" → "false positive").
_CODER_STATUS_SYNONYMS: dict[str, str] = {
    "verified": CODER_STATUS_VERIFIED,
    "confirmed": CODER_STATUS_VERIFIED,
    "valid": CODER_STATUS_VERIFIED,
    "true_positive": CODER_STATUS_VERIFIED,
    "true positive": CODER_STATUS_VERIFIED,
    "not_verified": CODER_STATUS_NOT_VERIFIED,
    "not verified": CODER_STATUS_NOT_VERIFIED,
    "false_positive": CODER_STATUS_NOT_VERIFIED,
    "false positive": CODER_STATUS_NOT_VERIFIED,
    "rejected": CODER_STATUS_NOT_VERIFIED,
    "invalid": CODER_STATUS_NOT_VERIFIED,
    "inconclusive": CODER_STATUS_INCONCLUSIVE,
    "uncertain": CODER_STATUS_INCONCLUSIVE,
    "unknown": CODER_STATUS_INCONCLUSIVE,
    "indeterminate": CODER_STATUS_INCONCLUSIVE,
    "skipped": CODER_STATUS_SKIPPED,
    "pending": CODER_STATUS_PENDING,
    "in_progress": CODER_STATUS_PENDING,
    "in progress": CODER_STATUS_PENDING,
}


_CLAUDE_PRODUCIBLE_STATUSES: frozenset[str] = frozenset(
    {
        CODER_STATUS_VERIFIED,
        CODER_STATUS_NOT_VERIFIED,
        CODER_STATUS_INCONCLUSIVE,
        CODER_STATUS_SKIPPED,
        CODER_STATUS_PENDING,
    }
)


def normalize_coder_status(value: object) -> str:
    """Map a raw status string to the canonical CODER_STATUSES value.

    Unrecognised inputs fall back to ``Inconclusive`` so a malformed CLI
    response is still surfaced to reviewers rather than silently dropped.
    ``Fail`` is reserved for transport-level failures the audit assigns
    directly and is NOT producible by claude — a literal ``"Fail"``
    string from claude normalises to ``Inconclusive`` so the chip
    reflects what claude actually said (or didn't).
    """

    if value is None:
        return CODER_STATUS_INCONCLUSIVE
    text = " ".join(str(value).strip().split())
    if not text:
        return CODER_STATUS_INCONCLUSIVE
    if text in _CLAUDE_PRODUCIBLE_STATUSES:
        return text
    return _CODER_STATUS_SYNONYMS.get(text.lower(), CODER_STATUS_INCONCLUSIVE)


@dataclass(frozen=True)
class CoderEvidence:
    """One piece of repo-global call-chain evidence captured by the coder agent.

    ``role`` is an open enum (``call_site``, ``definition``, ``sanitizer``,
    ``capability_check``, ``supporting``, …); the renderer treats unknown
    roles as ``supporting``.
    """

    file_path: str
    function_name: str | None
    snippet: str
    language: str | None
    role: str


@dataclass(frozen=True)
class Finding:
    finding_id: str
    finding_name: str
    finding_description: str
    confidence_level: ConfidenceLevel
    source_references: tuple[SourceReference, ...]
    analysis: str
    reason: str
    context: str
    business_context: str
    exploitation_status: str
    exploitation_steps: str
    validation_status: ValidationStatus
    validation_analysis: str
    path_fingerprint: str
    function_names: tuple[str, ...] = ()
    analyzer_status: str = ""
    evidence_strength: str = ""
    suspect_function_id: str = ""
    suspect_line: int = 0
    context_notes: str = ""
    referenced_symbols: tuple[dict[str, object], ...] = ()
    coder_status: str = CODER_STATUS_SKIPPED
    coder_analysis: str = ""
    coder_reason: str = ""
    coder_call_chain_evidence: tuple[CoderEvidence, ...] = ()
    # `wire-agentic-into-workflow` Phase 3 — populated when the
    # finding was produced under `audit.stages.form: agentic`. Each
    # entry is one stage's transport transcript (analyzer / validator
    # / exploiter), shaped per `AgentResult.transcript_as_payload`.
    # Empty tuple under prompt mode.
    agentic_transcript: tuple[dict[str, object], ...] = ()
    # `restructure-audit-modes-and-coverage` Phase 5A + workflow
    # wiring in `wire-agentic-into-workflow` — populated by the
    # cross-unit reconciler at run end. Shape: `{per_unit_verdicts,
    # consolidated_verdict, consolidation_reasoning}`. None for
    # findings emitted before reconciliation completed.
    reconciliation: dict[str, object] | None = None


@dataclass(frozen=True)
class CoverageRecord:
    category: str
    identifier: str
    state: CoverageState


@dataclass(frozen=True)
class AuditedCallChain:
    path_fingerprint: str
    entry_function: str
    function_chain: tuple[str, ...]


@dataclass
class CoverageInventory:
    records: dict[str, list[CoverageRecord]] = field(default_factory=dict)
    audited_call_chains: list[AuditedCallChain] = field(default_factory=list)

    def add(self, record: CoverageRecord) -> None:
        self.records.setdefault(record.category, []).append(record)

    def items(self, category: str) -> list[CoverageRecord]:
        return list(self.records.get(category, []))

    def counts(self, category: str) -> dict[CoverageState, int]:
        counts = {state: 0 for state in CoverageState}
        for record in self.records.get(category, []):
            counts[record.state] += 1
        return counts

    def percentage(self, category: str) -> float:
        counts = self.counts(category)
        denominator = (
            counts[CoverageState.AUDITED]
            + counts[CoverageState.NOT_AUDITED]
            + counts[CoverageState.INTERRUPTED]
            + counts[CoverageState.FAILED]
        )
        if denominator == 0:
            return 100.0
        return counts[CoverageState.AUDITED] / denominator * 100.0


@dataclass(frozen=True)
class AuditRun:
    build_fingerprint: str
    findings: tuple[Finding, ...]
    coverage: CoverageInventory
    checkpoints: dict[str, str] = field(default_factory=dict)
    shared_state: dict[str, dict[str, object]] = field(default_factory=dict)
