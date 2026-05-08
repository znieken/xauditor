"use client";

import * as React from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label, Select } from "@/components/ui/input";
import { OverrideBadge } from "@/components/override-badge";
import { NumericField } from "@/components/numeric-field";
import { isReadOnly, sourceOf } from "@/lib/config-source";
import { cn } from "@/lib/utils";
import type { ConfigField } from "@/lib/types";

export interface AuditDraft {
  worker_count?: string;
  persist_false_positives?: string;
}

interface Props {
  fields: Record<string, ConfigField | undefined>;
  draft: AuditDraft;
  onChange: (patch: AuditDraft) => void;
}

function rawValue(
  fields: Record<string, ConfigField | undefined>,
  key: string,
): string {
  const raw = fields[key]?.value;
  if (raw === undefined || raw === null) return "";
  return String(raw);
}

export function AuditSection({ fields, draft, onChange }: Props) {
  const persistKey = "audit.persist_false_positives";
  const persistReadOnly = isReadOnly(fields, persistKey);
  const persistSource = sourceOf(fields, persistKey);
  const persistField = fields[persistKey];
  const persistValue =
    draft.persist_false_positives ?? rawValue(fields, persistKey) ?? "false";

  const workerCountValue =
    draft.worker_count ?? rawValue(fields, "audit.worker_count");

  return (
    <Card>
      <CardHeader>
        <CardTitle>Audit</CardTitle>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Worker concurrency for the audit pipeline. <code>1</code> runs paths
          in-process; <code>2..16</code> spawns that many subprocess workers.
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        <NumericField
          fullKey="audit.worker_count"
          label="Worker count"
          fields={fields}
          value={workerCountValue}
          onChange={(next) => onChange({ worker_count: next })}
          min={1}
          max={16}
          step="1"
          hint="Allowed range: [1, 16]."
        />
        <div
          className={cn(
            "grid grid-cols-1 gap-2 md:grid-cols-[240px_1fr_auto] md:items-center",
            persistReadOnly && "opacity-70",
          )}
        >
          <div className="space-y-0.5">
            <Label htmlFor={persistKey}>Persist false positives</Label>
            <div className="flex flex-wrap items-center gap-1.5">
              {persistReadOnly ? (
                <OverrideBadge ymlKey={persistKey} source={persistSource} />
              ) : null}
              {persistField ? (
                <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
                  src: {persistField.source}
                </span>
              ) : null}
            </div>
            <p className="text-xs text-zinc-500 dark:text-zinc-400">
              When <code>false</code> (default), validator-confirmed False
              Positives are dropped at the report-store boundary —
              <code>false-positives.md</code> is emitted with only an
              explanatory note, and reportdb / Neo4j receive no FP rows. Set
              <code>true</code> to keep historical behavior.
            </p>
          </div>
          <div>
            <Select
              id={persistKey}
              value={persistValue === "" ? "false" : persistValue}
              disabled={persistReadOnly}
              onChange={(event) =>
                onChange({ persist_false_positives: event.target.value })
              }
            >
              <option value="true">true</option>
              <option value="false">false</option>
            </Select>
          </div>
          <div />
        </div>
      </CardContent>
    </Card>
  );
}
