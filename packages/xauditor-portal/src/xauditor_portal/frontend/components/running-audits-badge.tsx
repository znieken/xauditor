import * as React from "react";
import { Badge } from "@/components/ui/badge";

export interface RunningAuditsBadgeProps {
  count: number;
  /**
   * Required so screen-reader users get the same information as sighted
   * users. Compose using the project name, e.g.
   * `aria-label={`${count} audits running for ${projectName}`}`.
   */
  "aria-label": string;
}

/**
 * A small pill that shows the integer `count` of in-progress audits with
 * a pulsing-ring animation. Returns `null` when `count <= 0` so callers
 * do not need to branch — the badge is invisible by design when nothing
 * is running, and the surrounding row stays pixel-identical to the
 * badge-less rendering.
 */
export function RunningAuditsBadge({
  count,
  "aria-label": ariaLabel,
}: RunningAuditsBadgeProps) {
  if (count <= 0) return null;
  return (
    <span
      className="relative inline-flex"
      role="status"
      aria-label={ariaLabel}
    >
      <span
        aria-hidden
        className="absolute inset-0 animate-ping rounded-full bg-sky-400/60 dark:bg-sky-500/40"
      />
      <Badge tone="info" className="relative tabular-nums" aria-hidden>
        {count}
      </Badge>
    </span>
  );
}
