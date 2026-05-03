"use client";

import * as React from "react";
import { Input, Label } from "@/components/ui/input";
import { OverrideBadge } from "@/components/override-badge";
import { isReadOnly, sourceOf } from "@/lib/config-source";
import { cn } from "@/lib/utils";
import type { ConfigField } from "@/lib/types";

export interface NumericFieldProps {
  fullKey: string;
  label: string;
  description?: string;
  fields: Record<string, ConfigField | undefined>;
  value: string;
  onChange: (next: string) => void;
  min?: number;
  max?: number;
  step?: string;
  hint?: string;
}

export function NumericField({
  fullKey,
  label,
  description,
  fields,
  value,
  onChange,
  min,
  max,
  step,
  hint,
}: NumericFieldProps) {
  const readOnly = isReadOnly(fields, fullKey);
  const source = sourceOf(fields, fullKey);
  const sourceField = fields[fullKey];
  return (
    <div
      className={cn(
        "grid grid-cols-1 gap-2 md:grid-cols-[240px_1fr_auto] md:items-center",
        readOnly && "opacity-70",
      )}
    >
      <div className="space-y-0.5">
        <Label htmlFor={fullKey}>{label}</Label>
        <div className="flex flex-wrap items-center gap-1.5">
          {readOnly ? (
            <OverrideBadge ymlKey={fullKey} source={source} />
          ) : null}
          {sourceField ? (
            <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
              src: {sourceField.source}
            </span>
          ) : null}
        </div>
        {description ? (
          <p className="text-xs text-zinc-500 dark:text-zinc-400">{description}</p>
        ) : null}
        {hint ? (
          <p className="text-[11px] text-zinc-400 dark:text-zinc-500">{hint}</p>
        ) : null}
      </div>
      <div>
        <Input
          id={fullKey}
          type="number"
          inputMode="numeric"
          value={value}
          disabled={readOnly}
          min={min}
          max={max}
          step={step}
          onChange={(event) => onChange(event.target.value)}
        />
      </div>
      <div />
    </div>
  );
}
