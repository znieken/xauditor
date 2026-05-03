import { AlertTriangle, Terminal } from "lucide-react";
import type { ConfigSource } from "@/lib/types";

interface Props {
  ymlKey: string;
  source?: ConfigSource;
}

export function OverrideBadge({ ymlKey, source = "yml" }: Props) {
  if (source === "env") {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full border border-sky-300 bg-sky-50 px-2 py-0.5 text-[11px] font-medium text-sky-900 dark:border-sky-700 dark:bg-sky-950 dark:text-sky-200"
        title={`Set by an XAUDITOR_* environment variable (${ymlKey}). UI edits are ignored until the env var is unset.`}
      >
        <Terminal className="h-3 w-3" aria-hidden />
        env override
      </span>
    );
  }
  return (
    <span
      className="inline-flex items-center gap-1 rounded-full border border-amber-300 bg-amber-50 px-2 py-0.5 text-[11px] font-medium text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-200"
      title={`Overridden by xauditor.yml (${ymlKey}). UI edits are ignored until removed from yml.`}
    >
      <AlertTriangle className="h-3 w-3" aria-hidden />
      yml override
    </span>
  );
}
