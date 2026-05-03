"use client";

import { Info, Save } from "lucide-react";
import * as React from "react";
import {
  ApiError,
  useEffectiveConfig,
  useEffectiveLLMConfig,
  useSaveSnapshot,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select, Textarea } from "@/components/ui/input";
import { OverrideBadge } from "@/components/override-badge";
import { AgentOverridesSection } from "@/components/agent-overrides-section";
import type { AgentDraft } from "@/components/agent-overrides-section";
import { AuditSection } from "@/components/audit-section";
import {
  CoderSection,
  isLocalEndpoint,
} from "@/components/coder-section";
import type { CoderDraft } from "@/components/coder-section";
import { EffectiveConfigPreview } from "@/components/effective-config-preview";
import {
  GraphBuildSection,
} from "@/components/graph-build-section";
import type { GraphBuildDraft } from "@/components/graph-build-section";
import { ModelSettingsSection } from "@/components/model-settings-section";
import type { ProviderDraft } from "@/components/model-settings-section";
import { RepositorySection } from "@/components/repository-section";
import { SettingsToc } from "@/components/settings-toc";
import { TeamingSection } from "@/components/teaming-section";
import type { TeamingDraft } from "@/components/teaming-section";
import {
  isReadOnly as configIsReadOnly,
  readOnlyCountForPrefix,
  sourceOf,
} from "@/lib/config-source";
import { cn } from "@/lib/utils";
import { validateSamplingValue } from "@/lib/validate-sampling";
import type { SamplingField } from "@/lib/validate-sampling";
import type { ConfigField, RedactedKey } from "@/lib/types";

type FieldKind = "string" | "boolean" | "number" | "enum";

interface FieldSpec {
  key: string;
  label: string;
  kind: FieldKind;
  options?: string[];
  description?: string;
}

const LOGGING_FIELDS: FieldSpec[] = [
  {
    key: "logging.level",
    label: "Logging level",
    kind: "enum",
    options: ["error", "warning", "info", "debug"],
    description: "Verbosity of xauditor's runtime log output.",
  },
  {
    key: "logging.file",
    label: "Logging file",
    kind: "string",
    description: "Optional path to mirror logs into a file.",
  },
];

const AGENTS = ["graph_builder", "auditor", "exploitation", "validator"] as const;
type Agent = (typeof AGENTS)[number];

function coerce(input: string, kind: FieldKind): unknown {
  if (kind === "boolean") return input === "true";
  if (kind === "number") {
    const n = Number(input);
    return Number.isFinite(n) ? n : input;
  }
  return input;
}

function setNested(
  target: Record<string, unknown>,
  key: string,
  value: unknown,
): void {
  const parts = key.split(".");
  let cursor = target;
  for (const segment of parts.slice(0, -1)) {
    if (typeof cursor[segment] !== "object" || cursor[segment] === null) {
      cursor[segment] = {};
    }
    cursor = cursor[segment] as Record<string, unknown>;
  }
  cursor[parts[parts.length - 1]] = value;
}

function parseSamplingNumber(value: string, field: SamplingField): number | null {
  if (value === "") return null;
  const parsed = field === "top_k" ? Number.parseInt(value, 10) : Number(value);
  if (!Number.isFinite(parsed)) return null;
  return parsed;
}

interface CollapsibleSectionProps {
  id: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}

function CollapsibleSection({
  id,
  defaultOpen = false,
  children,
}: CollapsibleSectionProps) {
  return (
    <details
      id={id}
      open={defaultOpen}
      className="group rounded-xl border border-zinc-200 bg-white open:shadow-sm dark:border-zinc-800 dark:bg-zinc-950/40"
    >
      <summary className="cursor-pointer list-none rounded-t-xl px-4 py-3 text-sm font-medium hover:bg-zinc-50 dark:hover:bg-zinc-900/60">
        <SectionSummaryContent>{children}</SectionSummaryContent>
      </summary>
      <div className="px-4 pb-4">
        <SectionBodyContent>{children}</SectionBodyContent>
      </div>
    </details>
  );
}

// The summary row renders the title + badge from the first child;
// the body renders everything else. This lets a single CollapsibleSection
// wrap any of the cards without needing them to know about <details>.
function SectionSummaryContent({ children }: { children: React.ReactNode }) {
  const [first] = React.Children.toArray(children);
  return <>{first}</>;
}

function SectionBodyContent({ children }: { children: React.ReactNode }) {
  const all = React.Children.toArray(children);
  return <>{all.slice(1)}</>;
}

interface SectionShellProps {
  id: string;
  title: string;
  badgeCount: number;
  description?: React.ReactNode;
  defaultOpen?: boolean;
  children: React.ReactNode;
}

function SectionShell({
  id,
  title,
  badgeCount,
  description,
  defaultOpen,
  children,
}: SectionShellProps) {
  return (
    <details
      id={id}
      open={defaultOpen}
      className="group rounded-xl border border-zinc-200 bg-white open:shadow-sm dark:border-zinc-800 dark:bg-zinc-950/40"
    >
      <summary className="flex cursor-pointer items-center justify-between gap-3 rounded-t-xl px-4 py-3 hover:bg-zinc-50 dark:hover:bg-zinc-900/60">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold tracking-tight">{title}</span>
          {badgeCount > 0 ? (
            <span
              className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-amber-200 px-1.5 text-[10px] font-semibold text-amber-900 dark:bg-amber-800 dark:text-amber-100"
              title={`${badgeCount} read-only-source key${
                badgeCount === 1 ? "" : "s"
              } in this section`}
            >
              {badgeCount}
            </span>
          ) : null}
        </div>
        <span className="text-[11px] uppercase tracking-wider text-zinc-400 group-open:hidden dark:text-zinc-500">
          expand
        </span>
        <span className="hidden text-[11px] uppercase tracking-wider text-zinc-400 group-open:inline dark:text-zinc-500">
          collapse
        </span>
      </summary>
      <div className="space-y-3 border-t border-zinc-100 px-4 py-3 dark:border-zinc-800/60">
        {description ? (
          <p className="text-xs text-zinc-500 dark:text-zinc-400">{description}</p>
        ) : null}
        {children}
      </div>
    </details>
  );
}

export default function SettingsPage() {
  const { data, isLoading, error, refetch } = useEffectiveConfig();
  const llm = useEffectiveLLMConfig();
  const saveSnapshot = useSaveSnapshot();

  const [draft, setDraft] = React.useState<Record<string, string>>({});
  const [providerDrafts, setProviderDrafts] = React.useState<
    Record<string, ProviderDraft>
  >({});
  const [agentDrafts, setAgentDrafts] = React.useState<Record<Agent, AgentDraft>>(
    () => ({
      graph_builder: {},
      auditor: {},
      exploitation: {},
      validator: {},
    }),
  );
  const [defaultProviderDraft, setDefaultProviderDraft] = React.useState<
    string | undefined
  >(undefined);
  const [repositoryDraft, setRepositoryDraft] = React.useState<
    string[] | undefined
  >(undefined);
  const [graphBuildDraft, setGraphBuildDraft] = React.useState<GraphBuildDraft>(
    {},
  );
  const [auditDraft, setAuditDraft] = React.useState<string | undefined>(
    undefined,
  );
  const [teamingDraft, setTeamingDraft] = React.useState<TeamingDraft>({});
  const [coderDraft, setCoderDraft] = React.useState<CoderDraft>({});
  const [note, setNote] = React.useState("");
  const [saveError, setSaveError] = React.useState<string | null>(null);
  const [lastSaved, setLastSaved] = React.useState<number | null>(null);
  const [samplingErrors, setSamplingErrors] = React.useState<
    Record<string, string>
  >({});

  const fields = data?.fields ?? {};
  const redactedKeys: Record<string, RedactedKey> = data?.redacted_keys ?? {};
  const providerNames = llm.data?.providers ?? [];

  function isReadOnly(key: string): boolean {
    return configIsReadOnly(fields, key);
  }

  function currentValue(spec: FieldSpec): string {
    const key = spec.key;
    if (Object.prototype.hasOwnProperty.call(draft, key)) {
      return draft[key];
    }
    const raw = fields[key]?.value;
    if (raw === undefined || raw === null) return "";
    return String(raw);
  }

  function updateField(spec: FieldSpec, value: string) {
    setDraft((prev) => ({ ...prev, [spec.key]: value }));
  }

  function rawList(key: string): string[] {
    const raw = fields[key]?.value;
    if (Array.isArray(raw)) return raw.map(String);
    return [];
  }

  const repositoryValue = repositoryDraft ?? rawList("repository.excludes");

  function teamingProviderList(team: "analyzer" | "validator" | "exploiter"): string[] {
    const draftValue = teamingDraft[team]?.provider_list;
    if (draftValue !== undefined) return draftValue;
    return rawList(`teaming.${team}.provider_list`);
  }

  function teamingEnabledEffective(): boolean {
    const draftEnabled = teamingDraft.enabled;
    if (draftEnabled !== undefined) return draftEnabled === "true";
    return Boolean(fields["teaming.enabled"]?.value);
  }

  function validateAllDrafts(): boolean {
    const errors: Record<string, string> = {};
    for (const [name, provider] of Object.entries(providerDrafts)) {
      for (const samplingField of [
        "temperature",
        "top_p",
        "top_k",
        "repetition_penalty",
      ] as const) {
        const raw = provider[samplingField];
        if (raw === undefined || raw === "") continue;
        const parsed = parseSamplingNumber(raw, samplingField);
        if (parsed === null) {
          errors[`llm.providers.${name}.${samplingField}`] =
            `${samplingField} must be a number`;
          continue;
        }
        const message = validateSamplingValue(samplingField, parsed);
        if (message) {
          errors[`llm.providers.${name}.${samplingField}`] = message;
        }
      }
      const reqRaw = provider.request_timeout_seconds;
      if (reqRaw !== undefined && reqRaw !== "") {
        const parsed = Number(reqRaw);
        if (!Number.isFinite(parsed) || parsed <= 0) {
          errors[`llm.providers.${name}.request_timeout_seconds`] =
            "request_timeout_seconds must be > 0";
        }
      }
    }
    for (const agent of AGENTS) {
      const agentDraft = agentDrafts[agent];
      if (!agentDraft) continue;
      for (const samplingField of [
        "temperature",
        "top_p",
        "top_k",
        "repetition_penalty",
      ] as const) {
        const raw = agentDraft[samplingField];
        if (raw === undefined || raw === "") continue;
        const parsed = parseSamplingNumber(raw, samplingField);
        if (parsed === null) {
          errors[`agents.${agent}.llm.${samplingField}`] =
            `${samplingField} must be a number`;
          continue;
        }
        const message = validateSamplingValue(samplingField, parsed);
        if (message) {
          errors[`agents.${agent}.llm.${samplingField}`] = message;
        }
      }
      const reqRaw = agentDraft.request_timeout_seconds;
      if (reqRaw !== undefined && reqRaw !== "") {
        const parsed = Number(reqRaw);
        if (!Number.isFinite(parsed) || parsed <= 0) {
          errors[`agents.${agent}.llm.request_timeout_seconds`] =
            "request_timeout_seconds must be > 0";
        }
      }
    }
    if (teamingEnabledEffective()) {
      for (const team of ["analyzer", "validator", "exploiter"] as const) {
        if (teamingProviderList(team).length === 0) {
          errors[`teaming.${team}.provider_list`] =
            "Required when teaming.enabled is true";
        }
      }
    }
    setSamplingErrors(errors);
    return Object.keys(errors).length === 0;
  }

  async function submit() {
    setSaveError(null);
    if (!validateAllDrafts()) {
      setSaveError("Fix validation errors before saving.");
      return;
    }
    const body: Record<string, unknown> = {};

    // Logging fields (uses the legacy SECTIONS-style array).
    for (const spec of LOGGING_FIELDS) {
      if (isReadOnly(spec.key)) continue;
      const raw = Object.prototype.hasOwnProperty.call(draft, spec.key)
        ? draft[spec.key]
        : String(fields[spec.key]?.value ?? "");
      if (raw === "" && spec.kind === "string") continue;
      setNested(body, spec.key, coerce(raw, spec.kind));
    }

    // Repository excludes (chip-input draft).
    if (
      repositoryDraft !== undefined &&
      !isReadOnly("repository.excludes")
    ) {
      setNested(body, "repository.excludes", repositoryDraft);
    }

    // Graph build extended fields.
    const graphBuildEntries: Array<[keyof GraphBuildDraft, string, FieldKind]> = [
      ["enable_llm_enrichment", "graph.build.enable_llm_enrichment", "boolean"],
      ["max_file_bytes", "graph.build.max_file_bytes", "number"],
      ["paths_max_depth", "graph.build.paths_max_depth", "number"],
      ["paths_max_count", "graph.build.paths_max_count", "number"],
      ["neo4j_chunk_size", "graph.build.neo4j_chunk_size", "number"],
    ];
    for (const [drKey, fullKey, kind] of graphBuildEntries) {
      if (isReadOnly(fullKey)) continue;
      const raw = graphBuildDraft[drKey];
      if (raw === undefined || raw === "") continue;
      setNested(body, fullKey, coerce(raw, kind));
    }

    // Audit.
    if (auditDraft !== undefined && !isReadOnly("audit.worker_count")) {
      const parsed = Number(auditDraft);
      if (Number.isFinite(parsed)) {
        setNested(body, "audit.worker_count", parsed);
      }
    }

    // Teaming.
    if (
      teamingDraft.enabled !== undefined &&
      !isReadOnly("teaming.enabled")
    ) {
      setNested(body, "teaming.enabled", teamingDraft.enabled === "true");
    }
    for (const team of ["analyzer", "validator", "exploiter"] as const) {
      const teamDraft = teamingDraft[team];
      if (!teamDraft) continue;
      if (
        teamDraft.subagent_count !== undefined &&
        teamDraft.subagent_count !== "" &&
        !isReadOnly(`teaming.${team}.subagent_count`)
      ) {
        const n = Number(teamDraft.subagent_count);
        if (Number.isFinite(n)) {
          setNested(body, `teaming.${team}.subagent_count`, n);
        }
      }
      if (
        teamDraft.provider_list !== undefined &&
        !isReadOnly(`teaming.${team}.provider_list`)
      ) {
        setNested(body, `teaming.${team}.provider_list`, teamDraft.provider_list);
      }
      if (
        team === "validator" &&
        teamDraft.debate_rounds !== undefined &&
        teamDraft.debate_rounds !== "" &&
        !isReadOnly("teaming.validator.debate_rounds")
      ) {
        const n = Number(teamDraft.debate_rounds);
        if (Number.isFinite(n)) {
          setNested(body, "teaming.validator.debate_rounds", n);
        }
      }
    }

    // Coder section.
    const coderStringEntries: Array<keyof CoderDraft> = [
      "transport",
      "model_url",
      "model_name",
      "working_directory",
      "endpoint",
      "container_image",
      "container_name",
      "runtime_socket_path",
      "workspace_root",
      "project_name",
    ];
    const coderNumberEntries: Array<keyof CoderDraft> = [
      "concurrency",
      "request_timeout_seconds",
      "poll_interval_seconds",
      "preflight_timeout_seconds",
    ];
    if (
      coderDraft.enabled !== undefined &&
      !isReadOnly("coder.enabled")
    ) {
      setNested(body, "coder.enabled", coderDraft.enabled === "true");
    }
    if (
      coderDraft.enable_auth !== undefined &&
      !isReadOnly("coder.enable_auth")
    ) {
      setNested(body, "coder.enable_auth", coderDraft.enable_auth === "true");
    }
    if (
      coderDraft.thinking_effort !== undefined &&
      !isReadOnly("coder.thinking_effort")
    ) {
      // Empty string in the select == "(unset)" — submit as omitted.
      if (coderDraft.thinking_effort !== "") {
        setNested(body, "coder.thinking_effort", coderDraft.thinking_effort);
      }
    }
    if (
      coderDraft.cli_command !== undefined &&
      !isReadOnly("coder.cli_command")
    ) {
      setNested(body, "coder.cli_command", coderDraft.cli_command);
    }
    for (const key of coderStringEntries) {
      const raw = coderDraft[key];
      if (typeof raw !== "string") continue;
      const fullKey = `coder.${key}`;
      if (isReadOnly(fullKey)) continue;
      if (raw === "") continue;
      setNested(body, fullKey, raw);
    }
    for (const key of coderNumberEntries) {
      const raw = coderDraft[key];
      if (typeof raw !== "string") continue;
      const fullKey = `coder.${key}`;
      if (isReadOnly(fullKey)) continue;
      if (raw === "") continue;
      const parsed = Number(raw);
      if (Number.isFinite(parsed)) setNested(body, fullKey, parsed);
    }

    // LLM default provider.
    if (
      defaultProviderDraft !== undefined &&
      !isReadOnly("llm.default_provider") &&
      defaultProviderDraft !== ""
    ) {
      setNested(body, "llm.default_provider", defaultProviderDraft);
    }

    // LLM provider cards.
    for (const [name, provider] of Object.entries(providerDrafts)) {
      for (const stringField of [
        "base_url",
        "model_name",
        "kind",
        "thinking_effort",
      ] as const) {
        const raw = provider[stringField];
        if (raw === undefined || raw === "") continue;
        const key = `llm.providers.${name}.${stringField}`;
        if (!isReadOnly(key)) setNested(body, key, raw);
      }
      if (provider.thinking_enabled !== undefined) {
        const key = `llm.providers.${name}.thinking_enabled`;
        if (!isReadOnly(key)) setNested(body, key, provider.thinking_enabled);
      }
      if (
        provider.request_timeout_seconds !== undefined &&
        provider.request_timeout_seconds !== ""
      ) {
        const parsed = Number(provider.request_timeout_seconds);
        const key = `llm.providers.${name}.request_timeout_seconds`;
        if (!isReadOnly(key) && Number.isFinite(parsed)) {
          setNested(body, key, parsed);
        }
      }
      for (const samplingField of [
        "temperature",
        "top_p",
        "top_k",
        "repetition_penalty",
      ] as const) {
        const raw = provider[samplingField];
        if (raw === undefined || raw === "") continue;
        const parsed = parseSamplingNumber(raw, samplingField);
        if (parsed === null) continue;
        const key = `llm.providers.${name}.${samplingField}`;
        if (!isReadOnly(key)) setNested(body, key, parsed);
      }
    }

    // Per-agent overrides.
    for (const agent of AGENTS) {
      const agentDraft = agentDrafts[agent];
      if (!agentDraft) continue;
      const providerKey = `agents.${agent}.llm.provider`;
      if (
        agentDraft.provider !== undefined &&
        agentDraft.provider !== "" &&
        !isReadOnly(providerKey)
      ) {
        setNested(body, providerKey, agentDraft.provider);
      }
      if (agentDraft.thinking_enabled !== undefined) {
        const key = `agents.${agent}.llm.thinking_enabled`;
        if (!isReadOnly(key)) setNested(body, key, agentDraft.thinking_enabled);
      }
      if (
        agentDraft.request_timeout_seconds !== undefined &&
        agentDraft.request_timeout_seconds !== ""
      ) {
        const parsed = Number(agentDraft.request_timeout_seconds);
        const key = `agents.${agent}.llm.request_timeout_seconds`;
        if (!isReadOnly(key) && Number.isFinite(parsed)) {
          setNested(body, key, parsed);
        }
      }
      for (const samplingField of [
        "temperature",
        "top_p",
        "top_k",
        "repetition_penalty",
      ] as const) {
        const raw = agentDraft[samplingField];
        if (raw === undefined || raw === "") continue;
        const parsed = parseSamplingNumber(raw, samplingField);
        if (parsed === null) continue;
        const key = `agents.${agent}.llm.${samplingField}`;
        if (!isReadOnly(key)) setNested(body, key, parsed);
      }
    }

    try {
      await saveSnapshot.mutateAsync({ body, note: note || undefined });
      setDraft({});
      setProviderDrafts({});
      setAgentDrafts({
        graph_builder: {},
        auditor: {},
        exploitation: {},
        validator: {},
      });
      setDefaultProviderDraft(undefined);
      setRepositoryDraft(undefined);
      setGraphBuildDraft({});
      setAuditDraft(undefined);
      setTeamingDraft({});
      setCoderDraft({});
      setNote("");
      setSamplingErrors({});
      setLastSaved(Date.now());
      refetch();
      llm.refetch();
    } catch (err) {
      setSaveError(
        err instanceof ApiError
          ? typeof err.message === "string"
            ? err.message
            : "Save failed."
          : "Save failed.",
      );
    }
  }

  if (isLoading) {
    return (
      <p className="text-sm text-zinc-500 dark:text-zinc-400">
        Loading effective configuration…
      </p>
    );
  }
  if (error || !data) {
    return (
      <p
        role="alert"
        className="rounded-md bg-rose-50 px-3 py-2 text-sm text-rose-700 dark:bg-rose-950 dark:text-rose-200"
      >
        Failed to load configuration.
      </p>
    );
  }

  // Banner: keys whose source is yml or env (the read-only sources).
  const readOnlyKeys = Object.entries(fields)
    .filter(([, field]) => field.source === "yml" || field.source === "env")
    .map(([key]) => key)
    .sort();

  const sectionDefs: Array<{
    id: string;
    label: string;
    prefix: string;
    defaultOpen?: boolean;
  }> = [
    { id: "section-logging", label: "Logging", prefix: "logging.", defaultOpen: true },
    { id: "section-repository", label: "Repository", prefix: "repository." },
    { id: "section-graph-build", label: "Graph build", prefix: "graph.build." },
    { id: "section-audit", label: "Audit", prefix: "audit." },
    { id: "section-teaming", label: "Teaming", prefix: "teaming." },
    { id: "section-coder", label: "Coder", prefix: "coder." },
    {
      id: "section-llm-providers",
      label: "LLM providers",
      prefix: "llm.providers.",
      defaultOpen: true,
    },
    {
      id: "section-per-agent",
      label: "Default provider & per-agent",
      prefix: "agents.",
      defaultOpen: true,
    },
    { id: "section-effective-preview", label: "Effective preview", prefix: "__none__", defaultOpen: true },
    { id: "section-save", label: "Save snapshot", prefix: "__none__", defaultOpen: true },
  ];

  const tocEntries = sectionDefs.map((s) => ({
    id: s.id,
    label: s.label,
    badgeCount: readOnlyCountForPrefix(fields, s.prefix),
  }));

  return (
    <div className="space-y-5">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-zinc-500 dark:text-zinc-400">
          Edit the UI-configurable subset of xauditor. Fields sourced from{" "}
          <span className="font-mono">xauditor.yml</span> or an{" "}
          <span className="font-mono">XAUDITOR_*</span> environment variable
          are read-only here — remove them from yml / unset the env var to
          take ownership from the UI.
        </p>
      </header>

      {readOnlyKeys.length > 0 ? (
        <div className="rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-200">
          <div className="mb-2 flex items-center gap-2 font-medium">
            <Info className="h-4 w-4" aria-hidden />
            {readOnlyKeys.length} configuration{" "}
            {readOnlyKeys.length === 1 ? "field is" : "fields are"} sourced
            from yml or env (read-only)
          </div>
          <ul className="ml-6 list-disc font-mono text-xs">
            {readOnlyKeys.map((key) => (
              <li key={key}>
                {key}{" "}
                <span className="text-amber-700 dark:text-amber-300">
                  ({sourceOf(fields, key)})
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="rounded-xl border border-zinc-200 bg-white px-4 py-3 text-xs text-zinc-500 dark:border-zinc-800 dark:bg-zinc-900/60 dark:text-zinc-400">
        <strong className="text-zinc-800 dark:text-zinc-200">About.</strong>{" "}
        Remote database connection info (
        <span className="font-mono">reportdb.remote.*</span>,{" "}
        <span className="font-mono">graph.db.remote.*</span>) and every
        secret-shaped key (<span className="font-mono">*.api_key</span>,
        <span className="font-mono">*.password</span>,
        <span className="font-mono">*.model_api_key</span>,
        <span className="font-mono">*.endpoint_token</span>) are managed
        in <span className="font-mono">xauditor.yml</span> only — the
        portal redacts these at the API boundary.
      </div>

      <SettingsToc entries={tocEntries} />

      {/* Logging — uses the legacy SECTIONS-style render. */}
      <SectionShell
        id="section-logging"
        title="Logging"
        badgeCount={readOnlyCountForPrefix(fields, "logging.")}
        defaultOpen
      >
        <div className="space-y-3">
          {LOGGING_FIELDS.map((spec) => {
            const readOnly = isReadOnly(spec.key);
            const sourceField = fields[spec.key];
            const value = currentValue(spec);
            return (
              <div
                key={spec.key}
                className={cn(
                  "grid grid-cols-1 gap-2 md:grid-cols-[240px_1fr_auto] md:items-center",
                  readOnly && "opacity-70",
                )}
              >
                <div className="space-y-0.5">
                  <Label htmlFor={spec.key}>{spec.label}</Label>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {readOnly ? (
                      <OverrideBadge
                        ymlKey={spec.key}
                        source={sourceField?.source}
                      />
                    ) : null}
                    {sourceField ? (
                      <span className="font-mono text-[10px] text-zinc-500 dark:text-zinc-400">
                        src: {sourceField.source}
                      </span>
                    ) : null}
                  </div>
                  {spec.description ? (
                    <p className="text-xs text-zinc-500 dark:text-zinc-400">
                      {spec.description}
                    </p>
                  ) : null}
                </div>
                <div>
                  {spec.kind === "boolean" ? (
                    <Select
                      id={spec.key}
                      disabled={readOnly}
                      value={value === "" ? "false" : value}
                      onChange={(event) => updateField(spec, event.target.value)}
                    >
                      <option value="true">true</option>
                      <option value="false">false</option>
                    </Select>
                  ) : spec.kind === "enum" ? (
                    <Select
                      id={spec.key}
                      disabled={readOnly}
                      value={value}
                      onChange={(event) => updateField(spec, event.target.value)}
                    >
                      {(spec.options ?? []).map((option) => (
                        <option key={option} value={option}>
                          {option}
                        </option>
                      ))}
                    </Select>
                  ) : (
                    <Input
                      id={spec.key}
                      type={spec.kind === "number" ? "number" : "text"}
                      disabled={readOnly}
                      value={value}
                      onChange={(event) => updateField(spec, event.target.value)}
                    />
                  )}
                </div>
                <div />
              </div>
            );
          })}
        </div>
      </SectionShell>

      <SectionShell
        id="section-repository"
        title="Repository"
        badgeCount={readOnlyCountForPrefix(fields, "repository.")}
      >
        <RepositorySection
          fields={fields}
          value={repositoryValue}
          onChange={(next) => setRepositoryDraft(next)}
        />
      </SectionShell>

      <SectionShell
        id="section-graph-build"
        title="Graph build"
        badgeCount={readOnlyCountForPrefix(fields, "graph.build.")}
      >
        <GraphBuildSection
          fields={fields}
          draft={graphBuildDraft}
          onChange={(patch) =>
            setGraphBuildDraft((prev) => ({ ...prev, ...patch }))
          }
        />
      </SectionShell>

      <SectionShell
        id="section-audit"
        title="Audit"
        badgeCount={readOnlyCountForPrefix(fields, "audit.")}
      >
        <AuditSection
          fields={fields}
          value={auditDraft ?? String(fields["audit.worker_count"]?.value ?? "")}
          onChange={(next) => setAuditDraft(next)}
        />
      </SectionShell>

      <SectionShell
        id="section-teaming"
        title="Teaming"
        badgeCount={readOnlyCountForPrefix(fields, "teaming.")}
      >
        <TeamingSection
          fields={fields}
          providerNames={providerNames}
          draft={teamingDraft}
          onChange={(patch) => setTeamingDraft((prev) => ({ ...prev, ...patch }))}
        />
      </SectionShell>

      <SectionShell
        id="section-coder"
        title="Coder"
        badgeCount={readOnlyCountForPrefix(fields, "coder.")}
      >
        <CoderSection
          fields={fields}
          redactedKeys={redactedKeys}
          draft={coderDraft}
          onChange={(patch) => setCoderDraft((prev) => ({ ...prev, ...patch }))}
        />
      </SectionShell>

      <SectionShell
        id="section-llm-providers"
        title="LLM providers"
        badgeCount={readOnlyCountForPrefix(fields, "llm.providers.")}
        defaultOpen
      >
        <ModelSettingsSection
          providerNames={providerNames}
          fields={fields}
          drafts={providerDrafts}
          errors={samplingErrors}
          onDraftChange={(name, next) =>
            setProviderDrafts((prev) => ({ ...prev, [name]: next }))
          }
        />
      </SectionShell>

      <SectionShell
        id="section-per-agent"
        title="Default provider & per-agent overrides"
        badgeCount={
          readOnlyCountForPrefix(fields, "agents.") +
          readOnlyCountForPrefix(fields, "llm.default_provider")
        }
        defaultOpen
      >
        <AgentOverridesSection
          providerNames={providerNames}
          defaultProvider={llm.data?.default_provider ?? null}
          defaultProviderDraft={defaultProviderDraft}
          onDefaultProviderChange={setDefaultProviderDraft}
          fields={fields}
          drafts={agentDrafts}
          errors={samplingErrors}
          onDraftChange={(agent, next) =>
            setAgentDrafts((prev) => ({ ...prev, [agent]: next }))
          }
        />
      </SectionShell>

      <SectionShell
        id="section-effective-preview"
        title="Effective configuration preview"
        badgeCount={0}
        defaultOpen
      >
        <EffectiveConfigPreview />
      </SectionShell>

      <SectionShell
        id="section-save"
        title="Save snapshot"
        badgeCount={0}
        defaultOpen
      >
        <Card>
          <CardHeader>
            <CardTitle>Save snapshot</CardTitle>
            <p className="text-xs text-zinc-500 dark:text-zinc-400">
              Writes a new row in{" "}
              <span className="font-mono">config.config_snapshots</span>.
              Read-only fields (yml / env source) are excluded — only
              UI-owned keys go to the database.
            </p>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="snapshot-note">Note (optional)</Label>
              <Textarea
                id="snapshot-note"
                rows={2}
                value={note}
                onChange={(event) => setNote(event.target.value)}
                placeholder="e.g. lowered debate rounds for the nightly pipeline"
              />
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <Button onClick={submit} disabled={saveSnapshot.isPending}>
                <Save className="h-4 w-4" aria-hidden />
                {saveSnapshot.isPending ? "Saving…" : "Save snapshot"}
              </Button>
              {lastSaved ? (
                <span className="text-xs text-emerald-600 dark:text-emerald-400">
                  Saved.
                </span>
              ) : null}
            </div>
            {saveError ? (
              <p
                role="alert"
                className="rounded-md bg-rose-50 px-3 py-2 text-xs text-rose-700 dark:bg-rose-950 dark:text-rose-200"
              >
                {saveError}
              </p>
            ) : null}
            {Object.keys(samplingErrors).length > 0 ? (
              <ul
                role="alert"
                className="list-disc rounded-md bg-rose-50 px-5 py-2 text-xs text-rose-700 dark:bg-rose-950 dark:text-rose-200"
              >
                {Object.entries(samplingErrors).map(([key, message]) => (
                  <li key={key}>
                    <span className="font-mono">{key}</span>: {message}
                  </li>
                ))}
              </ul>
            ) : null}
          </CardContent>
        </Card>
      </SectionShell>
    </div>
  );
}
