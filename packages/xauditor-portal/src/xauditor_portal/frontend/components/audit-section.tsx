"use client";

import * as React from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { NumericField } from "@/components/numeric-field";
import type { ConfigField } from "@/lib/types";

interface Props {
  fields: Record<string, ConfigField | undefined>;
  value: string;
  onChange: (next: string) => void;
}

export function AuditSection({ fields, value, onChange }: Props) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Audit</CardTitle>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Worker concurrency for the audit pipeline. <code>1</code> runs paths
          in-process; <code>2..16</code> spawns that many subprocess workers.
        </p>
      </CardHeader>
      <CardContent>
        <NumericField
          fullKey="audit.worker_count"
          label="Worker count"
          fields={fields}
          value={value}
          onChange={onChange}
          min={1}
          max={16}
          step="1"
          hint="Allowed range: [1, 16]."
        />
      </CardContent>
    </Card>
  );
}
