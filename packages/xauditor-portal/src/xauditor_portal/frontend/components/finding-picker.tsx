"use client";

import * as React from "react";
import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import type { FindingSummary } from "@/lib/types";

/**
 * Modal that lets the reviewer pick the canonical target for
 * a "duplicate" feedback label. Scope is limited to the same
 * run as the current finding (per ``add-duplicate-feedback-label``
 * design D1). Self and already-duplicate-labeled findings are
 * filtered out client-side so the user can't pick invalid
 * candidates (the API would 400 anyway via D2 single-level guard).
 */
export function FindingPicker({
  runId,
  excludeFindingId,
  onPick,
  onClose,
}: {
  runId: string;
  excludeFindingId: string;
  onPick: (target: { id: string; finding_id: string; name: string }) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = React.useState("");
  const [selected, setSelected] = React.useState<FindingSummary | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["picker", runId],
    queryFn: () => api.listFindings(runId),
    enabled: Boolean(runId),
  });

  const candidates = React.useMemo(() => {
    if (!data) return [];
    const filtered = data.filter(
      (f) =>
        f.id !== excludeFindingId &&
        f.feedback_label !== "duplicate",
    );
    if (!query.trim()) return filtered;
    const q = query.trim().toLowerCase();
    return filtered.filter(
      (f) =>
        f.finding_id.toLowerCase().includes(q) ||
        f.finding_name.toLowerCase().includes(q) ||
        (f.file_path ?? "").toLowerCase().includes(q),
    );
  }, [data, excludeFindingId, query]);

  React.useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  function confirm() {
    if (!selected) return;
    onPick({
      id: selected.id,
      finding_id: selected.finding_id,
      name: selected.finding_name,
    });
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="finding-picker-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="flex max-h-[80vh] w-full max-w-2xl flex-col overflow-hidden rounded-lg border border-zinc-200 bg-white shadow-xl dark:border-zinc-800 dark:bg-zinc-900">
        <div className="flex items-center justify-between border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
          <h2
            id="finding-picker-title"
            className="text-sm font-semibold text-zinc-900 dark:text-zinc-50"
          >
            Pick the canonical finding this is a duplicate of
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close picker"
            className="text-zinc-500 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-100"
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>
        <div className="border-b border-zinc-200 px-4 py-2 dark:border-zinc-800">
          <Input
            autoFocus
            placeholder="Search by id, name, or file…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <div className="flex-1 overflow-auto px-4 py-2">
          {isLoading ? (
            <p className="text-xs text-zinc-500 dark:text-zinc-400">
              Loading…
            </p>
          ) : error ? (
            <p
              role="alert"
              className="rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:bg-rose-950 dark:text-rose-200"
            >
              Failed to load findings. Try closing and reopening the picker.
            </p>
          ) : candidates.length === 0 ? (
            <p className="text-xs text-zinc-500 dark:text-zinc-400">
              No matching findings in this run.
            </p>
          ) : (
            <ul className="space-y-1">
              {candidates.map((f) => {
                const isSelected = selected?.id === f.id;
                return (
                  <li key={f.id}>
                    <button
                      type="button"
                      onClick={() => setSelected(f)}
                      onDoubleClick={() => {
                        setSelected(f);
                        onPick({
                          id: f.id,
                          finding_id: f.finding_id,
                          name: f.finding_name,
                        });
                      }}
                      className={cn(
                        "flex w-full flex-col items-start rounded-md border px-3 py-2 text-left transition-colors",
                        isSelected
                          ? "border-emerald-500 bg-emerald-50 dark:bg-emerald-950"
                          : "border-zinc-200 hover:border-zinc-400 dark:border-zinc-800 dark:hover:border-zinc-600",
                      )}
                      aria-pressed={isSelected}
                    >
                      <span className="font-mono text-xs text-zinc-600 dark:text-zinc-400">
                        {f.finding_id}
                      </span>
                      <span className="text-sm font-medium text-zinc-900 dark:text-zinc-50">
                        {f.finding_name}
                      </span>
                      {f.file_path ? (
                        <span className="text-[11px] text-zinc-500 dark:text-zinc-400">
                          {f.file_path}
                          {f.suspect_line ? `:${f.suspect_line}` : ""}
                        </span>
                      ) : null}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
        <div className="flex items-center justify-end gap-2 border-t border-zinc-200 px-4 py-3 dark:border-zinc-800">
          <Button size="sm" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            size="sm"
            onClick={confirm}
            disabled={!selected}
            aria-disabled={!selected}
          >
            Mark as duplicate of {selected?.finding_id ?? "…"}
          </Button>
        </div>
      </div>
    </div>
  );
}
