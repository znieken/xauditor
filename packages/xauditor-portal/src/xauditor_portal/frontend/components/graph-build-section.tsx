"use client";

import * as React from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label, Select } from "@/components/ui/input";
import { OverrideBadge } from "@/components/override-badge";
import { NumericField } from "@/components/numeric-field";
import { isReadOnly, sourceOf } from "@/lib/config-source";
import { cn } from "@/lib/utils";
import type { ConfigField } from "@/lib/types";

export interface GraphBuildDraft {
  enable_llm_enrichment?: string;
  max_file_bytes?: string;
  paths_max_depth?: string;
  paths_max_count?: string;
  neo4j_chunk_size?: string;
}

interface Props {
  fields: Record<string, ConfigField | undefined>;
  draft: GraphBuildDraft;
  onChange: (patch: GraphBuildDraft) => void;
}

function rawValue(
  fields: Record<string, ConfigField | undefined>,
  key: string,
): string {
  const raw = fields[key]?.value;
  if (raw === undefined || raw === null) return "";
  return String(raw);
}

export function GraphBuildSection({ fields, draft, onChange }: Props) {
  const enableKey = "graph.build.enable_llm_enrichment";
  const enableReadOnly = isReadOnly(fields, enableKey);
  const enableSource = sourceOf(fields, enableKey);
  const enableField = fields[enableKey];
  const enableValue =
    draft.enable_llm_enrichment ?? rawValue(fields, enableKey) ?? "false";

  const numberFor = (key: keyof GraphBuildDraft, fullKey: string) =>
    draft[key] ?? rawValue(fields, fullKey);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Graph build</CardTitle>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Limits for the repository code-graph builder. The Neo4j chunk size
          can also be set via the{" "}
          <span className="font-mono">XAUDITOR_GRAPH_BUILD_NEO4J_CHUNK_SIZE</span>{" "}
          environment variable; when present it wins over both yml and DB.
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        <div
          className={cn(
            "grid grid-cols-1 gap-2 md:grid-cols-[240px_1fr_auto] md:items-center",
            enableReadOnly && "opacity-70",
          )}
        >
          <div className="space-y-0.5">
            <Label htmlFor={enableKey}>Enable LLM enrichment</Label>
            <div className="flex flex-wrap items-center gap-1.5">
              {enableReadOnly ? (
                <OverrideBadge ymlKey={enableKey} source={enableSource} />
              ) : null}
              {enableField ? (
                <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
                  src: {enableField.source}
                </span>
              ) : null}
            </div>
            <p className="text-xs text-zinc-500 dark:text-zinc-400">
              Layer summaries / business context onto graph nodes.
            </p>
          </div>
          <div>
            <Select
              id={enableKey}
              value={enableValue === "" ? "false" : enableValue}
              disabled={enableReadOnly}
              onChange={(event) =>
                onChange({ enable_llm_enrichment: event.target.value })
              }
            >
              <option value="true">true</option>
              <option value="false">false</option>
            </Select>
          </div>
          <div />
        </div>
        <NumericField
          fullKey="graph.build.max_file_bytes"
          label="Max file bytes"
          fields={fields}
          value={numberFor("max_file_bytes", "graph.build.max_file_bytes")}
          onChange={(next) => onChange({ max_file_bytes: next })}
          min={1}
          step="1"
          description="Upper bound on file size accepted by the parser."
        />
        <NumericField
          fullKey="graph.build.paths_max_depth"
          label="Paths max depth"
          fields={fields}
          value={numberFor("paths_max_depth", "graph.build.paths_max_depth")}
          onChange={(next) => onChange({ paths_max_depth: next })}
          min={1}
          step="1"
          description="Cap on call-graph traversal depth used by audit path planning."
        />
        <NumericField
          fullKey="graph.build.paths_max_count"
          label="Paths max count"
          fields={fields}
          value={numberFor("paths_max_count", "graph.build.paths_max_count")}
          onChange={(next) => onChange({ paths_max_count: next })}
          min={1}
          step="1"
          description="Cap on the total number of audit paths considered per build."
        />
        <NumericField
          fullKey="graph.build.neo4j_chunk_size"
          label="Neo4j chunk size"
          fields={fields}
          value={numberFor("neo4j_chunk_size", "graph.build.neo4j_chunk_size")}
          onChange={(next) => onChange({ neo4j_chunk_size: next })}
          min={100}
          max={50_000}
          step="1"
          description="Records per UNWIND-MERGE batch sent to Neo4j during canonical_finalize."
          hint="Allowed range: [100, 50000]."
        />
      </CardContent>
    </Card>
  );
}
