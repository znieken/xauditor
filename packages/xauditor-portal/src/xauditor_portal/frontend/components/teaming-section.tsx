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

const TEAMS = ["analyzer", "validator", "exploiter"] as const;
type Team = (typeof TEAMS)[number];

export interface TeamDraft {
  subagent_count?: string;
  provider_list?: string[];
  debate_rounds?: string;
}

export type TeamingDraft = {
  enabled?: string;
} & Partial<Record<Team, TeamDraft>>;

interface Props {
  fields: Record<string, ConfigField | undefined>;
  providerNames: string[];
  draft: TeamingDraft;
  onChange: (patch: TeamingDraft) => void;
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

export function TeamingSection({
  fields,
  providerNames,
  draft,
  onChange,
}: Props) {
  const enabledKey = "teaming.enabled";
  const enabledReadOnly = isReadOnly(fields, enabledKey);
  const enabledSource = sourceOf(fields, enabledKey);
  const enabledField = fields[enabledKey];
  const enabledValue =
    draft.enabled ?? rawValue(fields, enabledKey) ?? "false";
  const teamingActive = enabledValue === "true";

  function teamDraft(team: Team): TeamDraft {
    return draft[team] ?? {};
  }

  function updateTeam(team: Team, patch: TeamDraft): void {
    onChange({ [team]: { ...(draft[team] ?? {}), ...patch } });
  }

  const validateProvider = providerNames.length
    ? (chip: string) =>
        providerNames.includes(chip) ? null : `Unknown provider '${chip}'`
    : undefined;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Teaming mode</CardTitle>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Multi-subagent orchestration. Audit cost scales ~linearly in the
          subagent counts. Each team's <span className="font-mono">provider_list</span>{" "}
          is required when teaming is enabled.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        <div
          className={cn(
            "grid grid-cols-1 gap-2 md:grid-cols-[240px_1fr_auto] md:items-center",
            enabledReadOnly && "opacity-70",
          )}
        >
          <div className="space-y-0.5">
            <Label htmlFor={enabledKey}>Teaming mode enabled</Label>
            <div className="flex flex-wrap items-center gap-1.5">
              {enabledReadOnly ? (
                <OverrideBadge ymlKey={enabledKey} source={enabledSource} />
              ) : null}
              {enabledField ? (
                <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
                  src: {enabledField.source}
                </span>
              ) : null}
            </div>
          </div>
          <div>
            <Select
              id={enabledKey}
              value={enabledValue === "" ? "false" : enabledValue}
              disabled={enabledReadOnly}
              onChange={(event) => onChange({ enabled: event.target.value })}
            >
              <option value="true">true</option>
              <option value="false">false</option>
            </Select>
          </div>
          <div />
        </div>
        {TEAMS.map((team) => {
          const subagentKey = `teaming.${team}.subagent_count`;
          const providerListKey = `teaming.${team}.provider_list`;
          const debateRoundsKey = `teaming.${team}.debate_rounds`;
          const draftForTeam = teamDraft(team);
          const providerListValue =
            draftForTeam.provider_list ?? rawList(fields, providerListKey);
          const providerListReadOnly = isReadOnly(fields, providerListKey);
          const providerListSource = sourceOf(fields, providerListKey);
          const providerListField = fields[providerListKey];
          return (
            <div
              key={team}
              className="space-y-3 rounded-lg border border-zinc-200 px-3 py-3 dark:border-zinc-800"
            >
              <h3 className="font-mono text-sm text-zinc-900 dark:text-zinc-100">
                {team}
              </h3>
              <NumericField
                fullKey={subagentKey}
                label="Subagent count"
                fields={fields}
                value={
                  draftForTeam.subagent_count ?? rawValue(fields, subagentKey)
                }
                onChange={(next) => updateTeam(team, { subagent_count: next })}
                min={1}
                step="1"
              />
              {team === "validator" ? (
                <NumericField
                  fullKey={debateRoundsKey}
                  label="Debate rounds"
                  fields={fields}
                  value={
                    draftForTeam.debate_rounds ?? rawValue(fields, debateRoundsKey)
                  }
                  onChange={(next) => updateTeam(team, { debate_rounds: next })}
                  min={1}
                  step="1"
                />
              ) : null}
              <div
                className={cn(
                  "grid grid-cols-1 gap-2 md:grid-cols-[240px_1fr] md:items-start",
                  providerListReadOnly && "opacity-70",
                )}
              >
                <div className="space-y-0.5">
                  <Label htmlFor={providerListKey}>Provider list</Label>
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
                  {teamingActive && providerListValue.length === 0 ? (
                    <p className="text-[11px] text-rose-600 dark:text-rose-300">
                      Required when teaming is enabled.
                    </p>
                  ) : null}
                </div>
                <ChipInput
                  id={providerListKey}
                  value={providerListValue}
                  onChange={(next) =>
                    updateTeam(team, { provider_list: next })
                  }
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
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}
