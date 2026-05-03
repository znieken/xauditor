"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

export interface SettingsTocEntry {
  id: string;
  label: string;
  badgeCount?: number;
}

export function SettingsToc({ entries }: { entries: SettingsTocEntry[] }) {
  return (
    <nav
      aria-label="Settings sections"
      className="rounded-xl border border-zinc-200 bg-zinc-50/60 px-4 py-3 dark:border-zinc-800 dark:bg-zinc-900/40"
    >
      <p className="mb-2 text-[11px] font-medium uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
        Sections
      </p>
      <ul className="flex flex-wrap gap-x-4 gap-y-1.5">
        {entries.map((entry) => (
          <li key={entry.id}>
            <a
              href={`#${entry.id}`}
              className="inline-flex items-center gap-1.5 text-xs text-zinc-700 hover:underline dark:text-zinc-300"
            >
              <span>{entry.label}</span>
              {entry.badgeCount && entry.badgeCount > 0 ? (
                <span
                  className={cn(
                    "inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-amber-200 px-1 text-[10px] font-semibold text-amber-900",
                    "dark:bg-amber-800 dark:text-amber-100",
                  )}
                  title={`${entry.badgeCount} read-only-source key${
                    entry.badgeCount === 1 ? "" : "s"
                  } in this section`}
                >
                  {entry.badgeCount}
                </span>
              ) : null}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}
