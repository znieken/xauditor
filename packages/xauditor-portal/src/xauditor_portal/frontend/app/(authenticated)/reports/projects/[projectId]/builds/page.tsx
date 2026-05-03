"use client";

import Link from "next/link";
import * as React from "react";
import { Breadcrumbs } from "@/components/breadcrumbs";
import {
  PaginationFooter,
  usePagination,
} from "@/components/paginated-list";
import { StatusChip } from "@/components/status-chip";
import { EmptyState } from "@/components/ui/empty-state";
import { useBuildsForProject, useProjects } from "@/lib/api";
import { formatTimestamp } from "@/lib/utils";
import type { RunStatus } from "@/lib/types";

export default function BuildListPage({
  params,
}: {
  params: { projectId: string };
}) {
  const pagination = usePagination("builds");
  const { data, isLoading, error } = useBuildsForProject(params.projectId, {
    limit: pagination.pageSize,
    offset: pagination.offset,
  });

  // Resolve the display name by fetching the projects list once (cached).
  const { data: projectPage } = useProjects({ limit: 200, offset: 0 });
  const project = projectPage?.items.find(
    (p) => p.project_key === params.projectId,
  );

  return (
    <div className="space-y-5">
      <Breadcrumbs
        items={[
          { label: "Reports", href: "/reports" },
          { label: project?.project_name ?? params.projectId.slice(0, 8) },
        ]}
      />
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold tracking-tight">
          {project?.project_name ?? "Graph builds"}
        </h1>
        {project ? (
          <p className="font-mono text-xs text-zinc-500 dark:text-zinc-400">
            {project.repo_root}
          </p>
        ) : null}
        <p className="text-sm text-zinc-500 dark:text-zinc-400">
          Each row is one graph build. Pick a build to see its audit runs.
        </p>
      </header>

      {error ? (
        <p
          role="alert"
          className="rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:bg-rose-950 dark:text-rose-200"
        >
          Failed to load graph builds for this project.
        </p>
      ) : null}

      {isLoading ? (
        <div className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</div>
      ) : data && data.items.length > 0 ? (
        <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900/60">
          <table className="w-full text-sm">
            <thead className="bg-zinc-50 text-xs uppercase tracking-wider text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400">
              <tr>
                <th className="px-5 py-2 text-left">Build fingerprint</th>
                <th className="px-5 py-2 text-left">Built</th>
                <th className="px-5 py-2 text-left">Audit runs</th>
                <th className="px-5 py-2 text-left">Last run</th>
                <th className="px-5 py-2 text-left">Last status</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((build) => (
                <tr
                  key={build.build_fingerprint}
                  className="border-t border-zinc-100 transition-colors hover:bg-zinc-50/70 dark:border-zinc-800 dark:hover:bg-zinc-800/40"
                >
                  <td className="px-5 py-3">
                    <Link
                      href={`/reports/projects/${params.projectId}/builds/${build.build_fingerprint}/runs`}
                      className="font-mono text-xs text-zinc-900 hover:underline dark:text-zinc-100"
                    >
                      {build.build_fingerprint.slice(0, 24)}
                    </Link>
                  </td>
                  <td className="px-5 py-3 text-xs text-zinc-500 dark:text-zinc-400">
                    {formatTimestamp(build.built_at)}
                  </td>
                  <td className="px-5 py-3 tabular-nums">
                    {build.total_audit_runs}
                  </td>
                  <td className="px-5 py-3 text-xs text-zinc-500 dark:text-zinc-400">
                    {formatTimestamp(build.last_run_started_at)}
                  </td>
                  <td className="px-5 py-3">
                    {build.last_run_status ? (
                      <StatusChip
                        status={build.last_run_status as RunStatus}
                      />
                    ) : (
                      <span className="text-xs text-zinc-400">—</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <PaginationFooter
            scope="builds"
            total={data.total}
            pagination={pagination}
            label="builds"
          />
        </div>
      ) : (
        <EmptyState
          title="No graph builds for this project"
          description="Builds appear here once an audit run records one."
        />
      )}
    </div>
  );
}
