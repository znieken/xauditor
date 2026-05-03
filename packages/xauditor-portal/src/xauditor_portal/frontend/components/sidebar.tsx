"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import * as React from "react";
import { useMe } from "@/lib/api";
import { tierSatisfies } from "@/lib/auth";
import { TABS } from "@/lib/tabs";
import { cn } from "@/lib/utils";

export function Sidebar() {
  const pathname = usePathname();
  const { data: me } = useMe();
  const role = me?.role;

  const visibleTabs = React.useMemo(() => {
    return TABS.filter((tab) => {
      if (!tab.requiredRole) return true;
      if (!role) return false;
      return tierSatisfies(role, tab.requiredRole);
    });
  }, [role]);

  return (
    <aside className="flex w-60 shrink-0 flex-col border-r border-zinc-200 bg-white/60 dark:border-zinc-800 dark:bg-zinc-950/50">
      <Link
        href="/reports"
        className="flex items-center gap-2 px-5 py-4 text-sm font-semibold tracking-tight"
      >
        <span className="inline-flex h-7 w-7 items-center justify-center rounded-md bg-zinc-900 text-[10px] font-bold text-zinc-50 dark:bg-zinc-50 dark:text-zinc-900">
          xA
        </span>
        xauditor Portal
      </Link>
      <nav aria-label="Primary" className="flex flex-col gap-0.5 px-2 py-2">
        {visibleTabs.map((tab) => {
          const Icon = tab.icon;
          const active =
            pathname === tab.route || pathname.startsWith(tab.route + "/");
          return (
            <Link
              key={tab.id}
              href={tab.route}
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors",
                active
                  ? "bg-zinc-100 font-medium text-zinc-900 dark:bg-zinc-800 dark:text-zinc-50"
                  : "text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100",
              )}
            >
              <Icon className="h-4 w-4" aria-hidden />
              {tab.label}
            </Link>
          );
        })}
      </nav>
      <div className="mt-auto px-5 pb-4 text-[10px] uppercase tracking-wider text-zinc-400 dark:text-zinc-600">
        v0.1.0
      </div>
    </aside>
  );
}
