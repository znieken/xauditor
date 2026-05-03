"use client";

import * as React from "react";
import {
  PaginationFooter,
  usePagination,
} from "@/components/paginated-list";
import type { PageSizeScope } from "@/components/page-size-selector";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input, Select } from "@/components/ui/input";
import {
  useCoverageFiles,
  useCoverageFunctions,
  useCoverageModules,
  useCoverageSummary,
} from "@/lib/api";
import type {
  CoverageCategorySummary,
  CoverageFileItem,
  CoverageFunctionItem,
  CoverageListParams,
  CoverageModuleItem,
  CoveragePage,
  CoverageStatus,
} from "@/lib/types";
import { cn } from "@/lib/utils";

const STATUS_TONE: Record<
  string,
  React.ComponentProps<typeof Badge>["tone"]
> = {
  audited: "success",
  unaudited: "warning",
  excluded: "neutral",
  skipped: "info",
};

const STATUS_OPTIONS: Array<{ value: "" | CoverageStatus; label: string }> = [
  { value: "", label: "All statuses" },
  { value: "audited", label: "Audited" },
  { value: "unaudited", label: "Unaudited" },
  { value: "excluded", label: "Excluded" },
  { value: "skipped", label: "Skipped" },
];

function StatusPill({ status }: { status: string }) {
  return <Badge tone={STATUS_TONE[status] ?? "neutral"}>{status}</Badge>;
}

export function CoverageView({
  runId,
  runStatus,
}: {
  runId: string;
  runStatus?: "in_progress" | "completed" | "failed" | "cancelled";
}) {
  // Poll every 3s while the run is still producing coverage rows.
  // React Query's background-refresh contract (same one used by useFindings)
  // keeps per-card pagination, status filter, search value, and scroll
  // position intact across every poll — do not break this invariant.
  const pollWhileRunning = runStatus === "in_progress";
  const summary = useCoverageSummary(runId, { pollWhileRunning });

  if (summary.isLoading) {
    return (
      <p className="text-sm text-zinc-500 dark:text-zinc-400">
        Loading coverage…
      </p>
    );
  }
  if (summary.error) {
    return (
      <p
        role="alert"
        className="rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:bg-rose-950 dark:text-rose-200"
      >
        Failed to load coverage.
      </p>
    );
  }
  if (
    summary.data &&
    summary.data.modules.total === 0 &&
    summary.data.files.total === 0 &&
    summary.data.functions.total === 0
  ) {
    return (
      <EmptyState
        title="No coverage data for this run"
        description="Coverage rows appear after the audit reports stage completes."
      />
    );
  }
  if (!summary.data) return null;

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
      <ModulesCard
        runId={runId}
        summary={summary.data.modules}
        pollWhileRunning={pollWhileRunning}
      />
      <FilesCard
        runId={runId}
        summary={summary.data.files}
        pollWhileRunning={pollWhileRunning}
      />
      <FunctionsCard
        runId={runId}
        summary={summary.data.functions}
        pollWhileRunning={pollWhileRunning}
      />
    </div>
  );
}

interface CategoryCardProps<T> {
  title: string;
  scope: PageSizeScope;
  summary: CoverageCategorySummary;
  searchPlaceholder: string;
  query: ReturnType<typeof useCoverageModules> | ReturnType<typeof useCoverageFiles> | ReturnType<typeof useCoverageFunctions>;
  filter: CoverageListParams;
  setFilter: (next: CoverageListParams) => void;
  pagination: ReturnType<typeof usePagination>;
  renderItem: (item: T, key: string) => React.ReactNode;
  keyOf: (item: T) => string;
}

function CategoryCard<T>({
  title,
  scope,
  summary,
  searchPlaceholder,
  query,
  filter,
  setFilter,
  pagination,
  renderItem,
  keyOf,
}: CategoryCardProps<T>) {
  const page = (query.data as CoveragePage<T> | undefined) ?? undefined;
  return (
    <Card className="flex min-h-[400px] flex-col">
      <CardHeader className="space-y-2">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <CardTitle>{title}</CardTitle>
          <div className="flex items-baseline gap-2 text-xs text-zinc-500 dark:text-zinc-400">
            <span>
              total <span className="font-medium tabular-nums text-zinc-700 dark:text-zinc-200">{summary.total}</span>
            </span>
            <span>·</span>
            <span>
              <span className="font-medium tabular-nums text-emerald-600 dark:text-emerald-400">
                {summary.percent_audited}%
              </span>{" "}
              audited
            </span>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-1.5 text-[10px] text-zinc-500 dark:text-zinc-400">
          {STATUS_OPTIONS.filter((o) => o.value !== "").map((option) => (
            <Badge
              key={option.value}
              tone={STATUS_TONE[option.value] ?? "neutral"}
            >
              {option.label.toLowerCase()}{" "}
              {summary.by_status[option.value as CoverageStatus] ?? 0}
            </Badge>
          ))}
        </div>
        <div className="flex flex-wrap gap-2">
          <Input
            value={filter.q ?? ""}
            placeholder={searchPlaceholder}
            onChange={(event) => {
              setFilter({ ...filter, q: event.target.value || undefined });
              pagination.setPage(1);
            }}
          />
          <Select
            value={(filter.status as string) ?? ""}
            onChange={(event) => {
              const value = event.target.value;
              setFilter({
                ...filter,
                status: (value || undefined) as CoverageStatus | undefined,
              });
              pagination.setPage(1);
            }}
          >
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </div>
      </CardHeader>
      <CardContent className="flex-1 overflow-hidden p-0">
        <div className="max-h-[420px] overflow-auto">
          {query.isLoading && !page ? (
            <p className="px-4 py-3 text-xs text-zinc-500 dark:text-zinc-400">
              Loading…
            </p>
          ) : query.error ? (
            <p
              role="alert"
              className="m-3 rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:bg-rose-950 dark:text-rose-200"
            >
              Failed to load.
            </p>
          ) : page && page.items.length === 0 ? (
            <p className="px-4 py-6 text-center text-xs text-zinc-500 dark:text-zinc-400">
              No matches in this category.
            </p>
          ) : page ? (
            <ul
              className={cn(
                "space-y-1 px-3 py-2 text-sm",
                query.isFetching && "opacity-75",
              )}
            >
              {page.items.map((item) => (
                <li key={keyOf(item)}>{renderItem(item, keyOf(item))}</li>
              ))}
            </ul>
          ) : null}
        </div>
      </CardContent>
      <PaginationFooter
        scope={scope}
        total={page?.total ?? 0}
        pagination={pagination}
        label={title.toLowerCase()}
      />
    </Card>
  );
}

function ModulesCard({
  runId,
  summary,
  pollWhileRunning,
}: {
  runId: string;
  summary: CoverageCategorySummary;
  pollWhileRunning: boolean;
}) {
  const pagination = usePagination("coverage-modules");
  const [filter, setFilter] = React.useState<CoverageListParams>({});
  const query = useCoverageModules(
    runId,
    {
      ...filter,
      limit: pagination.pageSize,
      offset: pagination.offset,
    },
    { pollWhileRunning },
  );
  return (
    <CategoryCard<CoverageModuleItem>
      title="Modules"
      scope="coverage-modules"
      summary={summary}
      searchPlaceholder="search module name"
      query={query}
      filter={filter}
      setFilter={setFilter}
      pagination={pagination}
      keyOf={(m) => m.module_name}
      renderItem={(m) => (
        <div className="flex items-center justify-between gap-2 rounded-md border border-zinc-100 px-3 py-1.5 dark:border-zinc-800">
          <span className="truncate font-mono text-xs">{m.module_name}</span>
          <StatusPill status={m.status} />
        </div>
      )}
    />
  );
}

function FilesCard({
  runId,
  summary,
  pollWhileRunning,
}: {
  runId: string;
  summary: CoverageCategorySummary;
  pollWhileRunning: boolean;
}) {
  const pagination = usePagination("coverage-files");
  const [filter, setFilter] = React.useState<CoverageListParams>({});
  const query = useCoverageFiles(
    runId,
    {
      ...filter,
      limit: pagination.pageSize,
      offset: pagination.offset,
    },
    { pollWhileRunning },
  );
  return (
    <CategoryCard<CoverageFileItem>
      title="Files"
      scope="coverage-files"
      summary={summary}
      searchPlaceholder="search file path"
      query={query}
      filter={filter}
      setFilter={setFilter}
      pagination={pagination}
      keyOf={(f) => f.file_path}
      renderItem={(f) => (
        <div className="flex items-center justify-between gap-2 rounded-md border border-zinc-100 px-3 py-1.5 dark:border-zinc-800">
          <span className="truncate font-mono text-xs">{f.file_path}</span>
          <StatusPill status={f.status} />
        </div>
      )}
    />
  );
}

function FunctionsCard({
  runId,
  summary,
  pollWhileRunning,
}: {
  runId: string;
  summary: CoverageCategorySummary;
  pollWhileRunning: boolean;
}) {
  const pagination = usePagination("coverage-functions");
  const [filter, setFilter] = React.useState<CoverageListParams>({});
  const query = useCoverageFunctions(
    runId,
    {
      ...filter,
      limit: pagination.pageSize,
      offset: pagination.offset,
    },
    { pollWhileRunning },
  );
  return (
    <CategoryCard<CoverageFunctionItem>
      title="Functions"
      scope="coverage-functions"
      summary={summary}
      searchPlaceholder="search qualified name"
      query={query}
      filter={filter}
      setFilter={setFilter}
      pagination={pagination}
      keyOf={(fn) => fn.function_id}
      renderItem={(fn) => (
        <div className="flex flex-col gap-1 rounded-md border border-zinc-100 px-3 py-1.5 dark:border-zinc-800">
          <div className="flex items-center justify-between gap-2">
            <span className="truncate font-mono text-xs">
              {fn.qualified_name}
            </span>
            <StatusPill status={fn.status} />
          </div>
          {fn.file_path ? (
            <span className="truncate font-mono text-[11px] text-zinc-500 dark:text-zinc-400">
              {fn.file_path}
            </span>
          ) : null}
        </div>
      )}
    />
  );
}
