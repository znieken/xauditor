"use client";

import * as React from "react";
import { useRunProgress } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { formatTimestamp } from "@/lib/utils";

const TONE: Record<string, React.ComponentProps<typeof Badge>["tone"]> = {
  started: "info",
  progress: "neutral",
  stage_completed: "success",
  finished: "success",
  failed: "danger",
  resumed: "warning",
};

export function AuditLogView({ runId }: { runId: string }) {
  const { data, isLoading, error } = useRunProgress(runId);
  if (isLoading) {
    return (
      <p className="text-sm text-zinc-500 dark:text-zinc-400">
        Loading audit log…
      </p>
    );
  }
  if (error) {
    return (
      <p
        role="alert"
        className="rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:bg-rose-950 dark:text-rose-200"
      >
        Failed to load audit log.
      </p>
    );
  }
  if (!data || data.length === 0) {
    return (
      <EmptyState
        title="No progress events yet"
        description="Heartbeats start flowing as soon as the audit workflow begins."
      />
    );
  }
  return (
    <ol className="relative border-l border-zinc-200 pl-4 dark:border-zinc-800">
      {data.map((event, idx) => (
        <li
          key={idx}
          className="mb-3 ml-2 rounded-md border border-zinc-100 bg-white px-3 py-2 text-sm dark:border-zinc-800 dark:bg-zinc-900/60"
        >
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={TONE[event.heartbeat_kind] ?? "neutral"}>
              {event.heartbeat_kind}
            </Badge>
            <span className="font-mono text-xs">{event.stage}</span>
            {event.total_paths !== null ? (
              <span className="text-xs text-zinc-500 dark:text-zinc-400">
                {event.current_path_index ?? 0}/{event.total_paths}
              </span>
            ) : null}
          </div>
          {event.message ? (
            <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
              {event.message}
            </p>
          ) : null}
          <p className="mt-1 text-[11px] text-zinc-400 dark:text-zinc-500">
            {formatTimestamp(event.timestamp)}
          </p>
        </li>
      ))}
    </ol>
  );
}
