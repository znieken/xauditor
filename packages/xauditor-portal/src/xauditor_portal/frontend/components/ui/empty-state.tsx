import * as React from "react";
import { cn } from "@/lib/utils";

export function EmptyState({
  title,
  description,
  icon,
  className,
}: {
  title: string;
  description?: string;
  icon?: React.ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center rounded-xl border border-dashed border-zinc-300 bg-white py-12 px-6 text-center dark:border-zinc-800 dark:bg-zinc-900/40",
        className,
      )}
    >
      {icon ? (
        <div className="mb-3 text-zinc-400 dark:text-zinc-500">{icon}</div>
      ) : null}
      <p className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
        {title}
      </p>
      {description ? (
        <p className="mt-1 max-w-sm text-xs text-zinc-500 dark:text-zinc-400">
          {description}
        </p>
      ) : null}
    </div>
  );
}
