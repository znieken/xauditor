import type { ConfigField, ConfigSource } from "@/lib/types";

const READ_ONLY_SOURCES: ReadonlySet<ConfigSource> = new Set(["yml", "env"]);

/**
 * Render rule for the Settings page: any field whose effective source
 * is `yml` or `env` is read-only, regardless of whether the DB snapshot
 * also names the same key. The legacy `overridden_by_yml` flag is
 * preserved on the wire for back-compat consumers, but the UI no
 * longer keys gating off it.
 */
export function isReadOnly(
  fields: Record<string, ConfigField | undefined>,
  key: string,
): boolean {
  const source = fields[key]?.source;
  if (!source) return false;
  return READ_ONLY_SOURCES.has(source);
}

/**
 * Return the source tag for a key, or `undefined` if the key has no
 * resolved field (typical for keys that have no value at any level).
 */
export function sourceOf(
  fields: Record<string, ConfigField | undefined>,
  key: string,
): ConfigSource | undefined {
  return fields[key]?.source;
}

/**
 * Count the number of UI-editable keys under ``prefix`` whose source
 * makes them read-only. Used to populate the badge counts on the
 * Settings page TOC and section headers.
 */
export function readOnlyCountForPrefix(
  fields: Record<string, ConfigField | undefined>,
  prefix: string,
): number {
  let n = 0;
  for (const [key, field] of Object.entries(fields)) {
    if (!field) continue;
    if (!key.startsWith(prefix)) continue;
    if (READ_ONLY_SOURCES.has(field.source)) n += 1;
  }
  return n;
}

export const READ_ONLY_CONFIG_SOURCES = READ_ONLY_SOURCES;
