"use client";

import * as React from "react";
import { ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";

export interface ConfirmActionDialogProps {
  /** Dialog heading. Short, imperative ("Disable user"). */
  title: string;
  /**
   * Optional subtitle rendered under the heading. Use this to surface
   * context the admin needs to confirm they're acting on the right
   * target (username, project name, started-at timestamp, current state).
   */
  subtitle?: React.ReactNode;
  /**
   * Body text describing what the action does and any side effects the
   * admin should be aware of (sessions revoked, cascade deletes, etc.).
   */
  description: React.ReactNode;
  /** Submit-button label. Defaults to ``Confirm``. */
  confirmLabel?: string;
  /**
   * Destructive actions get a danger-toned submit button so the admin
   * gets a visual cue distinct from routine state flips.
   */
  destructive?: boolean;
  /**
   * Set to render a single-line free-text reason input above the buttons.
   * The trimmed value is passed to ``onConfirm``; an empty input becomes
   * ``null``. Default ``false``.
   */
  reasonInput?: boolean;
  /** Placeholder for the reason input when enabled. */
  reasonPlaceholder?: string;
  /** Generic error message used when the API throws a non-ApiError. */
  fallbackErrorMessage?: string;
  /**
   * Optional extra footer button rendered between "Cancel" (close) and the
   * primary submit button. Use this to offer an alternative path that
   * keeps the dialog open — e.g. "Cancel run instead" on a Delete dialog
   * when the run is still in_progress, which flips the surrounding state
   * to swap the dialog over to the Cancel flow without making the admin
   * re-traverse the menu.
   */
  secondaryAction?: {
    label: string;
    onClick: () => void;
  };
  /**
   * Called when the user submits the confirmation. The dialog closes on
   * resolve and stays open with an inline error on reject so the admin
   * can retry without re-opening from scratch.
   */
  onConfirm: (reason: string | null) => Promise<void>;
  onClose: () => void;
}

export function ConfirmActionDialog({
  title,
  subtitle,
  description,
  confirmLabel = "Confirm",
  destructive = false,
  reasonInput = false,
  reasonPlaceholder = "Optional note for the audit log",
  fallbackErrorMessage = "The action failed.",
  secondaryAction,
  onConfirm,
  onClose,
}: ConfirmActionDialogProps) {
  const [reason, setReason] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      const trimmed = reason.trim();
      await onConfirm(trimmed.length > 0 ? trimmed : null);
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : fallbackErrorMessage);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-zinc-950/60 p-6 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirm-action-title"
    >
      <Card className="w-full max-w-md">
        <CardHeader>
          <CardTitle id="confirm-action-title">{title}</CardTitle>
          {subtitle ? (
            <div className="mt-1 text-xs text-zinc-500 dark:text-zinc-400">
              {subtitle}
            </div>
          ) : null}
        </CardHeader>
        <CardContent>
          <div className="mb-4 text-sm text-zinc-600 dark:text-zinc-300">
            {description}
          </div>
          <form onSubmit={handleSubmit} className="space-y-3">
            {reasonInput ? (
              <div className="space-y-1">
                <Label htmlFor="confirm-action-reason">Reason (optional)</Label>
                <Input
                  id="confirm-action-reason"
                  value={reason}
                  onChange={(event) => setReason(event.target.value)}
                  placeholder={reasonPlaceholder}
                  autoFocus
                  maxLength={2048}
                />
              </div>
            ) : null}
            {error ? (
              <p
                role="alert"
                className="rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:bg-rose-950 dark:text-rose-200"
              >
                {error}
              </p>
            ) : null}
            <div className="flex justify-end gap-2 pt-1">
              <Button
                type="button"
                variant="outline"
                onClick={onClose}
                disabled={submitting}
              >
                Cancel
              </Button>
              {secondaryAction ? (
                <Button
                  type="button"
                  variant="outline"
                  onClick={secondaryAction.onClick}
                  disabled={submitting}
                >
                  {secondaryAction.label}
                </Button>
              ) : null}
              <Button
                type="submit"
                disabled={submitting}
                className={
                  destructive
                    ? "bg-rose-600 text-white hover:bg-rose-700 dark:bg-rose-700 dark:hover:bg-rose-600"
                    : undefined
                }
              >
                {submitting ? "Working…" : confirmLabel}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
