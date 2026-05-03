"use client";

import * as React from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { OverrideBadge } from "@/components/override-badge";
import { validateSamplingValue } from "@/lib/validate-sampling";
import { isReadOnly as _isReadOnly } from "@/lib/config-source";
import { cn } from "@/lib/utils";
import type { ConfigField } from "@/lib/types";

const AGENTS = ["graph_builder", "auditor", "exploitation", "validator"] as const;
type Agent = (typeof AGENTS)[number];

export interface AgentDraft {
  provider?: string;
  temperature?: string;
  top_p?: string;
  top_k?: string;
  repetition_penalty?: string;
  thinking_enabled?: boolean;
  request_timeout_seconds?: string;
}

interface Props {
  providerNames: string[];
  defaultProvider: string | null;
  defaultProviderDraft?: string;
  onDefaultProviderChange: (value: string) => void;
  fields: Record<string, ConfigField>;
  drafts: Record<Agent, AgentDraft>;
  errors: Record<string, string>;
  onDraftChange: (agent: Agent, draft: AgentDraft) => void;
}

function isOverridden(fields: Record<string, ConfigField>, key: string): boolean {
  return _isReadOnly(fields, key);
}

function rawValue(
  fields: Record<string, ConfigField>,
  key: string,
): string {
  const raw = fields[key]?.value;
  if (raw === undefined || raw === null) return "";
  return String(raw);
}

export function AgentOverridesSection({
  providerNames,
  defaultProvider,
  defaultProviderDraft,
  onDefaultProviderChange,
  fields,
  drafts,
  errors,
  onDraftChange,
}: Props) {
  const defaultProviderOverridden = isOverridden(fields, "llm.default_provider");
  const defaultProviderValue =
    defaultProviderDraft ?? defaultProvider ?? "";
  return (
    <Card>
      <CardHeader>
        <CardTitle>Default provider &amp; per-agent overrides</CardTitle>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Each agent resolves to <span className="font-mono">llm.default_provider</span>{" "}
          unless it declares its own. Per-agent sampling fields layer on top of
          the selected provider's sampling fields.
        </p>
      </CardHeader>
      <CardContent className="space-y-5">
        <div
          className={cn(
            "grid grid-cols-1 gap-2 md:grid-cols-[200px_1fr] md:items-center",
            defaultProviderOverridden && "opacity-70",
          )}
        >
          <div className="space-y-0.5">
            <Label htmlFor="llm.default_provider">Default provider</Label>
            <div className="flex flex-wrap items-center gap-1.5">
              {defaultProviderOverridden ? (
                <OverrideBadge
                  ymlKey="llm.default_provider"
                  source={fields["llm.default_provider"]?.source}
                />
              ) : null}
              {fields["llm.default_provider"] ? (
                <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
                  src: {fields["llm.default_provider"].source}
                </span>
              ) : null}
            </div>
          </div>
          <Select
            id="llm.default_provider"
            value={defaultProviderValue}
            disabled={defaultProviderOverridden || providerNames.length === 0}
            onChange={(event) => onDefaultProviderChange(event.target.value)}
          >
            <option value="">— no default —</option>
            {providerNames.map((provider) => (
              <option key={provider} value={provider}>
                {provider}
              </option>
            ))}
          </Select>
        </div>

        {AGENTS.map((agent) => {
          const draft = drafts[agent];
          const agentKeyBase = `agents.${agent}.llm`;
          const providerKey = `${agentKeyBase}.provider`;
          const providerOverridden = isOverridden(fields, providerKey);
          const providerValue =
            draft?.provider ?? rawValue(fields, providerKey);
          const update = (patch: AgentDraft) =>
            onDraftChange(agent, { ...(draft ?? {}), ...patch });
          return (
            <div
              key={agent}
              className="space-y-3 rounded-lg border border-zinc-200 px-3 py-3 dark:border-zinc-800"
            >
              <h3 className="font-mono text-sm text-zinc-900 dark:text-zinc-100">
                {agent}
              </h3>
              <div
                className={cn(
                  "grid grid-cols-1 gap-2 md:grid-cols-[200px_1fr] md:items-center",
                  providerOverridden && "opacity-70",
                )}
              >
                <div className="space-y-0.5">
                  <Label htmlFor={providerKey}>Provider</Label>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {providerOverridden ? (
                      <OverrideBadge
                        ymlKey={providerKey}
                        source={fields[providerKey]?.source}
                      />
                    ) : null}
                    {fields[providerKey] ? (
                      <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
                        src: {fields[providerKey].source}
                      </span>
                    ) : null}
                  </div>
                </div>
                <Select
                  id={providerKey}
                  value={providerValue}
                  disabled={providerOverridden}
                  onChange={(event) => update({ provider: event.target.value })}
                >
                  <option value="">— use default —</option>
                  {providerNames.map((provider) => (
                    <option key={provider} value={provider}>
                      {provider}
                    </option>
                  ))}
                </Select>
              </div>
              <BooleanField
                label="Thinking enabled"
                fullKey={`${agentKeyBase}.thinking_enabled`}
                value={
                  draft?.thinking_enabled !== undefined
                    ? String(draft.thinking_enabled)
                    : rawValue(fields, `${agentKeyBase}.thinking_enabled`)
                }
                onChange={(value) =>
                  update({ thinking_enabled: value === "true" })
                }
                overridden={isOverridden(
                  fields,
                  `${agentKeyBase}.thinking_enabled`,
                )}
                sourceField={fields[`${agentKeyBase}.thinking_enabled`]}
              />
              <RequestTimeoutField
                fullKey={`${agentKeyBase}.request_timeout_seconds`}
                value={
                  draft?.request_timeout_seconds ??
                  rawValue(fields, `${agentKeyBase}.request_timeout_seconds`)
                }
                onChange={(value) =>
                  update({ request_timeout_seconds: value })
                }
                overridden={isOverridden(
                  fields,
                  `${agentKeyBase}.request_timeout_seconds`,
                )}
                sourceField={
                  fields[`${agentKeyBase}.request_timeout_seconds`]
                }
                error={
                  errors[`${agentKeyBase}.request_timeout_seconds`]
                }
              />
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                {(["temperature", "top_p", "top_k", "repetition_penalty"] as const).map(
                  (samplingField) => {
                    const fullKey = `${agentKeyBase}.${samplingField}`;
                    const draftValue = draft?.[samplingField];
                    return (
                      <SamplingField
                        key={samplingField}
                        samplingField={samplingField}
                        fullKey={fullKey}
                        value={
                          draftValue !== undefined
                            ? String(draftValue)
                            : rawValue(fields, fullKey)
                        }
                        onChange={(value) =>
                          update({
                            [samplingField]: value,
                          } as AgentDraft)
                        }
                        error={errors[fullKey]}
                        overridden={isOverridden(fields, fullKey)}
                        sourceField={fields[fullKey]}
                      />
                    );
                  },
                )}
              </div>
            </div>
          );
        })}
      </CardContent>
    </Card>
  );
}

function BooleanField({
  label,
  fullKey,
  value,
  onChange,
  overridden,
  sourceField,
}: {
  label: string;
  fullKey: string;
  value: string;
  onChange: (value: string) => void;
  overridden: boolean;
  sourceField: ConfigField | undefined;
}) {
  return (
    <div
      className={cn(
        "grid grid-cols-1 gap-2 md:grid-cols-[200px_1fr] md:items-center",
        overridden && "opacity-70",
      )}
    >
      <div className="space-y-0.5">
        <Label htmlFor={fullKey}>{label}</Label>
        <div className="flex flex-wrap items-center gap-1.5">
          {overridden ? (
            <OverrideBadge ymlKey={fullKey} source={sourceField?.source} />
          ) : null}
          {sourceField ? (
            <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
              src: {sourceField.source}
            </span>
          ) : null}
        </div>
      </div>
      <Select
        id={fullKey}
        value={value === "" ? "false" : value}
        disabled={overridden}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="true">true</option>
        <option value="false">false</option>
      </Select>
    </div>
  );
}

function RequestTimeoutField({
  fullKey,
  value,
  onChange,
  overridden,
  sourceField,
  error,
}: {
  fullKey: string;
  value: string;
  onChange: (value: string) => void;
  overridden: boolean;
  sourceField: ConfigField | undefined;
  error?: string;
}) {
  return (
    <div
      className={cn(
        "grid grid-cols-1 gap-2 md:grid-cols-[200px_1fr] md:items-center",
        overridden && "opacity-70",
      )}
    >
      <div className="space-y-0.5">
        <Label htmlFor={fullKey}>Request timeout (s)</Label>
        <div className="flex flex-wrap items-center gap-1.5">
          {overridden ? (
            <OverrideBadge ymlKey={fullKey} source={sourceField?.source} />
          ) : null}
          {sourceField ? (
            <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
              src: {sourceField.source}
            </span>
          ) : null}
        </div>
      </div>
      <div className="space-y-1">
        <Input
          id={fullKey}
          type="number"
          step="any"
          min={0}
          value={value}
          disabled={overridden}
          placeholder="(use provider value)"
          onChange={(event) => onChange(event.target.value)}
        />
        {error ? (
          <p className="text-[11px] text-rose-600 dark:text-rose-300">{error}</p>
        ) : null}
      </div>
    </div>
  );
}

function SamplingField({
  samplingField,
  fullKey,
  value,
  onChange,
  error,
  overridden,
  sourceField,
}: {
  samplingField: "temperature" | "top_p" | "top_k" | "repetition_penalty";
  fullKey: string;
  value: string;
  onChange: (value: string) => void;
  error?: string;
  overridden: boolean;
  sourceField: ConfigField | undefined;
}) {
  const label = samplingField.replace(/_/g, " ");
  return (
    <div className={cn("space-y-1", overridden && "opacity-70")}>
      <Label htmlFor={fullKey}>{label}</Label>
      <div className="flex flex-wrap items-center gap-1.5">
        {overridden ? (
          <OverrideBadge ymlKey={fullKey} source={sourceField?.source} />
        ) : null}
        {sourceField ? (
          <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
            src: {sourceField.source}
          </span>
        ) : null}
      </div>
      <Input
        id={fullKey}
        type="number"
        step={samplingField === "top_k" ? "1" : "any"}
        value={value}
        disabled={overridden}
        placeholder="(use provider value)"
        onBlur={(event) => {
          const raw = event.target.value;
          if (raw === "") return;
          const parsed = Number(raw);
          if (
            !Number.isNaN(parsed) &&
            !validateSamplingValue(samplingField, parsed)
          ) {
            onChange(raw);
          }
        }}
        onChange={(event) => onChange(event.target.value)}
      />
      {error ? (
        <p className="text-[11px] text-rose-600 dark:text-rose-300">{error}</p>
      ) : null}
    </div>
  );
}
