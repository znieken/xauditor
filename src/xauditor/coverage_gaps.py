"""CoverageGaps — what each audit run actually covered.

`restructure-audit-modes-and-coverage` Phase 5A introduces the
`CoverageGaps` structure: a per-run JSONB blob naming the
vulnerability classes the active mode + unit selection
**audited**, the classes a different mode would have audited
(`skipped_by_mode`), and the classes outside xauditor's scope
altogether (`out_of_scope`).

The structure is the answer to the operator's reasonable
question after seeing "247 units audited, 3 findings": what
**didn't** the audit look at? Without an explicit answer,
"tool found nothing" gets misread as "no vulnerabilities
exist". The `Coverage Gaps` section makes the silence honest.

This module is deliberately data-only (a closed taxonomy + a
mode-aware projection). It is consumed by:

- `services.run_audit` end-of-run (writes the JSONB into
  `audit_runs.coverage_gaps`)
- `reporting/markdown.py` (renders a `## Coverage Gaps`
  section)
- `reporting/exporter.py` (surfaces a top-level
  `coverage_gaps` field in the JSON export envelope)
- `xauditor_portal/api/runs.py` (exposes the field on the
  run-detail response so the portal can render a
  Coverage panel)

Phase 5A class taxonomy below; new vulnerability classes can
be appended as the AuditUnit kinds gain enumeration logic
(SinkAuditUnit / EntryAuditUnit / etc.).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterable, Literal


VulnerabilityClass = Literal[
    # Path / Sink covered (both modes once the relevant unit kind ships)
    "sql_injection",
    "command_injection",
    "ssrf",
    "path_traversal",
    "deserialization",
    "xss",
    "xxe",
    "sink_convergence",
    "entry_exposure",
    # Deep-only when their unit kinds light up
    "state_machine_violation",
    "toctou_race",
    "cross_process_taint",
    "configuration_misuse",
    # Out of scope for xauditor — point operators at other tools
    "supply_chain",
    "business_logic_idor",
    "cryptographic_primitives",
    "race_condition",
    "unknown_unknowns",
]


# ----- Audit-coverage taxonomy --------------------------------------------
# `_AUDITED_BY_UNIT_KIND` maps each unit kind to the vulnerability
# classes it can plausibly catch. The mode-aware computation below
# unions across the active unit set, then derives `skipped_by_mode` as
# "what the OTHER mode's units would add" and `out_of_scope` as the
# closed set of classes no mode covers.
#
# The unit-kind-to-class mapping reflects what each unit kind is
# **designed** to surface, not what it has actually surfaced. Operators
# reading the section should read it as "the audit looked for these
# classes" rather than "the audit guarantees no findings of these
# classes exist".

_AUDITED_BY_UNIT_KIND: dict[str, tuple[VulnerabilityClass, ...]] = {
    "path": (
        "sql_injection",
        "command_injection",
        "ssrf",
        "path_traversal",
        "deserialization",
        "xss",
        "xxe",
    ),
    "sink": ("sink_convergence",),
    "entry": ("entry_exposure",),
    "state": ("state_machine_violation", "toctou_race"),
    "boundary": ("cross_process_taint",),
    "config": ("configuration_misuse",),
}

# Classes xauditor does NOT audit at all. Always present in
# `out_of_scope` regardless of mode / unit selection. Each class
# pairs with a one-line "use $X tool instead" pointer in
# `_OUT_OF_SCOPE_ADVICE` consumed by `advice_to_user`.
_OUT_OF_SCOPE: tuple[VulnerabilityClass, ...] = (
    "supply_chain",
    "business_logic_idor",
    "cryptographic_primitives",
    "race_condition",
    "unknown_unknowns",
)

_OUT_OF_SCOPE_ADVICE: dict[VulnerabilityClass, str] = {
    "supply_chain": "Use OSV / SCA tooling for dependency-graph CVE checks.",
    "business_logic_idor": "Business-logic IDOR requires human review.",
    "cryptographic_primitives": "Run a dedicated crypto-primitive audit (e.g. cryptosense).",
    "race_condition": "Concurrency races need targeted dynamic-analysis tooling.",
    "unknown_unknowns": "By definition undetectable; pair with manual code review.",
}

# Default unit selections per mode. Stays here (not in
# `AuditModeConfig`) because (a) the unit selection is informational
# for the report, not behavioural for the workflow, and (b) future
# mode presets / unit-kind additions can extend the table without
# touching the config layer.
_MODE_DEFAULT_UNITS: dict[str, tuple[str, ...]] = {
    "fast": ("path", "sink", "entry"),
    "deep": ("path", "sink", "entry", "state", "boundary", "config"),
}


@dataclass(frozen=True)
class CoverageGaps:
    """Per-run coverage manifest.

    All fields are JSON-friendly so the dataclass round-trips
    cleanly into the `audit_runs.coverage_gaps` JSONB column.
    """

    audited_classes: tuple[VulnerabilityClass, ...]
    skipped_by_mode: tuple[VulnerabilityClass, ...]
    out_of_scope: tuple[VulnerabilityClass, ...]
    mode: str
    advice_to_user: str

    def to_payload(self) -> dict[str, object]:
        return {
            "audited_classes": list(self.audited_classes),
            "skipped_by_mode": list(self.skipped_by_mode),
            "out_of_scope": list(self.out_of_scope),
            "mode": self.mode,
            "advice_to_user": self.advice_to_user,
        }

    def to_markdown_section(self) -> str:
        """Render the `## Coverage Gaps` Markdown block."""
        lines: list[str] = ["## Coverage Gaps", ""]
        lines.append("This audit covered:")
        if self.audited_classes:
            for cls in self.audited_classes:
                lines.append(f"- `{cls}`")
        else:
            lines.append("- (none)")
        lines.append("")
        if self.skipped_by_mode:
            lines.append("This audit did NOT cover (a different mode would):")
            for cls in self.skipped_by_mode:
                lines.append(f"- `{cls}`")
            lines.append("")
        lines.append("Outside xauditor's scope (use other tools):")
        for cls in self.out_of_scope:
            advice = _OUT_OF_SCOPE_ADVICE.get(cls, "")
            if advice:
                lines.append(f"- `{cls}` — {advice}")
            else:
                lines.append(f"- `{cls}`")
        if self.advice_to_user:
            lines.append("")
            lines.append(f"_{self.advice_to_user}_")
        lines.append("")
        return "\n".join(lines)


def _resolve_mode_units(
    mode: str, override_units: Iterable[str] | None = None
) -> tuple[str, ...]:
    """Resolve the active unit set for a given mode.

    `override_units` (when supplied) wins over the mode preset —
    operators who manually select `audit.units` get exactly that
    set in the report.
    """

    if override_units is not None:
        return tuple(override_units)
    return _MODE_DEFAULT_UNITS.get(mode, _MODE_DEFAULT_UNITS["fast"])


def compute_coverage_gaps(
    *,
    mode: str,
    units: Iterable[str] | None = None,
) -> CoverageGaps:
    """Compute the `CoverageGaps` for the given mode + unit selection.

    `mode` is the resolved mode literal (`"fast"` or `"deep"`).
    `units` is the resolved unit kind set; when `None`, defaults
    to the mode's preset.

    The math:
    - `audited_classes` = union of `_AUDITED_BY_UNIT_KIND[unit]`
      across `units`.
    - `skipped_by_mode` = the OTHER mode's audited set MINUS the
      current `audited_classes`. Empty when the current selection
      already covers everything any mode covers (i.e. operator
      explicitly enabled all six units).
    - `out_of_scope` = `_OUT_OF_SCOPE` (always all of them).
    - `advice_to_user` is mode-aware and operator-actionable.
    """

    active_units = _resolve_mode_units(mode, units)
    audited: list[VulnerabilityClass] = []
    seen: set[str] = set()
    for unit_kind in active_units:
        for cls in _AUDITED_BY_UNIT_KIND.get(unit_kind, ()):
            if cls in seen:
                continue
            seen.add(cls)
            audited.append(cls)

    other_mode = "deep" if mode == "fast" else "fast"
    other_units = _resolve_mode_units(other_mode, None)
    other_audited: set[VulnerabilityClass] = set()
    for unit_kind in other_units:
        for cls in _AUDITED_BY_UNIT_KIND.get(unit_kind, ()):
            other_audited.add(cls)
    skipped = tuple(cls for cls in other_audited if cls not in seen)

    if mode == "fast" and skipped:
        advice = (
            "Run with `audit.mode: deep` to also cover "
            + ", ".join(f"`{cls}`" for cls in skipped)
            + "."
        )
    elif mode == "deep" and skipped:
        # Operator manually trimmed the deep unit set below the
        # default — name the missing kinds.
        advice = (
            "Some deep-mode unit kinds were excluded; restore the "
            "full deep preset or set `audit.units` to the missing "
            "kinds to recover this coverage."
        )
    else:
        advice = (
            "Out-of-scope classes need other tooling (see the "
            "list below)."
        )

    return CoverageGaps(
        audited_classes=tuple(audited),
        skipped_by_mode=skipped,
        out_of_scope=_OUT_OF_SCOPE,
        mode=mode,
        advice_to_user=advice,
    )


__all__ = [
    "CoverageGaps",
    "VulnerabilityClass",
    "compute_coverage_gaps",
]
