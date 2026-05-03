import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium transition-colors",
  {
    variants: {
      tone: {
        neutral:
          "border-zinc-300 bg-zinc-100 text-zinc-800 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-200",
        info:
          "border-sky-300 bg-sky-50 text-sky-900 dark:border-sky-800 dark:bg-sky-950 dark:text-sky-200",
        success:
          "border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
        warning:
          "border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-200",
        danger:
          "border-rose-300 bg-rose-50 text-rose-900 dark:border-rose-800 dark:bg-rose-950 dark:text-rose-200",
        accent:
          "border-violet-300 bg-violet-50 text-violet-900 dark:border-violet-800 dark:bg-violet-950 dark:text-violet-200",
      },
    },
    defaultVariants: { tone: "neutral" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export const Badge = React.forwardRef<HTMLSpanElement, BadgeProps>(
  ({ className, tone, ...props }, ref) => (
    <span
      ref={ref}
      className={cn(badgeVariants({ tone }), className)}
      {...props}
    />
  ),
);
Badge.displayName = "Badge";

export function confidenceTone(
  level: string,
): "success" | "warning" | "danger" | "neutral" {
  const v = (level || "").toLowerCase();
  if (v === "high") return "danger";
  if (v === "medium") return "warning";
  if (v === "low") return "info" as never; // fall-through via neutral tone visually
  return "neutral";
}

export function validationTone(
  status: string,
):
  | "success"
  | "info"
  | "warning"
  | "danger"
  | "neutral" {
  const v = (status || "").toLowerCase();
  if (v === "valid") return "success";
  if (v === "partial valid") return "info";
  if (v === "inconclusive") return "warning";
  if (v === "false positive") return "neutral";
  return "neutral";
}

export function exploitationTone(
  status: string,
): "success" | "warning" | "danger" | "neutral" {
  const v = (status || "").toLowerCase();
  if (v === "exploitable") return "danger";
  if (v === "uncertain") return "warning";
  if (v === "not_exploitable" || v === "not exploitable") return "success";
  return "neutral";
}

export function feedbackTone(
  label: string | null,
): "success" | "danger" | "neutral" {
  if (label === "true_positive") return "success";
  if (label === "false_positive") return "danger";
  return "neutral";
}

export function coderTone(
  status: string,
): "success" | "danger" | "warning" | "info" | "neutral" {
  // Maps the canonical CODER_STATUSES values defined in
  // src/xauditor/models.py:
  //   Verified -> success, Not Verified -> danger,
  //   Inconclusive -> warning, Pending -> info, Skipped -> neutral,
  //   Fail -> danger (transport-level failure; distinct from
  //   Not Verified via the icon/label rendered alongside the chip).
  const v = (status || "").toLowerCase();
  if (v === "verified") return "success";
  if (v === "not verified") return "danger";
  if (v === "inconclusive") return "warning";
  if (v === "pending") return "info";
  if (v === "fail") return "danger";
  return "neutral";
}
