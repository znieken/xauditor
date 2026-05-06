"use client";

import * as React from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label, Select } from "@/components/ui/input";
import { ChipInput } from "@/components/chip-input";
import { OverrideBadge } from "@/components/override-badge";
import { NumericField } from "@/components/numeric-field";
import { isReadOnly, sourceOf } from "@/lib/config-source";
import { cn } from "@/lib/utils";
import type { ConfigField } from "@/lib/types";

// Audit-mode section. Renamed from `TeamingSection` in
// `restructure-audit-modes-and-coverage` Phase 1C. Surfaces the
// canonical `audit.*` controls (mode preset + replication + debate
// + per-unit cap + per-stage `provider_list`). Phase 1B
// (`migrate-provider-list-to-audit-namespace`) finished the
// provider_list migration to `audit.<stage>.provider_list`; the
// legacy `teaming.<stage>.provider_list` keys still load through
// the migration shim for one minor release.
const TEAMS = ["analyzer", "validator", "exploiter"] as const;
type Team = (typeof TEAMS)[number];

export interface ReplicationDraft {
  analyzer?: string;
  validator?: string;
  exploiter?: string;
}

export interface ValidatorDebateDraft {
  enabled?: string;
  max_rounds?: string;
  halt_on_consensus?: string;
}

export interface ProviderListsDraft {
  // Canonical `audit.<stage>.provider_list` — required when mode is
  // "deep". The migration shim still accepts the legacy
  // `teaming.<stage>.provider_list` keys with a deprecation warning.
  analyzer?: string[];
  validator?: string[];
  exploiter?: string[];
}

export interface AuditModeDraft {
  mode?: string;
  max_findings_per_unit?: string;
  replication?: ReplicationDraft;
  validator_debate?: ValidatorDebateDraft;
  provider_lists?: ProviderListsDraft;
  // `portal-coverage-panel`: yes/no value for
  // `audit.coverage_gaps.report`. Empty when the operator
  // hasn't touched the toggle (effective value falls back
  // to the current config).
  coverage_gaps_report?: string;
}

interface Props {
  fields: Record<string, ConfigField | undefined>;
  providerNames: string[];
  draft: AuditModeDraft;
  onChange: (patch: AuditModeDraft) => void;
}

function rawValue(
  fields: Record<string, ConfigField | undefined>,
  key: string,
): string {
  const raw = fields[key]?.value;
  if (raw === undefined || raw === null) return "";
  return String(raw);
}

function rawList(
  fields: Record<string, ConfigField | undefined>,
  key: string,
): string[] {
  const raw = fields[key]?.value;
  if (Array.isArray(raw)) return raw.map(String);
  return [];
}

export function AuditModeSection({
  fields,
  providerNames,
  draft,
  onChange,
}: Props) {
  const modeKey = "audit.mode";
  const modeReadOnly = isReadOnly(fields, modeKey);
  const modeSource = sourceOf(fields, modeKey);
  const modeField = fields[modeKey];
  const modeValue =
    draft.mode ?? rawValue(fields, modeKey) ?? "fast";
  const deepActive = modeValue === "deep";

  const replication = draft.replication ?? {};
  const debate = draft.validator_debate ?? {};
  const providerLists = draft.provider_lists ?? {};

  function updateReplication(patch: ReplicationDraft): void {
    onChange({ replication: { ...replication, ...patch } });
  }

  function updateDebate(patch: ValidatorDebateDraft): void {
    onChange({ validator_debate: { ...debate, ...patch } });
  }

  function updateProviderList(team: Team, value: string[]): void {
    onChange({
      provider_lists: { ...providerLists, [team]: value },
    });
  }

  const validateProvider = providerNames.length
    ? (chip: string) =>
        providerNames.includes(chip) ? null : `Unknown provider '${chip}'`
    : undefined;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Audit mode</CardTitle>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          <span className="font-mono">fast</span> = single-replica
          prompt-driven chain (CI / batch).{" "}
          <span className="font-mono">deep</span> = multi-replica
          agentic chain with validator debate (PSIRT depth review).
          Cost scales ~linearly in the replication counts.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {/* Mode preset */}
        <div
          className={cn(
            "grid grid-cols-1 gap-2 md:grid-cols-[240px_1fr_auto] md:items-center",
            modeReadOnly && "opacity-70",
          )}
        >
          <div className="space-y-0.5">
            <Label htmlFor={modeKey}>Audit mode</Label>
            <div className="flex flex-wrap items-center gap-1.5">
              {modeReadOnly ? (
                <OverrideBadge ymlKey={modeKey} source={modeSource} />
              ) : null}
              {modeField ? (
                <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
                  src: {modeField.source}
                </span>
              ) : null}
            </div>
          </div>
          <div>
            <Select
              id={modeKey}
              value={modeValue || "fast"}
              disabled={modeReadOnly}
              onChange={(event) => onChange({ mode: event.target.value })}
            >
              <option value="fast">fast</option>
              <option value="deep">deep</option>
            </Select>
          </div>
          <div />
        </div>

        {/* Per-unit finding cap (fast mode only useful below). */}
        <NumericField
          fullKey="audit.max_findings_per_unit"
          label="Max findings per unit"
          fields={fields}
          value={
            draft.max_findings_per_unit ??
            rawValue(fields, "audit.max_findings_per_unit")
          }
          onChange={(next) => onChange({ max_findings_per_unit: next })}
          min={1}
          step="1"
        />

        {/* Advanced overrides — replication + debate + legacy provider_list */}
        <details className="rounded-lg border border-zinc-200 px-3 py-3 dark:border-zinc-800">
          <summary className="cursor-pointer select-none font-mono text-sm text-zinc-900 dark:text-zinc-100">
            Advanced overrides
          </summary>
          <div className="mt-3 space-y-4">
            <p className="text-xs text-zinc-500 dark:text-zinc-400">
              The mode preset already resolves these to sensible
              defaults. Override only when you know what you're doing —
              raising replication multiplies token cost.
            </p>

            <div className="space-y-2">
              <h4 className="font-mono text-xs uppercase tracking-wide text-zinc-500">
                Replication
              </h4>
              <NumericField
                fullKey="audit.replication.analyzer"
                label="analyzer"
                fields={fields}
                value={
                  replication.analyzer ??
                  rawValue(fields, "audit.replication.analyzer")
                }
                onChange={(next) => updateReplication({ analyzer: next })}
                min={1}
                step="1"
              />
              <NumericField
                fullKey="audit.replication.validator"
                label="validator"
                fields={fields}
                value={
                  replication.validator ??
                  rawValue(fields, "audit.replication.validator")
                }
                onChange={(next) => updateReplication({ validator: next })}
                min={1}
                step="1"
              />
              <NumericField
                fullKey="audit.replication.exploiter"
                label="exploiter"
                fields={fields}
                value={
                  replication.exploiter ??
                  rawValue(fields, "audit.replication.exploiter")
                }
                onChange={(next) => updateReplication({ exploiter: next })}
                min={1}
                step="1"
              />
            </div>

            <div className="space-y-2">
              <h4 className="font-mono text-xs uppercase tracking-wide text-zinc-500">
                Validator debate
              </h4>
              <div className="grid grid-cols-1 gap-2 md:grid-cols-[240px_1fr] md:items-center">
                <Label htmlFor="audit.validator.debate.enabled">
                  Debate enabled
                </Label>
                <Select
                  id="audit.validator.debate.enabled"
                  value={
                    debate.enabled ??
                    rawValue(fields, "audit.validator.debate.enabled") ??
                    "false"
                  }
                  disabled={isReadOnly(fields, "audit.validator.debate.enabled")}
                  onChange={(event) =>
                    updateDebate({ enabled: event.target.value })
                  }
                >
                  <option value="true">true</option>
                  <option value="false">false</option>
                </Select>
              </div>
              <NumericField
                fullKey="audit.validator.debate.max_rounds"
                label="max_rounds"
                fields={fields}
                value={
                  debate.max_rounds ??
                  rawValue(fields, "audit.validator.debate.max_rounds")
                }
                onChange={(next) => updateDebate({ max_rounds: next })}
                min={1}
                step="1"
              />
            </div>

            {/* Coverage Gaps reporting toggle (`portal-coverage-panel`).
             * Defaults to true; operators rarely need to flip it but
             * the control is here for completeness. */}
            <div className="space-y-2">
              <h4 className="font-mono text-xs uppercase tracking-wide text-zinc-500">
                Coverage Gaps
              </h4>
              <div className="grid grid-cols-1 gap-2 md:grid-cols-[240px_1fr] md:items-center">
                <Label htmlFor="audit.coverage_gaps.report">
                  Emit Coverage Gaps section
                </Label>
                <Select
                  id="audit.coverage_gaps.report"
                  value={
                    draft.coverage_gaps_report ??
                    rawValue(fields, "audit.coverage_gaps.report") ??
                    "true"
                  }
                  disabled={isReadOnly(fields, "audit.coverage_gaps.report")}
                  onChange={(event) =>
                    onChange({ coverage_gaps_report: event.target.value })
                  }
                >
                  <option value="true">true</option>
                  <option value="false">false</option>
                </Select>
              </div>
            </div>

            {/* Per-stage provider_list chips, bound to the canonical
              * `audit.<stage>.provider_list` keys. Required when mode
              * is "deep" (and only consumed in prompt-form runs;
              * agentic-form runs ignore analyzer/exploiter rotation
              * — see docs/audit-modes.md "Multi-model rotation per
              * stage × form"). */}
            <div className="space-y-3">
              <h4 className="font-mono text-xs uppercase tracking-wide text-zinc-500">
                Provider rotation
              </h4>
              <p className="text-[11px] text-zinc-500 dark:text-zinc-400">
                Each stage's subagents cycle through this list. Bound
                to{" "}
                <span className="font-mono">audit.&lt;stage&gt;.provider_list</span>;
                analyzer / exploiter rotation is silently ignored when
                stages.form is "agentic" (validator always honours it).
              </p>
              {TEAMS.map((team) => {
                const providerListKey = `audit.${team}.provider_list`;
                const providerListReadOnly = isReadOnly(fields, providerListKey);
                const providerListSource = sourceOf(fields, providerListKey);
                const providerListField = fields[providerListKey];
                const providerListValue =
                  providerLists[team] ?? rawList(fields, providerListKey);
                return (
                  <div
                    key={team}
                    className={cn(
                      "grid grid-cols-1 gap-2 md:grid-cols-[240px_1fr] md:items-start",
                      providerListReadOnly && "opacity-70",
                    )}
                  >
                    <div className="space-y-0.5">
                      <Label htmlFor={providerListKey}>{team}</Label>
                      <div className="flex flex-wrap items-center gap-1.5">
                        {providerListReadOnly ? (
                          <OverrideBadge
                            ymlKey={providerListKey}
                            source={providerListSource}
                          />
                        ) : null}
                        {providerListField ? (
                          <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
                            src: {providerListField.source}
                          </span>
                        ) : null}
                      </div>
                      {deepActive && providerListValue.length === 0 ? (
                        <p className="text-[11px] text-rose-600 dark:text-rose-300">
                          Required when audit mode is deep.
                        </p>
                      ) : null}
                    </div>
                    <ChipInput
                      id={providerListKey}
                      value={providerListValue}
                      onChange={(next) => updateProviderList(team, next)}
                      suggestions={providerNames}
                      validate={validateProvider}
                      disabled={providerListReadOnly}
                      placeholder={
                        providerNames.length
                          ? "Pick from configured providers"
                          : "(declare providers in xauditor.yml first)"
                      }
                    />
                  </div>
                );
              })}
            </div>
          </div>
        </details>
      </CardContent>
    </Card>
  );
}
