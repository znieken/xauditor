import { Badge } from "@/components/ui/badge";
import type { RunStatus } from "@/lib/types";

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

export function ModeChip({ mode }: { mode: "single" | "team" | string }) {
  return (
    <Badge tone={mode === "team" ? "accent" : "neutral"}>
      {mode === "team" ? "Team mode" : "Single mode"}
    </Badge>
  );
}
