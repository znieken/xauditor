"use client";

import { RotateCcw } from "lucide-react";
import * as React from "react";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { MultiSelect } from "@/components/ui/multi-select";
import {
  CODER_STATUS_DEFAULT,
  CODER_STATUS_OPTIONS,
  VALIDATION_DEFAULT,
} from "@/lib/findings-filters";
import type { FeedbackLabel, FindingFilters } from "@/lib/types";

const CONFIDENCE_OPTIONS = [
  { value: "High", label: "High" },
  { value: "Medium", label: "Medium" },
  { value: "Low", label: "Low" },
];

const VALIDATION_OPTIONS = [
  { value: "Valid", label: "Valid" },
  { value: "Partial Valid", label: "Partial Valid" },
  { value: "Inconclusive", label: "Inconclusive" },
  { value: "False Positive", label: "False Positive" },
];

const EXPLOITATION_OPTIONS = [
  { value: "exploitable", label: "Exploitable" },
  { value: "uncertain", label: "Uncertain" },
  { value: "not_exploitable", label: "Not exploitable" },
];

const FEEDBACK_OPTIONS: { value: FeedbackLabel; label: string }[] = [
  { value: "true_positive", label: "True positive" },
  { value: "false_positive", label: "False positive" },
  { value: "duplicate", label: "Duplicate" },
  { value: "unlabeled", label: "Unlabeled" },
];

const CODER_STATUS_CHIP_OPTIONS = CODER_STATUS_OPTIONS.map((value) => ({
  value,
  label: value,
}));

export function FilterBar({
  value,
  onChange,
  onExpandAll,
  onCollapseAll,
}: {
  value: FindingFilters;
  onChange: (next: FindingFilters) => void;
  onExpandAll: () => void;
  onCollapseAll: () => void;
}) {
  function setScalar(
    key: "file" | "function" | "q",
    v: string,
  ) {
    const next = { ...value };
    if (v === "") delete next[key];
    else next[key] = v;
    onChange(next);
  }

  function setArray(
    key:
      | "confidence"
      | "validation_status"
      | "exploitation_status"
      | "coder_status",
    v: string[],
  ) {
    const next = { ...value };
    // For dimensions with a default-checked subset (validation_status,
    // coder_status), an empty array is meaningful — the "user
    // explicitly cleared it" marker that serializes to
    // `?validation_status=` / `?coder_status=` and tells the parser
    // to suppress the default on reload. For the other arrays, drop
    // the key entirely on empty so the URL stays tidy.
    if (key === "validation_status" || key === "coder_status") {
      next[key] = v;
    } else if (v.length === 0) {
      delete next[key];
    } else {
      next[key] = v;
    }
    onChange(next);
  }

  function setFeedback(v: FeedbackLabel[]) {
    const next = { ...value };
    if (v.length === 0) delete next.feedback_label;
    else next.feedback_label = v;
    onChange(next);
  }

  function reset() {
    onChange({
      validation_status: [...VALIDATION_DEFAULT],
      coder_status: [...CODER_STATUS_DEFAULT],
    });
  }

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-zinc-200 bg-white/70 p-4 dark:border-zinc-800 dark:bg-zinc-900/40">
      <div className="grid grid-cols-1 gap-3 md:grid-cols-3 lg:grid-cols-7">
        <div className="space-y-1">
          <Label htmlFor="filter-file">File</Label>
          <Input
            id="filter-file"
            placeholder="substring"
            value={value.file ?? ""}
            onChange={(event) => setScalar("file", event.target.value)}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="filter-function">Function</Label>
          <Input
            id="filter-function"
            placeholder="substring"
            value={value.function ?? ""}
            onChange={(event) => setScalar("function", event.target.value)}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="filter-confidence">Confidence</Label>
          <MultiSelect
            id="filter-confidence"
            label="Confidence"
            options={CONFIDENCE_OPTIONS}
            value={value.confidence ?? []}
            onChange={(v) => setArray("confidence", v)}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="filter-validation">Validation</Label>
          <MultiSelect
            id="filter-validation"
            label="Validation"
            options={VALIDATION_OPTIONS}
            value={value.validation_status ?? []}
            onChange={(v) => setArray("validation_status", v)}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="filter-exploitation">Exploitation</Label>
          <MultiSelect
            id="filter-exploitation"
            label="Exploitation"
            options={EXPLOITATION_OPTIONS}
            value={value.exploitation_status ?? []}
            onChange={(v) => setArray("exploitation_status", v)}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="filter-feedback">Feedback</Label>
          <MultiSelect
            id="filter-feedback"
            label="Feedback"
            options={FEEDBACK_OPTIONS}
            value={value.feedback_label ?? []}
            onChange={(v) => setFeedback(v as FeedbackLabel[])}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="filter-coder-status">Coder verification</Label>
          <MultiSelect
            id="filter-coder-status"
            label="Coder verification"
            options={CODER_STATUS_CHIP_OPTIONS}
            value={value.coder_status ?? []}
            onChange={(v) => setArray("coder_status", v)}
          />
        </div>
      </div>
      <div className="flex flex-wrap items-end gap-3 md:items-center md:justify-between">
        <div className="flex-1 space-y-1">
          <Label htmlFor="filter-q">Search</Label>
          <Input
            id="filter-q"
            placeholder="free-text over name / description / analysis / reason"
            value={value.q ?? ""}
            onChange={(event) => setScalar("q", event.target.value)}
          />
        </div>
        <div className="flex items-center gap-2">
          <Button variant="secondary" size="sm" onClick={onExpandAll}>
            Expand all
          </Button>
          <Button variant="secondary" size="sm" onClick={onCollapseAll}>
            Collapse all
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={reset}
            aria-label="Reset filters"
          >
            <RotateCcw className="h-3.5 w-3.5" aria-hidden />
            Reset
          </Button>
        </div>
      </div>
    </div>
  );
}
