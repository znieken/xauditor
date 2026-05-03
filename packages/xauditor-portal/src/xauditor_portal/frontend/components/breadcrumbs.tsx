"use client";

import { ChevronRight } from "lucide-react";
import Link from "next/link";
import * as React from "react";

export interface BreadcrumbItem {
  label: string;
  href?: string;
}

export function Breadcrumbs({ items }: { items: BreadcrumbItem[] }) {
  return (
    <nav
      aria-label="Breadcrumb"
      className="flex flex-wrap items-center gap-1 text-xs text-zinc-500 dark:text-zinc-400"
    >
      {items.map((item, idx) => {
        const last = idx === items.length - 1;
        return (
          <React.Fragment key={`${item.label}-${idx}`}>
            {item.href && !last ? (
              <Link
                href={item.href}
                className="hover:text-zinc-900 hover:underline dark:hover:text-zinc-100"
              >
                {item.label}
              </Link>
            ) : (
              <span
                className={
                  last
                    ? "font-medium text-zinc-900 dark:text-zinc-100"
                    : undefined
                }
              >
                {item.label}
              </span>
            )}
            {!last ? (
              <ChevronRight
                className="h-3 w-3 text-zinc-400 dark:text-zinc-600"
                aria-hidden
              />
            ) : null}
          </React.Fragment>
        );
      })}
    </nav>
  );
}
