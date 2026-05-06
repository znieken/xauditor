"use client";

import { Check, ChevronDown } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/utils";

export interface MultiSelectOption {
  value: string;
  label: string;
}

/**
 * Small popover-style multi-select for closed-enum filters.
 *
 * The trigger button shows a one-line summary of what's selected. The
 * popover renders each option as a checkbox row. Clicking outside or
 * pressing Esc closes the popover.
 *
 * Keyboard: Tab moves focus; Space/Enter on a row toggles its checkbox
 * (the native checkbox owns this); Esc on any focused element inside
 * the popover closes it and returns focus to the trigger.
 */
export function MultiSelect({
  id,
  label,
  options,
  value,
  onChange,
  emptyLabel = "Any",
}: {
  id: string;
  label: string;
  options: MultiSelectOption[];
  value: string[];
  onChange: (next: string[]) => void;
  emptyLabel?: string;
}) {
  const [open, setOpen] = React.useState(false);
  const containerRef = React.useRef<HTMLDivElement>(null);
  const triggerRef = React.useRef<HTMLButtonElement>(null);

  // Close when focus leaves the popover entirely (covers click-outside +
  // tab-out without a separate document listener for each instance).
  React.useEffect(() => {
    if (!open) return;
    function onDocPointer(event: PointerEvent) {
      if (!containerRef.current) return;
      if (!containerRef.current.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", onDocPointer);
    return () => document.removeEventListener("pointerdown", onDocPointer);
  }, [open]);

  function toggle(optionValue: string) {
    const set = new Set(value);
    if (set.has(optionValue)) set.delete(optionValue);
    else set.add(optionValue);
    onChange(Array.from(set));
  }

  function onKeyDown(event: React.KeyboardEvent) {
    if (event.key === "Escape") {
      event.stopPropagation();
      setOpen(false);
      triggerRef.current?.focus();
    }
  }

  const summary =
    value.length === 0
      ? emptyLabel
      : value.length === options.length
        ? `All (${value.length})`
        : value.length === 1
          ? value[0]
          : `${value.length} selected`;

  return (
    <div className="relative" ref={containerRef} onKeyDown={onKeyDown}>
      <button
        id={id}
        ref={triggerRef}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={`${label}: ${summary}`}
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "flex h-8 w-full items-center justify-between rounded-md border border-zinc-300 bg-white px-2 text-sm",
          "hover:border-zinc-400 focus:outline-none focus-visible:ring-2 focus-visible:ring-zinc-400",
          "dark:border-zinc-700 dark:bg-zinc-900 dark:hover:border-zinc-600",
        )}
      >
        <span className="truncate">{summary}</span>
        <ChevronDown className="h-3.5 w-3.5 shrink-0 opacity-60" aria-hidden />
      </button>
      {open ? (
        <div
          role="listbox"
          aria-multiselectable="true"
          aria-labelledby={id}
          className={cn(
            "absolute z-20 mt-1 max-h-72 w-full min-w-[12rem] overflow-auto rounded-md border border-zinc-200 bg-white py-1 shadow-md",
            "dark:border-zinc-800 dark:bg-zinc-900",
          )}
        >
          {options.map((option) => {
            const checked = value.includes(option.value);
            return (
              <label
                key={option.value}
                className={cn(
                  "flex cursor-pointer items-center gap-2 px-2 py-1 text-sm",
                  "hover:bg-zinc-100 dark:hover:bg-zinc-800",
                )}
              >
                <input
                  type="checkbox"
                  className="sr-only"
                  checked={checked}
                  onChange={() => toggle(option.value)}
                />
                <span
                  aria-hidden
                  className={cn(
                    "flex h-4 w-4 shrink-0 items-center justify-center rounded border",
                    checked
                      ? "border-zinc-900 bg-zinc-900 text-white dark:border-zinc-100 dark:bg-zinc-100 dark:text-zinc-900"
                      : "border-zinc-300 bg-white dark:border-zinc-700 dark:bg-zinc-900",
                  )}
                >
                  {checked ? <Check className="h-3 w-3" aria-hidden /> : null}
                </span>
                <span className="truncate">{option.label}</span>
              </label>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
