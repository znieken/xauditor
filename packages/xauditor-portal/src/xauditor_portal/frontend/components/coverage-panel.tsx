"use client";

import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { CoverageGaps } from "@/lib/types";

// `portal-coverage-panel`: per-run Coverage panel rendered above
// the findings list on the run-detail page. Three groups
// (audited / skipped_by_mode / out_of_scope) plus the fast-mode
// CTA banner with a `audit.mode: deep` re-run prompt.
//
// Default expanded state:
// - fast mode + non-empty `skipped_by_mode` → expanded (CTA visible)
// - deep mode → collapsed (no CTA value to surface)
// - `coverageGaps === null` → "n/a — pre-Phase-5 audit" placeholder
//   instead of the panel

interface CoveragePanelProps {
  coverageGaps?: CoverageGaps | null;
  runMode: "fast" | "deep" | string;
  runId: string;
}

export function CoveragePanel({
  coverageGaps,
  runMode,
  runId,
}: CoveragePanelProps) {
  if (coverageGaps === null || coverageGaps === undefined) {
    return <PreRenamePlaceholder />;
  }

  const showCta =
    runMode === "fast" && coverageGaps.skipped_by_mode.length > 0;
  const [open, setOpen] = React.useState<boolean>(showCta);

  return (
    <Card className="mb-4">
      <CardHeader className="cursor-pointer" onClick={() => setOpen(!open)}>
        <div className="flex items-center justify-between">
          <div>
            <h3 className="font-mono text-sm font-medium text-zinc-900 dark:text-zinc-100">
              Coverage
            </h3>
            <p className="text-xs text-zinc-500 dark:text-zinc-400">
              {coverageGaps.audited_classes.length} audited ·{" "}
              {coverageGaps.skipped_by_mode.length} skipped ·{" "}
              {coverageGaps.out_of_scope.length} out of scope
            </p>
          </div>
          <button
            type="button"
            className="font-mono text-[11px] text-zinc-500 hover:text-zinc-900 dark:hover:text-zinc-100"
            aria-expanded={open}
          >
            {open ? "▼ collapse" : "▶ expand"}
          </button>
        </div>
      </CardHeader>
      {open ? (
        <CardContent className="space-y-3">
          {showCta ? (
            <CoverageCtaBanner
              advice={coverageGaps.advice_to_user}
              skippedCount={coverageGaps.skipped_by_mode.length}
              runId={runId}
            />
          ) : null}
          <CoverageGroup
            label="Audited"
            classes={coverageGaps.audited_classes}
            tone="success"
          />
          {coverageGaps.skipped_by_mode.length > 0 ? (
            <CoverageGroup
              label="Skipped (deep mode would cover)"
              classes={coverageGaps.skipped_by_mode}
              tone="warning"
            />
          ) : null}
          <CoverageGroup
            label="Out of scope (use other tools)"
            classes={coverageGaps.out_of_scope}
            tone="neutral"
          />
        </CardContent>
      ) : null}
    </Card>
  );
}

interface CoverageGroupProps {
  label: string;
  classes: string[];
  tone: "success" | "warning" | "neutral";
}

function CoverageGroup({ label, classes, tone }: CoverageGroupProps) {
  return (
    <div className="space-y-1.5">
      <h4
        className={cn(
          "font-mono text-[11px] uppercase tracking-wide",
          tone === "success" && "text-emerald-600 dark:text-emerald-400",
          tone === "warning" && "text-amber-600 dark:text-amber-400",
          tone === "neutral" && "text-zinc-500 dark:text-zinc-400",
        )}
      >
        {label}
      </h4>
      <div className="flex flex-wrap gap-1.5">
        {classes.map((cls) => (
          <Badge key={cls} tone={tone === "success" ? "success" : tone === "warning" ? "warning" : "neutral"}>
            {cls}
          </Badge>
        ))}
      </div>
    </div>
  );
}

interface CoverageCtaBannerProps {
  advice: string;
  skippedCount: number;
  runId: string;
}

function CoverageCtaBanner({
  advice,
  skippedCount,
  runId,
}: CoverageCtaBannerProps) {
  // Per-run dismissal lives in localStorage so closing the banner
  // on this run doesn't leak to other runs.
  const dismissalKey = `coverage-cta-dismissed-${runId}`;
  const [dismissed, setDismissed] = React.useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem(dismissalKey) === "1";
  });

  if (dismissed) return null;

  function copyDeepCommand() {
    const cmd = "xauditor audit run --mode deep";
    if (navigator.clipboard) {
      void navigator.clipboard.writeText(cmd);
    }
  }

  function dismiss() {
    if (typeof window !== "undefined") {
      window.localStorage.setItem(dismissalKey, "1");
    }
    setDismissed(true);
  }

  return (
    <div className="rounded-md border border-amber-300 bg-amber-50 p-3 dark:border-amber-700 dark:bg-amber-950/40">
      <div className="flex items-start gap-3">
        <div className="flex-1 space-y-1">
          <p className="text-sm font-medium text-amber-900 dark:text-amber-100">
            ⚠ {skippedCount} vulnerability{" "}
            {skippedCount === 1 ? "class was" : "classes were"} skipped
          </p>
          <p className="text-xs text-amber-800 dark:text-amber-200">
            {advice}
          </p>
        </div>
        <div className="flex flex-col gap-1.5 self-stretch">
          <button
            type="button"
            onClick={copyDeepCommand}
            className="rounded bg-amber-600 px-2 py-1 font-mono text-[11px] text-white hover:bg-amber-700"
          >
            Copy deep command
          </button>
          <button
            type="button"
            onClick={dismiss}
            className="rounded border border-amber-300 px-2 py-1 font-mono text-[11px] text-amber-900 hover:bg-amber-100 dark:border-amber-700 dark:text-amber-100 dark:hover:bg-amber-900"
          >
            Dismiss
          </button>
        </div>
      </div>
    </div>
  );
}

function PreRenamePlaceholder() {
  return (
    <div className="mb-4 rounded-md border border-zinc-200 bg-zinc-50 px-3 py-2 dark:border-zinc-800 dark:bg-zinc-900/40">
      <p className="text-xs text-zinc-500 dark:text-zinc-400">
        <span className="font-mono">Coverage:</span> n/a — pre-Phase-5
        audit. Re-run to populate Coverage data.
      </p>
    </div>
  );
}
