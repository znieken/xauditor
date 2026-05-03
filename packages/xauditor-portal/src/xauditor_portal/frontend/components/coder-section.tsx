"use client";

import * as React from "react";
import { Lock } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { ChipInput } from "@/components/chip-input";
import { OverrideBadge } from "@/components/override-badge";
import { NumericField } from "@/components/numeric-field";
import { isReadOnly, sourceOf } from "@/lib/config-source";
import { cn } from "@/lib/utils";
import type { ConfigField, RedactedKey } from "@/lib/types";

export interface CoderDraft {
  enabled?: string;
  transport?: string;
  cli_command?: string[];
  concurrency?: string;
  thinking_effort?: string;
  model_url?: string;
  model_name?: string;
  request_timeout_seconds?: string;
  working_directory?: string;
  endpoint?: string;
  enable_auth?: string;
  poll_interval_seconds?: string;
  preflight_timeout_seconds?: string;
  container_image?: string;
  container_name?: string;
  runtime_socket_path?: string;
  workspace_root?: string;
  project_name?: string;
}

interface Props {
  fields: Record<string, ConfigField | undefined>;
  redactedKeys: Record<string, RedactedKey>;
  draft: CoderDraft;
  onChange: (patch: CoderDraft) => void;
}

const HTTP_ONLY_KEYS = [
  "coder.endpoint",
  "coder.enable_auth",
  "coder.poll_interval_seconds",
  "coder.preflight_timeout_seconds",
] as const;

const CONTAINER_ONLY_KEYS = [
  "coder.container_image",
  "coder.container_name",
  "coder.runtime_socket_path",
  "coder.workspace_root",
  "coder.project_name",
] as const;

export function isLocalEndpoint(endpoint: string): boolean {
  const trimmed = endpoint.trim();
  if (trimmed === "") return true;
  if (trimmed.startsWith("unix://")) return true;
  try {
    const url = new URL(trimmed);
    const host = url.hostname.toLowerCase();
    // Some URL parsers preserve the IPv6 brackets in hostname; accept both.
    return (
      host === "localhost" ||
      host === "127.0.0.1" ||
      host === "::1" ||
      host === "[::1]"
    );
  } catch {
    return false;
  }
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
  if (typeof raw === "string" && raw.length > 0) return [raw];
  return [];
}

function StringField({
  fullKey,
  label,
  description,
  fields,
  value,
  onChange,
  disabled,
  disabledHint,
}: {
  fullKey: string;
  label: string;
  description?: string;
  fields: Record<string, ConfigField | undefined>;
  value: string;
  onChange: (next: string) => void;
  disabled: boolean;
  disabledHint?: string;
}) {
  const sourceField = fields[fullKey];
  const source = sourceOf(fields, fullKey);
  return (
    <div
      className={cn(
        "grid grid-cols-1 gap-2 md:grid-cols-[240px_1fr_auto] md:items-center",
        disabled && "opacity-70",
      )}
    >
      <div className="space-y-0.5">
        <Label htmlFor={fullKey}>{label}</Label>
        <div className="flex flex-wrap items-center gap-1.5">
          {isReadOnly(fields, fullKey) ? (
            <OverrideBadge ymlKey={fullKey} source={source} />
          ) : null}
          {sourceField ? (
            <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
              src: {sourceField.source}
            </span>
          ) : null}
        </div>
        {description ? (
          <p className="text-xs text-zinc-500 dark:text-zinc-400">{description}</p>
        ) : null}
        {disabled && disabledHint ? (
          <p className="text-[11px] text-zinc-400 dark:text-zinc-500">
            {disabledHint}
          </p>
        ) : null}
      </div>
      <Input
        id={fullKey}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      />
      <div />
    </div>
  );
}

function BooleanField({
  fullKey,
  label,
  fields,
  value,
  onChange,
  disabled,
  disabledHint,
}: {
  fullKey: string;
  label: string;
  fields: Record<string, ConfigField | undefined>;
  value: string;
  onChange: (next: string) => void;
  disabled: boolean;
  disabledHint?: string;
}) {
  const sourceField = fields[fullKey];
  const source = sourceOf(fields, fullKey);
  return (
    <div
      className={cn(
        "grid grid-cols-1 gap-2 md:grid-cols-[240px_1fr_auto] md:items-center",
        disabled && "opacity-70",
      )}
    >
      <div className="space-y-0.5">
        <Label htmlFor={fullKey}>{label}</Label>
        <div className="flex flex-wrap items-center gap-1.5">
          {isReadOnly(fields, fullKey) ? (
            <OverrideBadge ymlKey={fullKey} source={source} />
          ) : null}
          {sourceField ? (
            <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
              src: {sourceField.source}
            </span>
          ) : null}
        </div>
        {disabled && disabledHint ? (
          <p className="text-[11px] text-zinc-400 dark:text-zinc-500">
            {disabledHint}
          </p>
        ) : null}
      </div>
      <Select
        id={fullKey}
        value={value === "" ? "false" : value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="true">true</option>
        <option value="false">false</option>
      </Select>
      <div />
    </div>
  );
}

function SecretStub({
  fullKey,
  label,
  redactedKeys,
}: {
  fullKey: string;
  label: string;
  redactedKeys: Record<string, RedactedKey>;
}) {
  const entry = redactedKeys[fullKey];
  const present = entry?.present ?? false;
  const source = entry?.source ?? "default";
  return (
    <div className="grid grid-cols-1 gap-2 md:grid-cols-[240px_1fr_auto] md:items-center">
      <div className="space-y-0.5">
        <Label>{label}</Label>
        <div className="flex flex-wrap items-center gap-1 text-[10px] text-zinc-500 dark:text-zinc-400">
          <Lock className="h-3 w-3" aria-hidden /> yml-only
          <span className="font-mono">src: {source}</span>
        </div>
      </div>
      <Input
        disabled
        type="password"
        value={present ? "••••••••" : ""}
        placeholder={present ? undefined : "(not configured)"}
        title="Managed in xauditor.yml only"
      />
      <div className="text-[11px] text-zinc-500 dark:text-zinc-400">
        {present ? "configured" : "not configured"}
      </div>
    </div>
  );
}

const THINKING_EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const;
const TRANSPORTS = ["subprocess", "http"] as const;

export function CoderSection({ fields, redactedKeys, draft, onChange }: Props) {
  const transportRaw =
    draft.transport ?? rawValue(fields, "coder.transport") ?? "subprocess";
  const transport = transportRaw === "http" ? "http" : "subprocess";
  const endpointRaw = draft.endpoint ?? rawValue(fields, "coder.endpoint");
  const endpointIsLocal = isLocalEndpoint(endpointRaw);
  const httpDisabled = transport === "subprocess";
  const containerDisabled =
    transport === "subprocess" || (transport === "http" && !endpointIsLocal);

  function num(key: keyof CoderDraft, fullKey: string): string {
    return (draft[key] as string | undefined) ?? rawValue(fields, fullKey);
  }
  function str(key: keyof CoderDraft, fullKey: string): string {
    return (draft[key] as string | undefined) ?? rawValue(fields, fullKey);
  }

  const cliCommand =
    draft.cli_command ?? rawList(fields, "coder.cli_command");

  const thinkingEffort =
    draft.thinking_effort ?? rawValue(fields, "coder.thinking_effort") ?? "";
  const enabled = draft.enabled ?? rawValue(fields, "coder.enabled") ?? "false";
  const enableAuth =
    draft.enable_auth ?? rawValue(fields, "coder.enable_auth") ?? "false";

  return (
    <Card>
      <CardHeader>
        <CardTitle>Coder verification</CardTitle>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Per-finding verification stage. Toggle <code>enabled</code> off to
          skip the stage entirely. HTTP and container fields are disabled when
          they don't apply to the current transport / endpoint.
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        <BooleanField
          fullKey="coder.enabled"
          label="Enabled"
          fields={fields}
          value={enabled}
          onChange={(next) => onChange({ enabled: next })}
          disabled={isReadOnly(fields, "coder.enabled")}
        />
        <div
          className={cn(
            "grid grid-cols-1 gap-2 md:grid-cols-[240px_1fr_auto] md:items-center",
            isReadOnly(fields, "coder.transport") && "opacity-70",
          )}
        >
          <div className="space-y-0.5">
            <Label htmlFor="coder.transport">Transport</Label>
            <div className="flex flex-wrap items-center gap-1.5">
              {isReadOnly(fields, "coder.transport") ? (
                <OverrideBadge
                  ymlKey="coder.transport"
                  source={sourceOf(fields, "coder.transport")}
                />
              ) : null}
              {fields["coder.transport"] ? (
                <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
                  src: {fields["coder.transport"]?.source}
                </span>
              ) : null}
            </div>
          </div>
          <Select
            id="coder.transport"
            value={transport}
            disabled={isReadOnly(fields, "coder.transport")}
            onChange={(event) => onChange({ transport: event.target.value })}
          >
            {TRANSPORTS.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </Select>
          <div />
        </div>
        <div
          className={cn(
            "grid grid-cols-1 gap-2 md:grid-cols-[240px_1fr] md:items-start",
            isReadOnly(fields, "coder.cli_command") && "opacity-70",
          )}
        >
          <div className="space-y-0.5">
            <Label htmlFor="coder.cli_command">CLI command</Label>
            <div className="flex flex-wrap items-center gap-1.5">
              {isReadOnly(fields, "coder.cli_command") ? (
                <OverrideBadge
                  ymlKey="coder.cli_command"
                  source={sourceOf(fields, "coder.cli_command")}
                />
              ) : null}
              {fields["coder.cli_command"] ? (
                <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
                  src: {fields["coder.cli_command"]?.source}
                </span>
              ) : null}
            </div>
            <p className="text-xs text-zinc-500 dark:text-zinc-400">
              Argv used to spawn the verification CLI. Defaults to{" "}
              <code>claude</code>.
            </p>
          </div>
          <ChipInput
            id="coder.cli_command"
            value={cliCommand}
            onChange={(next) => onChange({ cli_command: next })}
            disabled={isReadOnly(fields, "coder.cli_command")}
            placeholder="e.g. claude, --some-flag"
          />
        </div>
        <NumericField
          fullKey="coder.concurrency"
          label="Concurrency"
          fields={fields}
          value={num("concurrency", "coder.concurrency")}
          onChange={(next) => onChange({ concurrency: next })}
          min={1}
          max={64}
          step="1"
          hint="Allowed range: [1, 64]."
        />
        <div
          className={cn(
            "grid grid-cols-1 gap-2 md:grid-cols-[240px_1fr_auto] md:items-center",
            isReadOnly(fields, "coder.thinking_effort") && "opacity-70",
          )}
        >
          <div className="space-y-0.5">
            <Label htmlFor="coder.thinking_effort">Thinking effort</Label>
            <div className="flex flex-wrap items-center gap-1.5">
              {isReadOnly(fields, "coder.thinking_effort") ? (
                <OverrideBadge
                  ymlKey="coder.thinking_effort"
                  source={sourceOf(fields, "coder.thinking_effort")}
                />
              ) : null}
              {fields["coder.thinking_effort"] ? (
                <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
                  src: {fields["coder.thinking_effort"]?.source}
                </span>
              ) : null}
            </div>
            <p className="text-xs text-zinc-500 dark:text-zinc-400">
              Discrete extended-thinking dial passed to the CLI. Choose{" "}
              <code>(unset)</code> to omit the kwarg entirely.
            </p>
          </div>
          <Select
            id="coder.thinking_effort"
            value={thinkingEffort}
            disabled={isReadOnly(fields, "coder.thinking_effort")}
            onChange={(event) =>
              onChange({ thinking_effort: event.target.value })
            }
          >
            <option value="">(unset)</option>
            {THINKING_EFFORTS.map((effort) => (
              <option key={effort} value={effort}>
                {effort}
              </option>
            ))}
          </Select>
          <div />
        </div>
        <StringField
          fullKey="coder.model_url"
          label="Model URL"
          fields={fields}
          value={str("model_url", "coder.model_url")}
          onChange={(next) => onChange({ model_url: next })}
          disabled={isReadOnly(fields, "coder.model_url")}
        />
        <StringField
          fullKey="coder.model_name"
          label="Model name"
          fields={fields}
          value={str("model_name", "coder.model_name")}
          onChange={(next) => onChange({ model_name: next })}
          disabled={isReadOnly(fields, "coder.model_name")}
        />
        <SecretStub
          fullKey="coder.model_api_key"
          label="Model API key"
          redactedKeys={redactedKeys}
        />
        <NumericField
          fullKey="coder.request_timeout_seconds"
          label="Request timeout (s)"
          fields={fields}
          value={num("request_timeout_seconds", "coder.request_timeout_seconds")}
          onChange={(next) => onChange({ request_timeout_seconds: next })}
          min={1}
          step="1"
        />
        <StringField
          fullKey="coder.working_directory"
          label="Working directory"
          fields={fields}
          value={str("working_directory", "coder.working_directory")}
          onChange={(next) => onChange({ working_directory: next })}
          disabled={isReadOnly(fields, "coder.working_directory")}
        />
        <StringField
          fullKey="coder.endpoint"
          label="Endpoint"
          fields={fields}
          value={str("endpoint", "coder.endpoint")}
          onChange={(next) => onChange({ endpoint: next })}
          disabled={
            httpDisabled || isReadOnly(fields, "coder.endpoint")
          }
          disabledHint={httpDisabled ? "HTTP transport only" : undefined}
        />
        <BooleanField
          fullKey="coder.enable_auth"
          label="Enable auth"
          fields={fields}
          value={enableAuth}
          onChange={(next) => onChange({ enable_auth: next })}
          disabled={
            httpDisabled || isReadOnly(fields, "coder.enable_auth")
          }
          disabledHint={httpDisabled ? "HTTP transport only" : undefined}
        />
        <SecretStub
          fullKey="coder.endpoint_token"
          label="Endpoint token"
          redactedKeys={redactedKeys}
        />
        <NumericField
          fullKey="coder.poll_interval_seconds"
          label="Poll interval (s)"
          fields={fields}
          value={num("poll_interval_seconds", "coder.poll_interval_seconds")}
          onChange={(next) => onChange({ poll_interval_seconds: next })}
          min={0}
          step="any"
          hint={
            httpDisabled ? "HTTP transport only — currently disabled" : undefined
          }
        />
        <NumericField
          fullKey="coder.preflight_timeout_seconds"
          label="Preflight timeout (s)"
          fields={fields}
          value={num(
            "preflight_timeout_seconds",
            "coder.preflight_timeout_seconds",
          )}
          onChange={(next) =>
            onChange({ preflight_timeout_seconds: next })
          }
          min={1}
          step="1"
          hint={
            httpDisabled ? "HTTP transport only — currently disabled" : undefined
          }
        />
        <StringField
          fullKey="coder.container_image"
          label="Container image"
          fields={fields}
          value={str("container_image", "coder.container_image")}
          onChange={(next) => onChange({ container_image: next })}
          disabled={
            containerDisabled ||
            isReadOnly(fields, "coder.container_image")
          }
          disabledHint={
            transport === "subprocess"
              ? "container-managed only"
              : "managed by deployment layer for remote endpoints"
          }
        />
        <StringField
          fullKey="coder.container_name"
          label="Container name"
          fields={fields}
          value={str("container_name", "coder.container_name")}
          onChange={(next) => onChange({ container_name: next })}
          disabled={
            containerDisabled ||
            isReadOnly(fields, "coder.container_name")
          }
          disabledHint={
            transport === "subprocess"
              ? "container-managed only"
              : "managed by deployment layer for remote endpoints"
          }
        />
        <StringField
          fullKey="coder.runtime_socket_path"
          label="Runtime socket path"
          fields={fields}
          value={str("runtime_socket_path", "coder.runtime_socket_path")}
          onChange={(next) => onChange({ runtime_socket_path: next })}
          disabled={
            containerDisabled ||
            isReadOnly(fields, "coder.runtime_socket_path")
          }
          disabledHint={
            transport === "subprocess"
              ? "container-managed only"
              : "managed by deployment layer for remote endpoints"
          }
        />
        <StringField
          fullKey="coder.workspace_root"
          label="Workspace root"
          fields={fields}
          value={str("workspace_root", "coder.workspace_root")}
          onChange={(next) => onChange({ workspace_root: next })}
          disabled={
            containerDisabled ||
            isReadOnly(fields, "coder.workspace_root")
          }
          disabledHint={
            transport === "subprocess"
              ? "container-managed only"
              : "managed by deployment layer for remote endpoints"
          }
        />
        <StringField
          fullKey="coder.project_name"
          label="Project name"
          fields={fields}
          value={str("project_name", "coder.project_name")}
          onChange={(next) => onChange({ project_name: next })}
          disabled={
            containerDisabled ||
            isReadOnly(fields, "coder.project_name")
          }
          disabledHint={
            transport === "subprocess"
              ? "container-managed only"
              : "managed by deployment layer for remote endpoints"
          }
        />
      </CardContent>
    </Card>
  );
}

export const CODER_SECTION_HTTP_ONLY_KEYS = HTTP_ONLY_KEYS;
export const CODER_SECTION_CONTAINER_ONLY_KEYS = CONTAINER_ONLY_KEYS;
