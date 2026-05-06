import { Badge } from "@/components/ui/badge";
import type { RunStatus, StagesForm } from "@/lib/types";

const STATUS_TONE: Record<RunStatus, React.ComponentProps<typeof Badge>["tone"]> = {
  in_progress: "info",
  completed: "success",
  failed: "danger",
  cancelled: "warning",
};

const STATUS_LABEL: Record<RunStatus, string> = {
  in_progress: "In Progress",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

export function StatusChip({ status }: { status: RunStatus | string }) {
  const key = status as RunStatus;
  return (
    <Badge tone={STATUS_TONE[key] ?? "neutral"}>
      {STATUS_LABEL[key] ?? status}
    </Badge>
  );
}

export function ModeChip({ mode }: { mode: "fast" | "deep" | string }) {
  return (
    <Badge tone={mode === "deep" ? "accent" : "neutral"}>
      {mode === "deep" ? "Deep mode" : "Fast mode"}
    </Badge>
  );
}

/**
 * Stage-call form chip — sits next to ModeChip / StatusChip in the run
 * header. `prompt` (default form, neutral tone) routes through LangChain
 * providers; `agentic` (info/sky tone) routes through coder-service's
 * /agent_invocations endpoint.
 *
 * `compact` shrinks padding and drops the `Stages:` prefix for the
 * run-list table where chip space is tight.
 */
export function StagesFormChip({
  stages_form,
  compact = false,
}: {
  stages_form: StagesForm | string;
  compact?: boolean;
}) {
  const isAgentic = stages_form === "agentic";
  const label = compact
    ? isAgentic
      ? "agentic"
      : "prompt"
    : `Stages: ${isAgentic ? "agentic" : "prompt"}`;
  return <Badge tone={isAgentic ? "info" : "neutral"}>{label}</Badge>;
}
