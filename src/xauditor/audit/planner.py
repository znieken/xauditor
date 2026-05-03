from __future__ import annotations

from xauditor.audit.source import AuditGraphSource
from xauditor.llm import LLMClient
from xauditor.models import AuditPlan, AuditUnit, PathRecord


def plan_audit_paths(
    source: AuditGraphSource,
    llm_client: LLMClient | None = None,
) -> AuditPlan:
    audit_units: list[AuditUnit] = []
    seen_fingerprints: set[str] = set()
    for path in source.iter_paths():
        if path.path_fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(path.path_fingerprint)
        function_ids = path.function_ids
        business_context = path.business_context
        trust_boundary = path.trust_boundary
        if llm_client is not None and (not business_context or not trust_boundary):
            enrichment = llm_client.summarize_path(path.entry_function, path.function_names)
            business_context = business_context or enrichment["business_context"]
            trust_boundary = trust_boundary or enrichment["trust_boundary"]
        audit_units.append(
            AuditUnit(
                path=PathRecord(
                    entry_function=path.entry_function,
                    function_names=path.function_names,
                    file_paths=path.file_paths,
                    path_fingerprint=path.path_fingerprint,
                    function_ids=function_ids,
                    business_context=business_context,
                    trust_boundary=trust_boundary,
                ),
                function_ids=function_ids,
            )
        )
    return AuditPlan(audit_units=tuple(audit_units))
