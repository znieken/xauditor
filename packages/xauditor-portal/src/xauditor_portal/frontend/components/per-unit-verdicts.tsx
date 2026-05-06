"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { PerUnitVerdict, Reconciliation } from "@/lib/types";

// `portal-coverage-panel`: per-finding Per-Unit Verdicts panel.
// Renders directly under the existing Validation Status /
// Validation Analysis lines on the finding card whenever the
// finding's `reconciliation` JSONB is non-null.
//
// Phase 5A baseline runs (PassthroughReconciler, single-unit
// groups) write NULL to the column, so this component returns
// null for path-only audits. It lights up when the agentic
// reconciler from `agentic-stage-runner-real` consolidates
// multi-unit findings.

interface PerUnitVerdictsPanelProps {
  reconciliation?: Reconciliation | null;
}

export function PerUnitVerdictsPanel({
  reconciliation,
}: PerUnitVerdictsPanelProps) {
  if (reconciliation === null || reconciliation === undefined) {
    return null;
  }
  if (
    !reconciliation.per_unit_verdicts ||
    reconciliation.per_unit_verdicts.length === 0
  ) {
    return null;
  }

  return (
    <section className="mt-3 rounded-md border border-zinc-200 bg-zinc-50 p-3 dark:border-zinc-800 dark:bg-zinc-900/40">
      <h4 className="font-mono text-[11px] uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
        Per-Unit Verdicts
      </h4>
      <ul className="mt-2 space-y-1.5">
        {reconciliation.per_unit_verdicts.map((verdict) => (
          <PerUnitRow
            key={`${verdict.unit_kind}::${verdict.unit_id}`}
            verdict={verdict}
          />
        ))}
      </ul>
      {reconciliation.consolidation_reasoning ? (
        <div className="mt-3 border-t border-zinc-200 pt-2 dark:border-zinc-800">
          <p className="font-mono text-[11px] uppercase tracking-wide text-zinc-500 dark:text-zinc-400">
            Consolidated reasoning
          </p>
          <p className="mt-1 text-xs text-zinc-700 dark:text-zinc-300">
            {reconciliation.consolidation_reasoning}
          </p>
        </div>
      ) : null}
    </section>
  );
}

interface PerUnitRowProps {
  verdict: PerUnitVerdict;
}

function PerUnitRow({ verdict }: PerUnitRowProps) {
  const [expanded, setExpanded] = React.useState<boolean>(false);
  const truncated =
    verdict.analysis.length > 80
      ? `${verdict.analysis.slice(0, 80)}…`
      : verdict.analysis;

  return (
    <li
      className="cursor-pointer rounded border border-zinc-200 bg-white px-2 py-1 hover:bg-zinc-100 dark:border-zinc-800 dark:bg-zinc-900 dark:hover:bg-zinc-800"
      onClick={() => setExpanded(!expanded)}
    >
      <div className="flex items-center gap-2">
        <Badge tone="neutral">{verdict.unit_kind}</Badge>
        <Badge tone={verdictTone(verdict.verdict)}>{verdict.verdict}</Badge>
        <span className="flex-1 truncate text-xs text-zinc-700 dark:text-zinc-300">
          {expanded ? verdict.analysis : truncated}
        </span>
      </div>
      {expanded && verdict.unit_id ? (
        <p className="mt-1 font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
          unit_id: {verdict.unit_id}
        </p>
      ) : null}
    </li>
  );
}

function verdictTone(
  verdict: string,
): React.ComponentProps<typeof Badge>["tone"] {
  switch (verdict) {
    case "Valid":
      return "success";
    case "Partial Valid":
      return "info";
    case "Inconclusive":
      return "warning";
    case "False Positive":
    case "Refuted":
      return "danger";
    default:
      return "neutral";
  }
}
