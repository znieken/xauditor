"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Ban, CheckCircle2, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";
import { Breadcrumbs } from "@/components/breadcrumbs";
import { ConfirmActionDialog } from "@/components/confirm-action-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AuditLogView } from "@/components/audit-log-view";
import { CoverageView } from "@/components/coverage-view";
import { CoveragePanel } from "@/components/coverage-panel";
import { FindingsView } from "@/components/findings-view";
import { ModeChip, StagesFormChip, StatusChip } from "@/components/status-chip";
import { ProgressBar } from "@/components/progress-bar";
import { cn, formatTimestamp } from "@/lib/utils";
import { ApiError, api, useMe, useProjects, useRun } from "@/lib/api";
import type { FeedbackBreakdown, RunDetail, RunsPage } from "@/lib/types";

const FP_METRIC_TOOLTIP =
  "Counts findings whose latest reviewer feedback is `false positive`. The agent's `validation_status` field does not contribute.";

type SubTab = "findings" | "coverage" | "audit-log";

const SUB_TABS: { id: SubTab; label: string }[] = [
  { id: "findings", label: "Findings" },
  { id: "coverage", label: "Coverage" },
  { id: "audit-log", label: "Audit Log" },
];

export default function RunDetailPage({
  params,
}: {
  params: { runId: string };
}) {
  const { data: run, isLoading, error } = useRun(params.runId);
  const [tab, setTab] = React.useState<SubTab>("findings");
  const { data: me } = useMe();
  const isAdmin = me?.role === "admin";
  // Best-effort project lookup so the breadcrumb shows the real name.
  const { data: projectPage } = useProjects({ limit: 200, offset: 0 });
  const project = React.useMemo(() => {
    if (!run || !projectPage) return undefined;
    return projectPage.items.find(
      (p) => p.repo_root === run.repo_root && p.project_name === run.project_name,
    );
  }, [projectPage, run]);

  if (isLoading) {
    return (
      <p className="text-sm text-zinc-500 dark:text-zinc-400">Loading run…</p>
    );
  }
  if (error || !run) {
    return (
      <div className="space-y-3">
        <Link
          href="/reports"
          className="inline-flex items-center gap-1 text-xs text-zinc-500 hover:underline dark:text-zinc-400"
        >
          <ArrowLeft className="h-3.5 w-3.5" aria-hidden /> Back to projects
        </Link>
        <p
          role="alert"
          className="rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:bg-rose-950 dark:text-rose-200"
        >
          Could not load this run.
        </p>
      </div>
    );
  }

  const breadcrumbs = [
    { label: "Reports", href: "/reports" },
    project
      ? {
          label: project.project_name,
          href: `/reports/projects/${project.project_key}/builds`,
        }
      : { label: run.project_name },
    project
      ? {
          label: `build ${run.build_fingerprint.slice(0, 12)}`,
          href: `/reports/projects/${project.project_key}/builds/${run.build_fingerprint}/runs`,
        }
      : { label: `build ${run.build_fingerprint.slice(0, 12)}` },
    { label: `run ${run.id.slice(0, 12)}` },
  ];

  const running = run.status === "in_progress";

  return (
    <div className="space-y-5">
      <Breadcrumbs items={breadcrumbs} />
      <Card>
        <CardHeader className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
          <div className="space-y-1">
            <CardTitle className="text-lg">{run.project_name}</CardTitle>
            <p className="font-mono text-xs text-zinc-500 dark:text-zinc-400">
              {run.repo_root}
            </p>
            <p className="font-mono text-[11px] text-zinc-400 dark:text-zinc-500">
              build {run.build_fingerprint.slice(0, 16)}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <ModeChip mode={run.mode} />
            <StagesFormChip stages_form={run.stages_form} />
            <StatusChip status={run.status} />
            {isAdmin ? (
              <AdminRunHeaderActions
                run={run}
                projectKey={project?.project_key}
                buildFingerprint={run.build_fingerprint}
              />
            ) : null}
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-1" aria-label="Overall run progress">
            <div className="flex items-center justify-between text-xs">
              <span className="font-medium text-zinc-700 dark:text-zinc-200">
                Overall audit progress
              </span>
              <span className="tabular-nums text-zinc-600 dark:text-zinc-300">
                {run.progress_percent}%
              </span>
            </div>
            <ProgressBar
              value={run.progress_percent}
              label="Overall audit progress"
            />
            {running ? (
              <p className="text-[11px] text-zinc-500 dark:text-zinc-400">
                refreshing every 3 seconds
              </p>
            ) : null}
          </div>
          <div className="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
            <MetricWithFeedback
              label="Valid findings"
              breakdown={run.valid_findings_breakdown}
              tone="success"
            />
            <Metric
              label="False positives"
              value={run.false_positives}
              tone="muted"
              titleAttr={FP_METRIC_TOOLTIP}
            />
            <Metric
              label="Duplicates"
              value={run.duplicate_findings ?? 0}
            />
            <Metric label="Unlabeled" value={run.unlabeled_findings} />
            <Metric
              label="Positive rate"
              value={formatValidRate(run.valid_rate)}
              titleAttr="Agent precision after reviewer feedback: post-feedback Valid count divided by the agent's Valid-side pool (Valid / Partial Valid / Inconclusive). Values above 100% mean the reviewer promoted more False Positive findings to true_positive than they downgraded Valid to false_positive."
            />
          </div>
          <div className="grid grid-cols-1 gap-3 text-xs text-zinc-500 dark:text-zinc-400 md:grid-cols-3">
            <div>
              <div className="uppercase tracking-wider">Started</div>
              <div className="text-zinc-800 dark:text-zinc-200">
                {formatTimestamp(run.started_at)}
              </div>
            </div>
            <div>
              <div className="uppercase tracking-wider">Ended</div>
              <div className="text-zinc-800 dark:text-zinc-200">
                {formatTimestamp(run.completed_at)}
              </div>
            </div>
            <div>
              <div className="uppercase tracking-wider">LLM providers</div>
              <div className="font-mono text-[11px] text-zinc-800 dark:text-zinc-200">
                {Object.entries(run.llm_providers_used ?? {}).length === 0
                  ? "—"
                  : Object.entries(run.llm_providers_used ?? {})
                      .map(([k, v]) => `${k}: ${formatProviderValue(v)}`)
                      .join(" · ")}
              </div>
            </div>
          </div>
        </CardContent>
      </Card>

      <nav
        aria-label="Run sub-tabs"
        className="flex flex-wrap gap-1 border-b border-zinc-200 dark:border-zinc-800"
      >
        {SUB_TABS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            onClick={() => setTab(entry.id)}
            aria-selected={tab === entry.id}
            className={cn(
              "-mb-px border-b-2 px-3 py-1.5 text-sm transition-colors",
              tab === entry.id
                ? "border-zinc-900 font-medium text-zinc-900 dark:border-zinc-100 dark:text-zinc-100"
                : "border-transparent text-zinc-500 hover:text-zinc-900 dark:text-zinc-400 dark:hover:text-zinc-200",
            )}
          >
            {entry.label}
          </button>
        ))}
      </nav>

      <section>
        {tab === "findings" ? (
          <>
            <CoveragePanel
              coverageGaps={run.coverage_gaps}
              runMode={run.mode}
              runId={run.id}
            />
            <FindingsView
              runId={run.id}
              runMode={run.mode}
              runStatus={run.status}
            />
          </>
        ) : null}
        {tab === "coverage" ? (
          <CoverageView runId={run.id} runStatus={run.status} />
        ) : null}
        {tab === "audit-log" ? <AuditLogView runId={run.id} /> : null}
      </section>
    </div>
  );
}

/**
 * Render an ``llm_providers_used`` value as a single line.
 *
 * Strings pass through. Objects pick a sensible primary field
 * (``version`` first, then ``model``, then ``name``) before falling
 * back to JSON. Without this guard, structured manifest entries
 * (e.g. ``RunMeta.llm_providers_used["coder"]`` historically a dict)
 * render as ``[object Object]``.
 */
function formatProviderValue(v: unknown): string {
  if (typeof v === "string") return v;
  if (v == null) return "—";
  if (typeof v === "object") {
    const obj = v as Record<string, unknown>;
    for (const key of ["version", "model", "name"]) {
      const candidate = obj[key];
      if (typeof candidate === "string" && candidate) return candidate;
    }
    try {
      return JSON.stringify(v);
    } catch {
      return "[unserialisable]";
    }
  }
  return String(v);
}

function Metric({
  label,
  value,
  tone,
  titleAttr,
}: {
  label: string;
  value: number | string;
  tone?: "success" | "muted";
  titleAttr?: string;
}) {
  return (
    <div
      className="rounded-lg border border-zinc-200 bg-white px-3 py-2 dark:border-zinc-800 dark:bg-zinc-900/60"
      title={titleAttr}
    >
      <div className="text-[11px] uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
        {label}
      </div>
      <div
        className={cn(
          "text-xl font-semibold tabular-nums",
          tone === "success" && "text-emerald-600 dark:text-emerald-400",
          tone === "muted" && "text-zinc-500 dark:text-zinc-400",
        )}
      >
        {value}
      </div>
    </div>
  );
}

function MetricWithFeedback({
  label,
  breakdown,
  tone,
}: {
  label: string;
  breakdown: FeedbackBreakdown;
  tone?: "success" | "muted";
}) {
  const hasDelta =
    breakdown.added_by_feedback > 0 ||
    breakdown.removed_by_feedback > 0 ||
    breakdown.duplicates_in_bucket > 0;
  return (
    <div className="rounded-lg border border-zinc-200 bg-white px-3 py-2 dark:border-zinc-800 dark:bg-zinc-900/60">
      <div className="text-[11px] uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
        {label}
      </div>
      <div
        className={cn(
          "text-xl font-semibold tabular-nums",
          tone === "success" && "text-emerald-600 dark:text-emerald-400",
          tone === "muted" && "text-zinc-500 dark:text-zinc-400",
        )}
      >
        {breakdown.net}
      </div>
      <div className="mt-0.5 flex flex-wrap items-center gap-1 text-[11px] text-zinc-500 dark:text-zinc-400">
        {hasDelta ? (
          <>
            <span className="tabular-nums">{breakdown.base}</span>
            <span
              className="tabular-nums text-emerald-600 dark:text-emerald-400"
              title={`${breakdown.added_by_feedback} finding(s) moved into this bucket by human feedback`}
            >
              + {breakdown.added_by_feedback}
            </span>
            <span
              className="tabular-nums text-rose-600 dark:text-rose-300"
              title={`${breakdown.removed_by_feedback} finding(s) moved out of this bucket by human feedback`}
            >
              − {breakdown.removed_by_feedback}
            </span>
            <span
              className="tabular-nums text-amber-600 dark:text-amber-400"
              title={`${breakdown.duplicates_in_bucket} finding(s) in this bucket marked as duplicate`}
            >
              − {breakdown.duplicates_in_bucket}
            </span>
            <span className="text-zinc-400">=</span>
            <span className="tabular-nums">{breakdown.net}</span>
          </>
        ) : (
          <span className="tabular-nums">= {breakdown.base}</span>
        )}
      </div>
    </div>
  );
}

function formatValidRate(rate: number | null | undefined): string {
  if (rate === null || rate === undefined || !Number.isFinite(rate)) {
    return "—";
  }
  return `${(rate * 100).toFixed(1)}%`;
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

function AdminRunHeaderActions({
  run,
  projectKey,
  buildFingerprint,
}: {
  run: RunDetail;
  projectKey: string | undefined;
  buildFingerprint: string;
}) {
  const qc = useQueryClient();
  const router = useRouter();
  const [active, setActive] = React.useState<RunAction | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const onConfirm = async (reason: string | null) => {
    setError(null);
    try {
      if (active === "cancel") {
        await api.cancelRun(run.id, reason);
        qc.invalidateQueries({ queryKey: ["build-runs"] });
        qc.invalidateQueries({ queryKey: ["run", run.id] });
      } else if (active === "complete") {
        await api.completeRun(run.id, reason);
        qc.invalidateQueries({ queryKey: ["build-runs"] });
        qc.invalidateQueries({ queryKey: ["run", run.id] });
      } else if (active === "delete") {
        await api.deleteRun(run.id);
        // Optimistically remove the deleted run from every cached
        // ``["build-runs", ...]`` page so the parent runs-list page
        // (which we're about to ``router.replace`` to) renders without
        // flashing the deleted row out of stale cache. We also drop
        // the per-run cache entry entirely — there's no row to refetch.
        qc.setQueriesData<RunsPage>(
          { queryKey: ["build-runs"] },
          (old) => {
            if (!old) return old;
            return {
              items: old.items.filter((r) => r.id !== run.id),
              total: Math.max(old.total - 1, 0),
            };
          },
        );
        qc.removeQueries({ queryKey: ["run", run.id] });
        qc.invalidateQueries({ queryKey: ["build-runs"] });
        // Navigate back to the parent runs list (or /reports if we
        // don't know the project key — e.g. project-lookup fell
        // through and we don't have a project_key handy).
        if (projectKey) {
          router.replace(
            `/reports/projects/${projectKey}/builds/${buildFingerprint}/runs`,
          );
        } else {
          router.replace("/reports");
        }
        return;
      }
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.message
          : `Failed to ${active} the run.`,
      );
      throw err;
    }
  };

  const cancelDisabled = run.status === "cancelled";
  const completeDisabled = run.status === "completed";

  const copy = active ? RUN_ACTION_COPY[active] : null;

  return (
    <>
      <div className="flex items-center gap-1">
        <Button
          size="sm"
          variant="outline"
          onClick={() => setActive("cancel")}
          disabled={cancelDisabled}
          title={
            cancelDisabled ? "Run is already cancelled" : "Cancel this run"
          }
        >
          <Ban className="mr-1 h-3.5 w-3.5" aria-hidden /> Cancel
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => setActive("complete")}
          disabled={completeDisabled}
          title={
            completeDisabled
              ? "Run is already complete"
              : "Manually mark this run complete"
          }
        >
          <CheckCircle2 className="mr-1 h-3.5 w-3.5" aria-hidden /> Mark complete
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => setActive("delete")}
          className="text-rose-700 hover:bg-rose-50 dark:text-rose-300 dark:hover:bg-rose-950/40"
          title="Permanently delete this run and all its data"
        >
          <Trash2 className="mr-1 h-3.5 w-3.5" aria-hidden /> Delete
        </Button>
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
          className="basis-full rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:bg-rose-950 dark:text-rose-200"
        >
          {error}
        </p>
      ) : null}
    </>
  );
}
