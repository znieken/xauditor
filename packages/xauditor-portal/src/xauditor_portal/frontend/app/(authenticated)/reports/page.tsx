"use client";

import Link from "next/link";
import * as React from "react";
import { Breadcrumbs } from "@/components/breadcrumbs";
import {
  PaginationFooter,
  usePagination,
} from "@/components/paginated-list";
import { EmptyState } from "@/components/ui/empty-state";
import { useProjects } from "@/lib/api";
import { formatTimestamp } from "@/lib/utils";

export default function ProjectListPage() {
  const pagination = usePagination("projects");
  const { data, isLoading, error } = useProjects({
    limit: pagination.pageSize,
    offset: pagination.offset,
  });

  return (
    <div className="space-y-5">
      <Breadcrumbs items={[{ label: "Reports" }]} />
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold tracking-tight">Projects</h1>
        <p className="text-sm text-zinc-500 dark:text-zinc-400">
          Every project xauditor has produced a run for. Pick a project to
          drill into its graph builds and audit runs.
        </p>
      </header>

      {error ? (
        <p
          role="alert"
          className="rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:bg-rose-950 dark:text-rose-200"
        >
          Failed to load projects.
        </p>
      ) : null}

      {isLoading ? (
        <div className="text-sm text-zinc-500 dark:text-zinc-400">Loading…</div>
      ) : data && data.items.length > 0 ? (
        <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900/60">
          <table className="w-full text-sm">
            <thead className="bg-zinc-50 text-xs uppercase tracking-wider text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400">
              <tr>
                <th className="px-5 py-2 text-left">Project</th>
                <th className="px-5 py-2 text-left">Graph builds</th>
                <th className="px-5 py-2 text-left">Audit runs</th>
                <th className="px-5 py-2 text-left">Added</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((project) => (
                <tr
                  key={project.project_key}
                  className="border-t border-zinc-100 transition-colors hover:bg-zinc-50/70 dark:border-zinc-800 dark:hover:bg-zinc-800/40"
                >
                  <td className="px-5 py-3">
                    <Link
                      href={`/reports/projects/${project.project_key}/builds`}
                      className="font-medium text-zinc-900 hover:underline dark:text-zinc-100"
                    >
                      {project.project_name}
                    </Link>
                    <div className="font-mono text-xs text-zinc-500 dark:text-zinc-400">
                      {project.repo_root}
                    </div>
                  </td>
                  <td className="px-5 py-3 tabular-nums">
                    {project.total_graph_builds}
                  </td>
                  <td className="px-5 py-3 tabular-nums">
                    {project.total_audit_runs}
                  </td>
                  <td className="px-5 py-3 text-xs text-zinc-500 dark:text-zinc-400">
                    {formatTimestamp(project.added_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <PaginationFooter
            scope="projects"
            total={data.total}
            pagination={pagination}
            label="projects"
          />
        </div>
      ) : (
        <EmptyState
          title="No projects yet"
          description="Run `xauditor audit` to produce a run; the portal groups every run under its project here."
        />
      )}
    </div>
  );
}
