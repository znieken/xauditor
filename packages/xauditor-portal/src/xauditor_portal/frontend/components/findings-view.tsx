"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";
import * as React from "react";
import { useFindings } from "@/lib/api";
import { EmptyState } from "@/components/ui/empty-state";
import { FilterBar } from "@/components/filter-bar";
import { FindingCard } from "@/components/finding-card";
import {
  parseFiltersWithDefaults,
  serializeFiltersToQueryString,
} from "@/lib/findings-filters";
import type { FindingFilters } from "@/lib/types";

export function FindingsView({
  runId,
  runMode,
  runStatus,
  banner,
}: {
  runId: string;
  runMode?: "fast" | "deep";
  runStatus?: "in_progress" | "completed" | "failed" | "cancelled";
  banner?: React.ReactNode;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  // Initial filter state: parse the URL once. For dimensions with a
  // default-checked subset (validation_status, coder_status), apply the
  // default when the key is absent entirely; honor any URL-encoded value
  // (including the empty marker `?validation_status=`/`?coder_status=`)
  // verbatim — see spec.
  const [filters, setFilters] = React.useState<FindingFilters>(() =>
    parseFiltersWithDefaults(searchParams),
  );

  // Sync the chosen filter state back to the URL. The first effect run
  // materializes the FP-excluding default via replaceState (no history
  // entry) so reloads are stable; subsequent user-driven changes also use
  // replaceState (we don't want a back-button entry for every checkbox
  // toggle).
  const lastSerializedRef = React.useRef<string | null>(null);
  React.useEffect(() => {
    const qs = serializeFiltersToQueryString(filters);
    if (qs === lastSerializedRef.current) return;
    lastSerializedRef.current = qs;
    router.replace(qs ? `${pathname}?${qs}` : pathname, { scroll: false });
  }, [filters, pathname, router]);

  const [expanded, setExpanded] = React.useState<Record<string, boolean>>({});
  const { data, isLoading, error } = useFindings(runId, filters, {
    pollWhileRunning: runStatus === "in_progress",
  });

  function onToggle(id: string, next: boolean) {
    setExpanded((prev) => {
      const updated = { ...prev };
      if (next) {
        updated[id] = true;
      } else {
        delete updated[id];
      }
      return updated;
    });
  }

  function expandAll() {
    if (!data) return;
    const next: Record<string, boolean> = {};
    for (const finding of data) next[finding.id] = true;
    setExpanded(next);
  }

  function collapseAll() {
    setExpanded({});
  }

  return (
    <div className="space-y-4">
      {banner}
      <FilterBar
        value={filters}
        onChange={setFilters}
        onExpandAll={expandAll}
        onCollapseAll={collapseAll}
      />
      {error ? (
        <p
          role="alert"
          className="rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:bg-rose-950 dark:text-rose-200"
        >
          Failed to load findings.
        </p>
      ) : null}
      {isLoading ? (
        <div className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</div>
      ) : data && data.length > 0 ? (
        <div className="flex flex-col gap-2">
          {data.map((finding) => (
            <FindingCard
              key={finding.id}
              finding={finding}
              runId={runId}
              runMode={runMode}
              expanded={Boolean(expanded[finding.id])}
              onToggle={(next) => onToggle(finding.id, next)}
            />
          ))}
        </div>
      ) : (
        <EmptyState
          title="No findings match the current filters"
          description="Tweak the filter bar or reset to see the full result set."
        />
      )}
    </div>
  );
}
