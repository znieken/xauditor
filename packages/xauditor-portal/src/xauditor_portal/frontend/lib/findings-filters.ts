// URL ↔ FindingFilters plumbing for the run findings page.
//
// The five enumerated-value filters (`confidence`, `validation_status`,
// `exploitation_status`, `feedback_label`, `coder_status`) round-trip as
// repeated query parameters: `?validation_status=Valid&validation_status=Inconclusive`.
//
// Two filters have a default-checked subset:
// - `validation_status` defaults to `[Valid, Partial Valid, Inconclusive]`
//   (hides `False Positive`).
// - `coder_status` defaults to every status except `Not Verified`.
// In both cases, an absent URL key yields the default; a present-but-empty
// key (e.g. `?coder_status=`) is the operator's "I unchecked everything"
// marker and suppresses the default on reload.

import type { FeedbackLabel, FindingFilters } from "@/lib/types";

export const VALIDATION_DEFAULT = [
  "Valid",
  "Partial Valid",
  "Inconclusive",
] as const;

export const CODER_STATUS_DEFAULT = [
  "Verified",
  "Inconclusive",
  "Fail",
  "Pending",
  "Skipped",
] as const;

export const CODER_STATUS_OPTIONS = [
  "Verified",
  "Not Verified",
  "Inconclusive",
  "Skipped",
  "Pending",
  "Fail",
] as const;

const ARRAY_KEYS = [
  "confidence",
  "validation_status",
  "exploitation_status",
  "feedback_label",
  "coder_status",
] as const;
const SCALAR_KEYS = ["file", "function", "q"] as const;

type ArrayKey = (typeof ARRAY_KEYS)[number];

export function parseFiltersFromSearchParams(
  sp: URLSearchParams | { has: (k: string) => boolean; get: (k: string) => string | null; getAll: (k: string) => string[] },
): FindingFilters {
  const out: FindingFilters = {};
  for (const k of SCALAR_KEYS) {
    const v = sp.get(k);
    if (v !== null && v !== "") out[k] = v;
  }
  for (const k of ARRAY_KEYS) {
    if (sp.has(k)) {
      const all = sp.getAll(k).filter((v) => v !== "");
      assignArrayKey(out, k, all);
    }
  }
  return out;
}

export function serializeFiltersToQueryString(filters: FindingFilters): string {
  const sp = new URLSearchParams();
  for (const k of SCALAR_KEYS) {
    const v = filters[k];
    if (v !== undefined && v !== "") sp.set(k, v);
  }
  for (const k of ARRAY_KEYS) {
    const v = readArrayKey(filters, k);
    if (v === undefined) continue;
    if (v.length === 0) {
      // For dimensions with a default-checked subset (validation_status,
      // coder_status), the empty marker tells the parser "user has
      // explicitly cleared this filter, do NOT apply the default on the
      // next render." For the other three arrays there is no default to
      // suppress, so we just omit the key.
      if (k === "validation_status" || k === "coder_status") {
        sp.append(k, "");
      }
      continue;
    }
    for (const item of v) sp.append(k, item);
  }
  return sp.toString();
}

function assignArrayKey(
  out: FindingFilters,
  key: ArrayKey,
  values: string[],
): void {
  if (key === "feedback_label") {
    out.feedback_label = values as FeedbackLabel[];
  } else {
    out[key] = values;
  }
}

function readArrayKey(
  filters: FindingFilters,
  key: ArrayKey,
): string[] | undefined {
  return filters[key];
}
