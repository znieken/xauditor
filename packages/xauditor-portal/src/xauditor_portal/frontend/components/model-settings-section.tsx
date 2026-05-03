"use client";

import { Lock } from "lucide-react";
import * as React from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { OverrideBadge } from "@/components/override-badge";
import { validateSamplingValue } from "@/lib/validate-sampling";
import { cn } from "@/lib/utils";
import type { ConfigField } from "@/lib/types";

export interface ProviderDraft {
  base_url?: string;
  model_name?: string;
  kind?: string;
  thinking_enabled?: boolean;
  thinking_effort?: string;
  request_timeout_seconds?: string;
  temperature?: string;
  top_p?: string;
  top_k?: string;
  repetition_penalty?: string;
}

const PROVIDER_KINDS = ["openai", "anthropic"] as const;
const PROVIDER_THINKING_EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const;

interface Props {
  providerNames: string[];
  fields: Record<string, ConfigField>;
  drafts: Record<string, ProviderDraft>;
  errors: Record<string, string>;
  onDraftChange: (providerName: string, draft: ProviderDraft) => void;
}

import { isReadOnly as _isReadOnly } from "@/lib/config-source";

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

function current(
  draft: ProviderDraft,
  fields: Record<string, ConfigField>,
  key: keyof ProviderDraft,
  fullKey: string,
): string {
  const draftValue = draft[key];
  if (draftValue !== undefined) return String(draftValue);
  return rawValue(fields, fullKey);
}

export function ModelSettingsSection({
  providerNames,
  fields,
  drafts,
  errors,
  onDraftChange,
}: Props) {
  if (providerNames.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>LLM providers</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-zinc-500 dark:text-zinc-400">
            No LLM providers are configured. Declare at least one provider
            under <span className="font-mono">llm.providers.&lt;name&gt;</span>{" "}
            in <span className="font-mono">xauditor.yml</span> for it to
            appear here.
          </p>
        </CardContent>
      </Card>
    );
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>LLM providers</CardTitle>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Each provider defines how xauditor reaches a specific LLM endpoint.
          Fields that appear in <span className="font-mono">xauditor.yml</span>{" "}
          are locked with a badge — remove them from yml to take ownership from
          the UI.{" "}
          <span className="font-mono">api_key</span> is always yml-only.
        </p>
      </CardHeader>
      <CardContent className="space-y-5">
        {providerNames.map((name) => {
          const draft = drafts[name] ?? {};
          const update = (patch: ProviderDraft) =>
            onDraftChange(name, { ...draft, ...patch });
          return (
            <div
              key={name}
              className="space-y-3 rounded-lg border border-zinc-200 px-3 py-3 dark:border-zinc-800"
            >
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="font-mono text-sm text-zinc-900 dark:text-zinc-100">
                  {name}
                </h3>
              </div>
              <StringField
                label="Base URL"
                fullKey={`llm.providers.${name}.base_url`}
                value={current(draft, fields, "base_url", `llm.providers.${name}.base_url`)}
                onChange={(value) => update({ base_url: value })}
                overridden={isOverridden(fields, `llm.providers.${name}.base_url`)}
                sourceField={fields[`llm.providers.${name}.base_url`]}
              />
              <StringField
                label="Model name"
                fullKey={`llm.providers.${name}.model_name`}
                value={current(draft, fields, "model_name", `llm.providers.${name}.model_name`)}
                onChange={(value) => update({ model_name: value })}
                overridden={isOverridden(fields, `llm.providers.${name}.model_name`)}
                sourceField={fields[`llm.providers.${name}.model_name`]}
              />
              <ApiKeyReadOnly />
              <KindField
                fullKey={`llm.providers.${name}.kind`}
                value={current(draft, fields, "kind", `llm.providers.${name}.kind`)}
                onChange={(value) => update({ kind: value })}
                overridden={isOverridden(fields, `llm.providers.${name}.kind`)}
                sourceField={fields[`llm.providers.${name}.kind`]}
              />
              <BooleanField
                label="Thinking enabled"
                fullKey={`llm.providers.${name}.thinking_enabled`}
                value={current(
                  draft,
                  fields,
                  "thinking_enabled",
                  `llm.providers.${name}.thinking_enabled`,
                )}
                onChange={(value) =>
                  update({ thinking_enabled: value === "true" })
                }
                overridden={isOverridden(
                  fields,
                  `llm.providers.${name}.thinking_enabled`,
                )}
                sourceField={fields[`llm.providers.${name}.thinking_enabled`]}
              />
              <ThinkingEffortField
                fullKey={`llm.providers.${name}.thinking_effort`}
                value={current(
                  draft,
                  fields,
                  "thinking_effort",
                  `llm.providers.${name}.thinking_effort`,
                )}
                onChange={(value) => update({ thinking_effort: value })}
                overridden={isOverridden(
                  fields,
                  `llm.providers.${name}.thinking_effort`,
                )}
                sourceField={fields[`llm.providers.${name}.thinking_effort`]}
              />
              <RequestTimeoutField
                fullKey={`llm.providers.${name}.request_timeout_seconds`}
                value={current(
                  draft,
                  fields,
                  "request_timeout_seconds",
                  `llm.providers.${name}.request_timeout_seconds`,
                )}
                onChange={(value) => update({ request_timeout_seconds: value })}
                overridden={isOverridden(
                  fields,
                  `llm.providers.${name}.request_timeout_seconds`,
                )}
                sourceField={fields[`llm.providers.${name}.request_timeout_seconds`]}
                error={
                  errors[`llm.providers.${name}.request_timeout_seconds`]
                }
              />
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                {(["temperature", "top_p", "top_k", "repetition_penalty"] as const).map(
                  (samplingField) => {
                    const fullKey = `llm.providers.${name}.${samplingField}`;
                    return (
                      <SamplingField
                        key={samplingField}
                        samplingField={samplingField}
                        fullKey={fullKey}
                        value={current(draft, fields, samplingField, fullKey)}
                        onChange={(value) =>
                          update({ [samplingField]: value } as ProviderDraft)
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

function ApiKeyReadOnly() {
  return (
    <div className="grid grid-cols-1 gap-2 md:grid-cols-[200px_1fr] md:items-center">
      <div className="space-y-0.5">
        <Label>API key</Label>
        <div className="flex items-center gap-1 text-[10px] text-zinc-500 dark:text-zinc-400">
          <Lock className="h-3 w-3" aria-hidden /> yml-only
        </div>
      </div>
      <Input
        disabled
        type="password"
        value="••••••••"
        title="Managed in xauditor.yml only"
      />
    </div>
  );
}

function StringField({
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
        <FieldMeta
          overridden={overridden}
          ymlKey={fullKey}
          source={sourceField?.source}
        />
      </div>
      <Input
        id={fullKey}
        value={value}
        disabled={overridden}
        onChange={(event) => onChange(event.target.value)}
      />
    </div>
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
        <FieldMeta
          overridden={overridden}
          ymlKey={fullKey}
          source={sourceField?.source}
        />
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
      <FieldMeta
        overridden={overridden}
        ymlKey={fullKey}
        source={sourceField?.source}
      />
      <Input
        id={fullKey}
        type="number"
        step={samplingField === "top_k" ? "1" : "any"}
        value={value}
        disabled={overridden}
        placeholder="(vendor default)"
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

function FieldMeta({
  overridden,
  ymlKey,
  source,
}: {
  overridden: boolean;
  ymlKey: string;
  source?: string;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {overridden ? (
        <OverrideBadge
          ymlKey={ymlKey}
          source={(source as "yml" | "env" | undefined) ?? "yml"}
        />
      ) : null}
      {source ? (
        <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
          src: {source}
        </span>
      ) : null}
    </div>
  );
}

function KindField({
  fullKey,
  value,
  onChange,
  overridden,
  sourceField,
}: {
  fullKey: string;
  value: string;
  onChange: (value: string) => void;
  overridden: boolean;
  sourceField: ConfigField | undefined;
}) {
  const resolved = value === "" ? "openai" : value;
  return (
    <div
      className={cn(
        "grid grid-cols-1 gap-2 md:grid-cols-[200px_1fr] md:items-center",
        overridden && "opacity-70",
      )}
    >
      <div className="space-y-0.5">
        <Label htmlFor={fullKey}>Kind</Label>
        <FieldMeta
          overridden={overridden}
          ymlKey={fullKey}
          source={sourceField?.source}
        />
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Wire protocol: <code>openai</code> uses langchain-openai;{" "}
          <code>anthropic</code> uses langchain-anthropic for native effort &
          prompt-caching support.
        </p>
      </div>
      <Select
        id={fullKey}
        value={resolved}
        disabled={overridden}
        onChange={(event) => onChange(event.target.value)}
      >
        {PROVIDER_KINDS.map((k) => (
          <option key={k} value={k}>
            {k}
          </option>
        ))}
      </Select>
    </div>
  );
}

function ThinkingEffortField({
  fullKey,
  value,
  onChange,
  overridden,
  sourceField,
}: {
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
        <Label htmlFor={fullKey}>Thinking effort</Label>
        <FieldMeta
          overridden={overridden}
          ymlKey={fullKey}
          source={sourceField?.source}
        />
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Anthropic-only effort dial. Choose <code>(unset)</code> to omit.
        </p>
      </div>
      <Select
        id={fullKey}
        value={value}
        disabled={overridden}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">(unset)</option>
        {PROVIDER_THINKING_EFFORTS.map((effort) => (
          <option key={effort} value={effort}>
            {effort}
          </option>
        ))}
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
        <FieldMeta
          overridden={overridden}
          ymlKey={fullKey}
          source={sourceField?.source}
        />
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Per-call SDK timeout. Leave blank to use the SDK default (~600s).
        </p>
      </div>
      <div className="space-y-1">
        <Input
          id={fullKey}
          type="number"
          step="any"
          min={0}
          value={value}
          disabled={overridden}
          placeholder="(SDK default)"
          onChange={(event) => onChange(event.target.value)}
        />
        {error ? (
          <p className="text-[11px] text-rose-600 dark:text-rose-300">{error}</p>
        ) : null}
      </div>
    </div>
  );
}
