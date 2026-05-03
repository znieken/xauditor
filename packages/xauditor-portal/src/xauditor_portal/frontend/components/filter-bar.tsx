"use client";

import { RotateCcw } from "lucide-react";
import * as React from "react";
import { Button } from "@/components/ui/button";
import { Input, Label, Select } from "@/components/ui/input";
import type { FindingFilters, FeedbackLabel } from "@/lib/types";

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
  function setField<K extends keyof FindingFilters>(
    key: K,
    v: FindingFilters[K] | "",
  ) {
    const next = { ...value };
    if (v === "" || v === undefined) {
      delete next[key];
    } else {
      next[key] = v as FindingFilters[K];
    }
    onChange(next);
  }

  function reset() {
    onChange({});
  }

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-zinc-200 bg-white/70 p-4 dark:border-zinc-800 dark:bg-zinc-900/40">
      <div className="grid grid-cols-1 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <div className="space-y-1">
          <Label htmlFor="filter-file">File</Label>
          <Input
            id="filter-file"
            placeholder="substring"
            value={value.file ?? ""}
            onChange={(event) => setField("file", event.target.value)}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="filter-function">Function</Label>
          <Input
            id="filter-function"
            placeholder="substring"
            value={value.function ?? ""}
            onChange={(event) => setField("function", event.target.value)}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="filter-confidence">Confidence</Label>
          <Select
            id="filter-confidence"
            value={value.confidence ?? ""}
            onChange={(event) => setField("confidence", event.target.value)}
          >
            <option value="">Any</option>
            <option value="High">High</option>
            <option value="Medium">Medium</option>
            <option value="Low">Low</option>
          </Select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="filter-validation">Validation</Label>
          <Select
            id="filter-validation"
            value={value.validation_status ?? ""}
            onChange={(event) =>
              setField("validation_status", event.target.value)
            }
          >
            <option value="">Any</option>
            <option value="Valid">Valid</option>
            <option value="Partial Valid">Partial Valid</option>
            <option value="Inconclusive">Inconclusive</option>
            <option value="False Positive">False Positive</option>
          </Select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="filter-exploitation">Exploitation</Label>
          <Select
            id="filter-exploitation"
            value={value.exploitation_status ?? ""}
            onChange={(event) =>
              setField("exploitation_status", event.target.value)
            }
          >
            <option value="">Any</option>
            <option value="exploitable">Exploitable</option>
            <option value="uncertain">Uncertain</option>
            <option value="not_exploitable">Not exploitable</option>
          </Select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="filter-feedback">Feedback</Label>
          <Select
            id="filter-feedback"
            value={value.feedback_label ?? ""}
            onChange={(event) =>
              setField("feedback_label", event.target.value as FeedbackLabel)
            }
          >
            <option value="">Any</option>
            <option value="true_positive">True positive</option>
            <option value="false_positive">False positive</option>
            <option value="duplicate">Duplicate</option>
            <option value="unlabeled">Unlabeled</option>
          </Select>
        </div>
      </div>
      <div className="flex flex-wrap items-end gap-3 md:items-center md:justify-between">
        <div className="flex-1 space-y-1">
          <Label htmlFor="filter-q">Search</Label>
          <Input
            id="filter-q"
            placeholder="free-text over name / description / analysis / reason"
            value={value.q ?? ""}
            onChange={(event) => setField("q", event.target.value)}
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
