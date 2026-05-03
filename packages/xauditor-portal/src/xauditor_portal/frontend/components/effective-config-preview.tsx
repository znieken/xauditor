"use client";

import * as React from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useEffectiveLLMConfig } from "@/lib/api";
import { cn } from "@/lib/utils";

const COLUMNS: Array<{ key: string; label: string }> = [
  { key: "agent", label: "Agent" },
  { key: "provider", label: "Provider" },
  { key: "model_name", label: "Model" },
  { key: "temperature", label: "Temperature" },
  { key: "top_p", label: "Top P" },
  { key: "top_k", label: "Top K" },
  { key: "repetition_penalty", label: "Rep. penalty" },
  { key: "thinking_enabled", label: "Thinking" },
  { key: "request_timeout_seconds", label: "Req. timeout (s)" },
];

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "on" : "off";
  return String(value);
}

export function EffectiveConfigPreview() {
  const { data, isLoading, error } = useEffectiveLLMConfig();

  return (
    <Card>
      <CardHeader>
        <CardTitle>Effective model configuration</CardTitle>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Read-only. Shows what each agent will actually send after yml
          overrides and per-agent overrides are resolved. The coloured badge
          per cell indicates whether the value came from{" "}
          <span className="font-mono">yml</span>,{" "}
          <span className="font-mono">db</span>, or{" "}
          <span className="font-mono">default</span>.
        </p>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <p className="text-xs text-zinc-500 dark:text-zinc-400">Loading…</p>
        ) : error ? (
          <p
            role="alert"
            className="rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:bg-rose-950 dark:text-rose-200"
          >
            Failed to load effective LLM config.
          </p>
        ) : data ? (
          <div className="overflow-auto">
            <table className="min-w-full text-xs">
              <thead className="bg-zinc-50 text-[10px] uppercase tracking-wider text-zinc-500 dark:bg-zinc-900 dark:text-zinc-400">
                <tr>
                  {COLUMNS.map((col) => (
                    <th key={col.key} className="px-3 py-2 text-left">
                      {col.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.agents.map((row) => (
                  <tr
                    key={row.agent}
                    className="border-t border-zinc-100 dark:border-zinc-800"
                  >
                    {COLUMNS.map((col) => {
                      const value =
                        col.key === "agent"
                          ? row.agent
                          : (row as unknown as Record<string, unknown>)[col.key];
                      const source =
                        col.key === "agent" ? "default" : row.sources[col.key];
                      return (
                        <td
                          key={col.key}
                          className="px-3 py-2 align-top font-mono text-zinc-800 dark:text-zinc-200"
                        >
                          <div>{renderValue(value)}</div>
                          {col.key !== "agent" ? (
                            <SourceTag source={source ?? "default"} />
                          ) : null}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

function SourceTag({ source }: { source: string }) {
  return (
    <span
      className={cn(
        "mt-1 inline-block rounded px-1.5 py-0.5 text-[10px]",
        source === "yml" &&
          "bg-amber-100 text-amber-900 dark:bg-amber-950/60 dark:text-amber-200",
        source === "env" &&
          "bg-sky-200 text-sky-900 dark:bg-sky-900/60 dark:text-sky-100",
        source === "db" &&
          "bg-sky-100 text-sky-900 dark:bg-sky-950/60 dark:text-sky-200",
        source === "default" &&
          "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
      )}
    >
      {source}
    </span>
  );
}
