"use client";

import * as React from "react";
import { Select } from "@/components/ui/input";

export const PAGE_SIZE_OPTIONS = [10, 25, 50, 100] as const;
export const DEFAULT_PAGE_SIZE = 25;

export type PageSizeScope =
  | "projects"
  | "builds"
  | "runs"
  | "coverage-modules"
  | "coverage-files"
  | "coverage-functions";

function storageKey(scope: PageSizeScope): string {
  return `xauditor.portal.pageSize.${scope}`;
}

export function readStoredPageSize(
  scope: PageSizeScope,
  fallback: number = DEFAULT_PAGE_SIZE,
): number {
  if (typeof window === "undefined") return fallback;
  try {
    const raw = window.localStorage.getItem(storageKey(scope));
    if (!raw) return fallback;
    const parsed = Number.parseInt(raw, 10);
    if (PAGE_SIZE_OPTIONS.includes(parsed as (typeof PAGE_SIZE_OPTIONS)[number])) {
      return parsed;
    }
  } catch {
    /* localStorage unavailable — fall through */
  }
  return fallback;
}

export function writeStoredPageSize(scope: PageSizeScope, value: number): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(storageKey(scope), String(value));
  } catch {
    /* storage full or denied — silently ignore */
  }
}

interface Props {
  value: number;
  onChange: (next: number) => void;
  scope: PageSizeScope;
}

export function PageSizeSelector({ value, onChange, scope }: Props) {
  return (
    <label className="flex items-center gap-2 text-xs text-zinc-500 dark:text-zinc-400">
      <span>Page size</span>
      <Select
        aria-label={`Page size for ${scope}`}
        value={String(value)}
        onChange={(event) => {
          const parsed = Number.parseInt(event.target.value, 10);
          if (
            PAGE_SIZE_OPTIONS.includes(
              parsed as (typeof PAGE_SIZE_OPTIONS)[number],
            )
          ) {
            writeStoredPageSize(scope, parsed);
            onChange(parsed);
          }
        }}
      >
        {PAGE_SIZE_OPTIONS.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </Select>
    </label>
  );
}
