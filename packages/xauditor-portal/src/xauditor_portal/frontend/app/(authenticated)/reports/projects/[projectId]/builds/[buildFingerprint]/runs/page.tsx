"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Ban, CheckCircle2, MoreHorizontal, Trash2 } from "lucide-react";
import Link from "next/link";
import * as React from "react";
import { Breadcrumbs } from "@/components/breadcrumbs";
import { ConfirmActionDialog } from "@/components/confirm-action-dialog";
import {
  PaginationFooter,
  usePagination,
} from "@/components/paginated-list";
import { ProgressBar } from "@/components/progress-bar";
import { ModeChip, StatusChip } from "@/components/status-chip";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { ApiError, api, useMe, useProjects, useRunsForBuild } from "@/lib/api";
import type { RunsPage, RunSummary } from "@/lib/types";
import { formatTimestamp } from "@/lib/utils";

export default function RunListPage({
  params,
}: {
  params: { projectId: string; buildFingerprint: string };
}) {
  const pagination = usePagination("runs");
  const qc = useQueryClient();
  const { data, isLoading, error, refetch } = useRunsForBuild(
    params.projectId,
    params.buildFingerprint,
    { limit: pagination.pageSize, offset: pagination.offset },
  );
  const { data: projectPage } = useProjects({ limit: 200, offset: 0 });
  const project = projectPage?.items.find(
    (p) => p.project_key === params.projectId,
  );
  const { data: me } = useMe();
  const isAdmin = me?.role === "admin";

  // Optimistic delete: hide the row from the runs list cache within one
  // render frame of the API success, before the invalidate-driven refetch
  // returns. Without this the row stays visible until the next 3-second
  // auto-refresh tick and an admin who clicks the row in that window
  // navigates to a 404. The previous code invalidated ``queryKey: ["runs"]``
  // which never matched the actual ``["build-runs", ...]`` key — making
  // the post-delete refetch a no-op too. Both bugs are fixed here.
  const onDeleted = React.useCallback(
    (deletedId: string) => {
      qc.setQueriesData<RunsPage>(
        {
          queryKey: ["build-runs", params.projectId, params.buildFingerprint],
        },
        (old) => {
          if (!old) return old;
          return {
            items: old.items.filter((r) => r.id !== deletedId),
            total: Math.max(old.total - 1, 0),
          };
        },
      );
      // Page-empty: if removing this row leaves the current page with
      // zero rows AND we're not on page 1, navigate back one page so
      // the admin doesn't land on a phantom empty page.
      const before = data?.items ?? [];
      if (
        before.length === 1
        && before[0].id === deletedId
        && pagination.page > 1
      ) {
        pagination.setPage(pagination.page - 1);
      }
      qc.invalidateQueries({
        queryKey: ["build-runs", params.projectId, params.buildFingerprint],
      });
      refetch();
    },
    [
      qc,
      data,
      pagination,
      params.projectId,
      params.buildFingerprint,
      refetch,
    ],
  );

  return (
    <div className="space-y-5">
      <Breadcrumbs
        items={[
          { label: "Reports", href: "/reports" },
          {
            label: project?.project_name ?? params.projectId.slice(0, 8),
            href: `/reports/projects/${params.projectId}/builds`,
          },
          { label: `build ${params.buildFingerprint.slice(0, 12)}` },
        ]}
      />
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold tracking-tight">Audit runs</h1>
        <p className="font-mono text-xs text-zinc-500 dark:text-zinc-400">
          build {params.buildFingerprint}
        </p>
      </header>

      {error ? (
        <p
          role="alert"
          className="rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:bg-rose-950 dark:text-rose-200"
        >
          Failed to load audit runs for this build.
        </p>
      ) : null}

      {isLoading ? (
        <div className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</div>
      ) : data && data.items.length > 0 ? (
        <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900/60">
          <table className="w-full text-sm">
            <thead className="bg-zinc-50 text-xs uppercase tracking-wider text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400">
              <tr>
                <th className="px-5 py-2 text-left">Run</th>
                <th className="px-5 py-2 text-left">Mode</th>
                <th className="px-5 py-2 text-left">Status</th>
                <th className="px-5 py-2 text-left">Progress</th>
                <th className="px-5 py-2 text-left">Findings</th>
                <th className="px-5 py-2 text-left">Started / ended</th>
                {isAdmin ? (
                  <th className="px-5 py-2 text-right">Admin</th>
                ) : null}
              </tr>
            </thead>
            <tbody>
              {data.items.map((run) => (
                <tr
                  key={run.id}
                  className="border-t border-zinc-100 transition-colors hover:bg-zinc-50/70 dark:border-zinc-800 dark:hover:bg-zinc-800/40"
                >
                  <td className="px-5 py-3">
                    <Link
                      href={`/reports/runs/${run.id}`}
                      className="font-mono text-xs text-zinc-900 hover:underline dark:text-zinc-100"
                    >
                      {run.id.slice(0, 16)}
                    </Link>
                  </td>
                  <td className="px-5 py-3">
                    <ModeChip mode={run.mode} />
                  </td>
                  <td className="px-5 py-3">
                    <StatusChip status={run.status} />
                  </td>
                  <td className="px-5 py-3 w-40">
                    <div className="flex flex-col gap-1">
                      <ProgressBar
                        value={run.progress_percent}
                        label="Run progress"
                      />
                      <span className="text-xs text-zinc-500 dark:text-zinc-400 tabular-nums">
                        {run.progress_percent}%
                      </span>
                    </div>
                  </td>
                  <td className="px-5 py-3 text-xs text-zinc-700 dark:text-zinc-300">
                    <span className="font-medium">{run.total_candidates}</span>{" "}
                    total /{" "}
                    <span className="font-medium text-emerald-600 dark:text-emerald-400">
                      {run.valid_findings}
                    </span>{" "}
                    valid /{" "}
                    <span className="font-medium text-zinc-500 dark:text-zinc-400">
                      {run.false_positives}
                    </span>{" "}
                    FP
                  </td>
                  <td className="px-5 py-3 text-xs text-zinc-500 dark:text-zinc-400">
                    <div>{formatTimestamp(run.started_at)}</div>
                    {run.completed_at ? (
                      <div>→ {formatTimestamp(run.completed_at)}</div>
                    ) : null}
                  </td>
                  {isAdmin ? (
                    <td className="px-5 py-3 text-right">
                      <AdminRunActions
                        run={run}
                        projectKey={params.projectId}
                        buildFingerprint={params.buildFingerprint}
                        onChanged={() => refetch()}
                        onDeleted={onDeleted}
                      />
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
          <PaginationFooter
            scope="runs"
            total={data.total}
            pagination={pagination}
            label="runs"
          />
        </div>
      ) : (
        <EmptyState
          title="No audit runs for this build"
          description="Trigger an audit against this graph build to populate this list."
        />
      )}
    </div>
  );
}

type RunAction = "cancel" | "complete" | "delete";

const RUN_ACTION_COPY: Record<
  RunAction,
  { title: string; description: string; confirmLabel: string; destructive: boolean }
> = {
  cancel: {
    title: "Cancel run",
    description:
      "Forces the run's status to `cancelled`. Any in-flight worker will discover the change through its existing polling path. This does not stop a worker process — it only updates the persisted status.",
    confirmLabel: "Cancel run",
    destructive: false,
  },
  complete: {
    title: "Mark run complete",
    description:
      "Forces the run's status to `completed`. Use when an automated worker has stalled but you have confirmed the run actually finished.",
    confirmLabel: "Mark complete",
    destructive: false,
  },
  delete: {
    title: "Delete run",
    description:
      "Permanently deletes the run, every finding it produced, every progress event, every coverage row, and every subagent record. This cannot be undone.",
    confirmLabel: "Delete run",
    destructive: true,
  },
};

function AdminRunActions({
  run,
  projectKey,
  buildFingerprint,
  onChanged,
  onDeleted,
}: {
  run: RunSummary;
  projectKey: string;
  buildFingerprint: string;
  onChanged: () => void;
  onDeleted: (id: string) => void;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = React.useState(false);
  const [active, setActive] = React.useState<RunAction | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const onAction = (next: RunAction) => {
    setError(null);
    setActive(next);
    setOpen(false);
  };

  const onConfirm = async (reason: string | null) => {
    try {
      if (active === "cancel") {
        await api.cancelRun(run.id, reason);
        qc.invalidateQueries({
          queryKey: ["build-runs", projectKey, buildFingerprint],
        });
        onChanged();
      } else if (active === "complete") {
        await api.completeRun(run.id, reason);
        qc.invalidateQueries({
          queryKey: ["build-runs", projectKey, buildFingerprint],
        });
        onChanged();
      } else if (active === "delete") {
        await api.deleteRun(run.id);
        // Parent owns the optimistic cache update + page-back-when-empty
        // logic so the row vanishes within one render frame of the API
        // success — well before the invalidate-driven refetch returns.
        onDeleted(run.id);
      }
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : `Failed to ${active} the run.`,
      );
      // Re-throw so the dialog can also surface the error inline.
      throw err;
    }
  };

  // Disable per-action menu entries when the run is already in the
  // corresponding terminal state — the API still accepts idempotent calls,
  // but the UX is clearer when a no-op control is visibly off.
  const cancelDisabled = run.status === "cancelled";
  const completeDisabled = run.status === "completed";

  const copy = active ? RUN_ACTION_COPY[active] : null;

  return (
    <>
      <div className="relative inline-block text-left">
        <Button
          variant="ghost"
          size="icon"
          aria-label="Admin run actions"
          onClick={() => setOpen((v) => !v)}
        >
          <MoreHorizontal className="h-4 w-4" aria-hidden />
        </Button>
        {open ? (
          <div
            role="menu"
            className="absolute right-0 z-30 mt-1 w-44 overflow-hidden rounded-md border border-zinc-200 bg-white text-sm shadow-lg dark:border-zinc-700 dark:bg-zinc-900"
          >
            <MenuItem
              icon={<Ban className="h-4 w-4" aria-hidden />}
              label="Cancel run"
              onClick={() => onAction("cancel")}
              disabled={cancelDisabled}
            />
            <MenuItem
              icon={<CheckCircle2 className="h-4 w-4" aria-hidden />}
              label="Mark complete"
              onClick={() => onAction("complete")}
              disabled={completeDisabled}
            />
            <MenuItem
              icon={<Trash2 className="h-4 w-4" aria-hidden />}
              label="Delete run"
              onClick={() => onAction("delete")}
              destructive
            />
          </div>
        ) : null}
      </div>
      {active && copy ? (
        <ConfirmActionDialog
          title={copy.title}
          subtitle={
            <>
              <span className="font-medium text-zinc-700 dark:text-zinc-300">
                {run.project_name}
              </span>
              <span className="ml-1">
                · started {new Date(run.started_at).toLocaleString()}
              </span>
              <span className="ml-1">· current status: {run.status}</span>
            </>
          }
          description={
            active === "delete" && run.status === "in_progress" ? (
              <>
                <p className="mb-2 rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
                  ⚠️ This run is still in progress. The audit worker is
                  unaware of this delete and will keep running until it
                  tries to persist a finding or progress event, at which
                  point it will crash with a foreign-key violation. Any
                  LLM calls already in flight will complete and their
                  results will be lost. <strong>Cancel first</strong> and
                  wait for the worker to self-terminate before deleting.
                </p>
                {copy.description}
              </>
            ) : (
              copy.description
            )
          }
          confirmLabel={copy.confirmLabel}
          destructive={copy.destructive}
          reasonInput={active !== "delete"}
          fallbackErrorMessage={`Failed to ${active} the run.`}
          secondaryAction={
            active === "delete" && run.status === "in_progress"
              ? {
                  label: "Cancel run instead",
                  onClick: () => setActive("cancel"),
                }
              : undefined
          }
          onConfirm={onConfirm}
          onClose={() => setActive(null)}
        />
      ) : null}
      {error ? (
        <p
          role="alert"
          className="mt-1 rounded bg-rose-50 px-2 py-1 text-[11px] text-rose-700 dark:bg-rose-950 dark:text-rose-200"
        >
          {error}
        </p>
      ) : null}
    </>
  );
}

function MenuItem({
  icon,
  label,
  onClick,
  disabled = false,
  destructive = false,
}: {
  icon: React.ReactNode;
  label: string;
  onClick: () => void;
  disabled?: boolean;
  destructive?: boolean;
}) {
  return (
    <button
      type="button"
      role="menuitem"
      onClick={onClick}
      disabled={disabled}
      className={`flex w-full items-center gap-2 px-3 py-2 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
        destructive
          ? "text-rose-700 hover:bg-rose-50 dark:text-rose-300 dark:hover:bg-rose-950/40"
          : "text-zinc-700 hover:bg-zinc-50 dark:text-zinc-200 dark:hover:bg-zinc-800/60"
      }`}
    >
      {icon}
      <span>{label}</span>
    </button>
  );
}
