"use client";

import * as React from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/input";
import { ChipInput } from "@/components/chip-input";
import { OverrideBadge } from "@/components/override-badge";
import { isReadOnly, sourceOf } from "@/lib/config-source";
import { cn } from "@/lib/utils";
import type { ConfigField } from "@/lib/types";

interface Props {
  fields: Record<string, ConfigField | undefined>;
  value: string[];
  onChange: (next: string[]) => void;
}

const KEY = "repository.excludes";

export function RepositorySection({ fields, value, onChange }: Props) {
  const readOnly = isReadOnly(fields, KEY);
  const source = sourceOf(fields, KEY);
  const sourceField = fields[KEY];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Repository</CardTitle>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Glob patterns excluded from the graph build and the audit. Patterns
          are matched against repo-relative paths.
        </p>
      </CardHeader>
      <CardContent>
        <div
          className={cn(
            "grid grid-cols-1 gap-2 md:grid-cols-[240px_1fr] md:items-start",
            readOnly && "opacity-70",
          )}
        >
          <div className="space-y-0.5">
            <Label htmlFor={KEY}>Excludes</Label>
            <div className="flex flex-wrap items-center gap-1.5">
              {readOnly ? <OverrideBadge ymlKey={KEY} source={source} /> : null}
              {sourceField ? (
                <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
                  src: {sourceField.source}
                </span>
              ) : null}
            </div>
          </div>
          <ChipInput
            id={KEY}
            value={value}
            onChange={onChange}
            disabled={readOnly}
            placeholder="e.g. tests/**, build/, *.lock"
          />
        </div>
      </CardContent>
    </Card>
  );
}
