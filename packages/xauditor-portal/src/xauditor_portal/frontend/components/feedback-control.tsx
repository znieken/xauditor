"use client";

import {
  Check,
  CircleSlash,
  Copy,
  ThumbsDown,
  ThumbsUp,
} from "lucide-react";
import * as React from "react";
import { useMe, useSaveFeedback } from "@/lib/api";
import { cn, formatRelative } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/input";
import { FindingPicker } from "@/components/finding-picker";
import type {
  DuplicateOfSummary,
  FeedbackLabel,
  FeedbackPayload,
} from "@/lib/types";

type State = "true_positive" | "false_positive" | "duplicate" | "unlabeled";

export function FeedbackControl({
  findingId,
  runId,
  current,
  onChanged,
}: {
  findingId: string;
  runId?: string;
  current: FeedbackPayload | null | undefined;
  onChanged?: () => void;
}) {
  const saveFeedback = useSaveFeedback(findingId, runId);
  const { data: me } = useMe();
  // Explicit viewer check: only mark the control read-only when we know
  // the user is a viewer. While `me` is loading or missing a role field
  // the UI stays writable; the API gate is the authoritative decision.
  const readOnly = me?.role === "viewer";
  const [note, setNote] = React.useState(current?.researcher_note ?? "");
  const [label, setLabel] = React.useState<State>(
    (current?.label as State) ?? "unlabeled",
  );
  const [duplicateOf, setDuplicateOf] = React.useState<
    DuplicateOfSummary | null
  >(current?.duplicate_of ?? null);
  const [savedAt, setSavedAt] = React.useState<string | undefined>(
    current?.updated_at,
  );
  const [error, setError] = React.useState<string | null>(null);
  const [pickerOpen, setPickerOpen] = React.useState(false);

  React.useEffect(() => {
    setLabel((current?.label as State) ?? "unlabeled");
    setNote(current?.researcher_note ?? "");
    setDuplicateOf(current?.duplicate_of ?? null);
    setSavedAt(current?.updated_at);
  }, [
    current?.label,
    current?.researcher_note,
    current?.updated_at,
    current?.duplicate_of?.id,
  ]);

  async function persistLabel(
    next: State,
    opts: {
      targetId?: string | null;
      targetSummary?: DuplicateOfSummary | null;
    } = {},
  ) {
    const targetId = opts.targetId ?? null;
    const targetSummary = opts.targetSummary ?? null;
    setError(null);
    try {
      const result = await saveFeedback.mutateAsync({
        label: next as FeedbackLabel,
        note,
        existing: Boolean(current),
        duplicateOfFindingId: targetId,
      });
      setLabel(next);
      setSavedAt(result.updated_at);
      // Prefer the server's resolved ``duplicate_of`` summary
      // (it carries the canonical's name + id from the DB);
      // fall back to the optimistic summary the picker passed
      // in if the server didn't include it.
      setDuplicateOf(result.duplicate_of ?? targetSummary ?? null);
      onChanged?.();
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to save feedback.",
      );
    }
  }

  async function applyLabel(next: State) {
    if (readOnly) {
      setError("Read-only access — contact an admin to upgrade your role.");
      return;
    }
    if (next === "duplicate") {
      // Duplicate requires a target; open picker. Persist runs
      // when the user confirms a target.
      if (!runId) {
        setError("Cannot mark as duplicate: run id is unavailable.");
        return;
      }
      setPickerOpen(true);
      return;
    }
    // Switching to anything else clears the canonical pointer
    // so the CHECK biconditional holds.
    await persistLabel(next, { targetId: null, targetSummary: null });
  }

  async function saveNote() {
    if (readOnly) {
      setError("Read-only access — contact an admin to upgrade your role.");
      return;
    }
    if (label === "unlabeled") {
      setError(
        "Pick True positive, False positive, or Duplicate before saving a note.",
      );
      return;
    }
    if (label === "duplicate" && !duplicateOf) {
      setError(
        "Pick the canonical finding before saving a note on a duplicate.",
      );
      return;
    }
    setError(null);
    try {
      const result = await saveFeedback.mutateAsync({
        label: label as FeedbackLabel,
        note,
        existing: Boolean(current),
        duplicateOfFindingId: duplicateOf?.id ?? null,
      });
      setSavedAt(result.updated_at);
      setDuplicateOf(result.duplicate_of ?? duplicateOf);
      onChanged?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save note.");
    }
  }

  return (
    <div className="rounded-lg border border-zinc-200 bg-zinc-50/60 p-3 dark:border-zinc-800 dark:bg-zinc-900/40">
      <div className="flex flex-wrap items-center gap-2">
        <FeedbackButton
          selected={label === "true_positive"}
          onClick={() => applyLabel("true_positive")}
          tone="success"
          icon={<ThumbsUp className="h-3.5 w-3.5" aria-hidden />}
          label="True positive"
          disabled={readOnly}
        />
        <FeedbackButton
          selected={label === "false_positive"}
          onClick={() => applyLabel("false_positive")}
          tone="danger"
          icon={<ThumbsDown className="h-3.5 w-3.5" aria-hidden />}
          label="False positive"
          disabled={readOnly}
        />
        <FeedbackButton
          selected={label === "duplicate"}
          onClick={() => applyLabel("duplicate")}
          tone="info"
          icon={<Copy className="h-3.5 w-3.5" aria-hidden />}
          label={
            label === "duplicate" && duplicateOf
              ? `Duplicate of ${duplicateOf.finding_id}`
              : "Duplicate"
          }
          disabled={readOnly}
        />
        <FeedbackButton
          selected={label === "unlabeled"}
          onClick={() => applyLabel("unlabeled")}
          tone="neutral"
          icon={<CircleSlash className="h-3.5 w-3.5" aria-hidden />}
          label="Unlabeled"
          disabled={readOnly}
        />
        {saveFeedback.isPending ? (
          <span className="text-xs text-zinc-500 dark:text-zinc-400">
            Saving…
          </span>
        ) : null}
      </div>
      <p className="mt-1.5 text-[11px] text-zinc-500 dark:text-zinc-400">
        Only mark as duplicate after confirming this is a real (valid)
        finding.
      </p>
      <div className="mt-3 space-y-1">
        <Textarea
          aria-label="Researcher note"
          placeholder={
            readOnly
              ? "Read-only access — contact an admin to upgrade your role"
              : "Optional note — why is this labeled this way?"
          }
          value={note}
          onChange={(event) => setNote(event.target.value)}
          rows={2}
          readOnly={readOnly}
          disabled={readOnly}
          title={
            readOnly
              ? "Read-only access — contact an admin to upgrade your role"
              : undefined
          }
        />
        <div className="flex items-center justify-between">
          <span className="text-[11px] text-zinc-500 dark:text-zinc-400">
            {readOnly
              ? "Read-only access (viewer)"
              : current
              ? `Reviewer: ${current.reviewer_username} · updated ${formatRelative(
                  savedAt ?? current.updated_at,
                )}`
              : "No review yet"}
          </span>
          <Button
            size="sm"
            variant="secondary"
            onClick={saveNote}
            disabled={readOnly}
            title={
              readOnly
                ? "Read-only access — contact an admin to upgrade your role"
                : undefined
            }
          >
            <Check className="h-3.5 w-3.5" aria-hidden />
            Save note
          </Button>
        </div>
      </div>
      {error ? (
        <p
          role="alert"
          className="mt-2 rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:bg-rose-950 dark:text-rose-200"
        >
          {error}
        </p>
      ) : null}
      {pickerOpen && runId ? (
        <FindingPicker
          runId={runId}
          excludeFindingId={findingId}
          onPick={async (target) => {
            setPickerOpen(false);
            await persistLabel("duplicate", {
              targetId: target.id,
              targetSummary: target,
            });
          }}
          onClose={() => setPickerOpen(false)}
        />
      ) : null}
    </div>
  );
}

function FeedbackButton({
  selected,
  onClick,
  tone,
  icon,
  label,
  disabled,
}: {
  selected: boolean;
  onClick: () => void;
  tone: "success" | "danger" | "neutral" | "info";
  icon: React.ReactNode;
  label: string;
  disabled?: boolean;
}) {
  const toneClass = {
    success: selected
      ? "border-emerald-500 bg-emerald-50 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-200"
      : "border-zinc-300 text-zinc-700 hover:border-emerald-400 dark:border-zinc-700 dark:text-zinc-200 dark:hover:border-emerald-600",
    danger: selected
      ? "border-rose-500 bg-rose-50 text-rose-900 dark:bg-rose-950 dark:text-rose-200"
      : "border-zinc-300 text-zinc-700 hover:border-rose-400 dark:border-zinc-700 dark:text-zinc-200 dark:hover:border-rose-600",
    info: selected
      ? "border-sky-500 bg-sky-50 text-sky-900 dark:bg-sky-950 dark:text-sky-200"
      : "border-zinc-300 text-zinc-700 hover:border-sky-400 dark:border-zinc-700 dark:text-zinc-200 dark:hover:border-sky-600",
    neutral: selected
      ? "border-zinc-500 bg-zinc-100 text-zinc-900 dark:bg-zinc-800 dark:text-zinc-100"
      : "border-zinc-300 text-zinc-500 hover:border-zinc-500 dark:border-zinc-700 dark:text-zinc-400 dark:hover:border-zinc-500",
  }[tone];
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition-colors",
        toneClass,
        disabled && "cursor-not-allowed opacity-50",
      )}
      aria-pressed={selected}
      aria-disabled={disabled}
      title={disabled ? "Read-only access — contact an admin to upgrade your role" : undefined}
    >
      {icon}
      {label}
    </button>
  );
}
