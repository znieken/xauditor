"use client";

import { X } from "lucide-react";
import * as React from "react";
import { cn } from "@/lib/utils";

export interface ChipInputProps {
  value: string[];
  onChange: (next: string[]) => void;
  suggestions?: string[];
  validate?: (chip: string) => string | null;
  disabled?: boolean;
  id?: string;
  placeholder?: string;
  "aria-describedby"?: string;
}

const CHIP_SEPARATOR_RE = /[,\n;]/;

function splitPasteContent(text: string): string[] {
  return text
    .split(CHIP_SEPARATOR_RE)
    .map((segment) => segment.trim())
    .filter((segment) => segment.length > 0);
}

function dedupe(chips: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const chip of chips) {
    if (seen.has(chip)) continue;
    seen.add(chip);
    out.push(chip);
  }
  return out;
}

export function ChipInput({
  value,
  onChange,
  suggestions,
  validate,
  disabled,
  id,
  placeholder,
  "aria-describedby": ariaDescribedBy,
}: ChipInputProps) {
  const [draft, setDraft] = React.useState("");
  const [activeSuggestion, setActiveSuggestion] = React.useState(0);
  const inputRef = React.useRef<HTMLInputElement>(null);

  const filteredSuggestions = React.useMemo(() => {
    if (!suggestions || suggestions.length === 0) return [];
    const q = draft.trim().toLowerCase();
    const seen = new Set(value);
    const matches = suggestions.filter((s) => !seen.has(s));
    if (q === "") return matches;
    return matches.filter((s) => s.toLowerCase().includes(q));
  }, [suggestions, draft, value]);

  const showPopover = !disabled && draft.length > 0 && filteredSuggestions.length > 0;

  function commit(raw: string): void {
    if (disabled) return;
    const trimmed = raw.trim();
    if (trimmed === "") return;
    const next = dedupe([...value, trimmed]);
    if (next.length !== value.length) onChange(next);
    setDraft("");
    setActiveSuggestion(0);
  }

  function commitMany(raws: string[]): void {
    if (disabled) return;
    const additions = raws
      .map((s) => s.trim())
      .filter((s) => s.length > 0);
    if (additions.length === 0) return;
    const next = dedupe([...value, ...additions]);
    if (next.length !== value.length) onChange(next);
    setDraft("");
    setActiveSuggestion(0);
  }

  function removeAt(index: number): void {
    if (disabled) return;
    const next = value.slice(0, index).concat(value.slice(index + 1));
    onChange(next);
  }

  function handleKeyDown(event: React.KeyboardEvent<HTMLInputElement>): void {
    if (disabled) return;
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      if (showPopover && filteredSuggestions[activeSuggestion]) {
        commit(filteredSuggestions[activeSuggestion]);
      } else {
        commit(draft);
      }
      return;
    }
    if (event.key === "Backspace" && draft === "" && value.length > 0) {
      event.preventDefault();
      removeAt(value.length - 1);
      return;
    }
    if (showPopover && event.key === "ArrowDown") {
      event.preventDefault();
      setActiveSuggestion((i) => Math.min(i + 1, filteredSuggestions.length - 1));
      return;
    }
    if (showPopover && event.key === "ArrowUp") {
      event.preventDefault();
      setActiveSuggestion((i) => Math.max(i - 1, 0));
      return;
    }
    if (event.key === "Escape") {
      setActiveSuggestion(0);
    }
  }

  function handlePaste(event: React.ClipboardEvent<HTMLInputElement>): void {
    if (disabled) return;
    const text = event.clipboardData.getData("text");
    if (!CHIP_SEPARATOR_RE.test(text)) return;
    event.preventDefault();
    commitMany(splitPasteContent(text));
  }

  function handleBlur(): void {
    if (draft.trim() === "") return;
    commit(draft);
  }

  return (
    <div className="space-y-1">
      <div
        className={cn(
          "flex flex-wrap items-center gap-1.5 rounded-md border border-zinc-300 bg-white px-2 py-1.5 text-sm focus-within:ring-2 focus-within:ring-zinc-500 dark:border-zinc-700 dark:bg-zinc-900",
          disabled && "cursor-not-allowed opacity-50",
        )}
        onClick={() => inputRef.current?.focus()}
      >
        {value.map((chip, index) => {
          const error = validate ? validate(chip) : null;
          return (
            <span
              key={`${chip}-${index}`}
              title={error ?? undefined}
              className={cn(
                "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs",
                error
                  ? "border-rose-300 bg-rose-50 text-rose-800 dark:border-rose-700 dark:bg-rose-950 dark:text-rose-200"
                  : "border-zinc-300 bg-zinc-100 text-zinc-800 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-100",
              )}
            >
              <span className="font-mono">{chip}</span>
              {!disabled ? (
                <button
                  type="button"
                  aria-label={`Remove ${chip}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    removeAt(index);
                  }}
                  className="rounded-full p-0.5 text-zinc-500 hover:bg-zinc-200 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-700 dark:hover:text-zinc-100"
                >
                  <X className="h-3 w-3" aria-hidden />
                </button>
              ) : null}
            </span>
          );
        })}
        <input
          ref={inputRef}
          id={id}
          type="text"
          value={draft}
          disabled={disabled}
          aria-describedby={ariaDescribedBy}
          placeholder={value.length === 0 ? placeholder : undefined}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          onBlur={handleBlur}
          className="min-w-[8ch] flex-1 bg-transparent px-1 py-0.5 text-sm placeholder:text-zinc-400 focus:outline-none disabled:cursor-not-allowed dark:placeholder:text-zinc-500"
        />
      </div>
      {showPopover ? (
        <ul
          role="listbox"
          className="rounded-md border border-zinc-200 bg-white text-sm shadow-sm dark:border-zinc-800 dark:bg-zinc-900"
        >
          {filteredSuggestions.map((suggestion, index) => (
            <li
              key={suggestion}
              role="option"
              aria-selected={index === activeSuggestion}
              onMouseDown={(event) => {
                // onMouseDown fires before the input's onBlur, so the
                // chip lands before the popover closes.
                event.preventDefault();
                commit(suggestion);
              }}
              className={cn(
                "cursor-pointer px-3 py-1 font-mono text-xs",
                index === activeSuggestion
                  ? "bg-zinc-100 dark:bg-zinc-800"
                  : "hover:bg-zinc-50 dark:hover:bg-zinc-800/50",
              )}
            >
              {suggestion}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
