"use client";

import {
  QueryClient,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import type {
  BuildsPage,
  ConfigSnapshot,
  CoverageFileItem,
  CoverageFunctionItem,
  CoverageListParams,
  CoverageModuleItem,
  CoveragePage,
  CoverageSummary,
  EffectiveConfig,
  FeedbackLabel,
  FeedbackPayload,
  FindingDebate,
  FindingDetail,
  FindingFilters,
  FindingSummary,
  IncomingDuplicatesPage,
  LLMEffectiveConfig,
  Me,
  ProgressEvent,
  ProjectsPage,
  Role,
  RunDetail,
  RunStatus,
  RunsPage,
  UserListResponse,
  UserSummary,
} from "./types";
import { serializeFiltersToQueryString } from "./findings-filters";

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "include",
    headers: {
      Accept: "application/json",
      ...(init.body && !(init.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
      ...(init.headers ?? {}),
    },
  });
  if (response.status === 401) {
    // Let the caller distinguish between /auth/me (transient, probing) and
    // other routes (hard auth failure). Callers of authenticated fetches
    // catch ApiError and redirect.
    const text = await response.text();
    throw new ApiError(401, "Unauthorized", safeParse(text));
  }
  if (response.status === 204) {
    return undefined as T;
  }
  const text = await response.text();
  const body = safeParse(text);
  if (!response.ok) {
    const message =
      (body as { detail?: string } | undefined)?.detail ??
      (typeof body === "string" ? body : `Request failed (${response.status})`);
    throw new ApiError(response.status, message, body);
  }
  return body as T;
}

function safeParse(text: string): unknown {
  if (!text) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

export function toQueryString(params: Record<string, unknown>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      for (const v of value) {
        if (v === undefined || v === null || v === "") continue;
        search.append(key, String(v));
      }
    } else {
      search.set(key, String(value));
    }
  }
  const s = search.toString();
  return s ? `?${s}` : "";
}

export const api = {
  me: () => request<Me>("/api/auth/me"),
  login: (username: string, password: string) =>
    request<{ username: string; must_change_password: boolean }>(
      "/api/auth/login",
      {
        method: "POST",
        body: JSON.stringify({ username, password }),
      },
    ),
  logout: () => request<{ status: string }>("/api/auth/logout", { method: "POST" }),
  changePassword: (current_password: string, new_password: string) =>
    request<{ status: string }>("/api/auth/change-password", {
      method: "POST",
      body: JSON.stringify({ current_password, new_password }),
    }),
  listRuns: (params: {
    limit?: number;
    offset?: number;
    status?: RunStatus | string;
    mode?: "fast" | "deep";
    project?: string;
  } = {}) => request<RunsPage>(`/api/runs${toQueryString(params)}`),
  getRun: (runId: string, filters?: FindingFilters) => {
    // Use `serializeFiltersToQueryString` (NOT `toQueryString`) so the
    // run-detail endpoint receives the same three-state ``coder_status``
    // marker the findings-list endpoint understands:
    //   - filters.coder_status undefined → no param → server applies default
    //   - filters.coder_status === []    → ?coder_status=   → no restriction
    //   - filters.coder_status === [...] → repeated params → verbatim
    // This keeps the run-detail header tile in lockstep with the findings
    // list as the operator toggles chips.
    const qs = filters ? serializeFiltersToQueryString(filters) : "";
    return request<RunDetail>(
      qs ? `/api/runs/${runId}?${qs}` : `/api/runs/${runId}`,
    );
  },
  cancelRun: (runId: string, reason?: string | null) =>
    request<RunDetail>(`/api/runs/${runId}/cancel`, {
      method: "POST",
      body: JSON.stringify({ reason: reason ?? null }),
    }),
  completeRun: (runId: string, reason?: string | null) =>
    request<RunDetail>(`/api/runs/${runId}/complete`, {
      method: "POST",
      body: JSON.stringify({ reason: reason ?? null }),
    }),
  deleteRun: (runId: string) =>
    request<void>(`/api/runs/${runId}`, { method: "DELETE" }),
  runProgress: (runId: string, limit?: number) =>
    request<ProgressEvent[]>(
      `/api/runs/${runId}/progress${toQueryString({ limit })}`,
    ),
  runCoverageSummary: (runId: string) =>
    request<CoverageSummary>(`/api/runs/${runId}/coverage/summary`),
  runCoverageModules: (runId: string, params: CoverageListParams = {}) =>
    request<CoveragePage<CoverageModuleItem>>(
      `/api/runs/${runId}/coverage/modules${toQueryString(params as Record<string, unknown>)}`,
    ),
  runCoverageFiles: (runId: string, params: CoverageListParams = {}) =>
    request<CoveragePage<CoverageFileItem>>(
      `/api/runs/${runId}/coverage/files${toQueryString(params as Record<string, unknown>)}`,
    ),
  runCoverageFunctions: (runId: string, params: CoverageListParams = {}) =>
    request<CoveragePage<CoverageFunctionItem>>(
      `/api/runs/${runId}/coverage/functions${toQueryString(params as Record<string, unknown>)}`,
    ),
  runDebates: (runId: string) =>
    request<
      Array<{
        id: string;
        path_fingerprint: string;
        finding_ref: string;
        rounds: unknown[];
        final_verdict: string;
        convergence_state: string;
        configured_round_cap: number;
      }>
    >(`/api/runs/${runId}/debates`),
  runSubagents: (runId: string, stage?: "analyzer" | "validator" | "exploiter") =>
    request<Record<string, unknown[]>>(
      `/api/runs/${runId}/subagents${toQueryString({ stage })}`,
    ),
  listFindings: (runId: string, filters: FindingFilters = {}) =>
    request<FindingSummary[]>(
      `/api/runs/${runId}/findings${toQueryString(filters as Record<string, unknown>)}`,
    ),
  getFinding: (findingId: string) =>
    request<FindingDetail>(`/api/findings/${findingId}`),
  getFeedback: (findingId: string) =>
    request<FeedbackPayload | undefined>(
      `/api/findings/${findingId}/feedback`,
    ),
  createFeedback: (
    findingId: string,
    label: FeedbackLabel,
    note?: string,
    duplicateOfFindingId?: string | null,
  ) =>
    request<FeedbackPayload>(`/api/findings/${findingId}/feedback`, {
      method: "POST",
      body: JSON.stringify({
        label,
        researcher_note: note ?? null,
        duplicate_of_finding_id: duplicateOfFindingId ?? null,
      }),
    }),
  updateFeedback: (
    findingId: string,
    label: FeedbackLabel,
    note?: string,
    duplicateOfFindingId?: string | null,
  ) =>
    request<FeedbackPayload>(`/api/findings/${findingId}/feedback`, {
      method: "PATCH",
      body: JSON.stringify({
        label,
        researcher_note: note ?? null,
        duplicate_of_finding_id: duplicateOfFindingId ?? null,
      }),
    }),
  listIncomingDuplicates: (findingId: string) =>
    request<IncomingDuplicatesPage>(
      `/api/findings/${findingId}/duplicates`,
    ),
  effectiveConfig: () => request<EffectiveConfig>("/api/config/effective"),
  effectiveLLMConfig: () =>
    request<LLMEffectiveConfig>("/api/config/effective/llm"),
  saveConfigSnapshot: (body: Record<string, unknown>, note?: string) =>
    request<ConfigSnapshot>("/api/config/snapshot", {
      method: "PUT",
      body: JSON.stringify({ body, note: note ?? null }),
    }),
  listConfigSnapshots: () =>
    request<ConfigSnapshot[]>("/api/config/snapshots"),
  listProjects: (params: { limit?: number; offset?: number } = {}) =>
    request<ProjectsPage>(`/api/projects${toQueryString(params)}`),
  listBuilds: (
    projectKey: string,
    params: { limit?: number; offset?: number } = {},
  ) =>
    request<BuildsPage>(
      `/api/projects/${projectKey}/builds${toQueryString(params)}`,
    ),
  listBuildRuns: (
    projectKey: string,
    buildFingerprint: string,
    params: { limit?: number; offset?: number } = {},
  ) =>
    request<RunsPage>(
      `/api/projects/${projectKey}/builds/${buildFingerprint}/runs${toQueryString(params)}`,
    ),
  findingDebate: (runId: string, findingId: string) =>
    request<FindingDebate>(
      `/api/runs/${runId}/findings/${findingId}/debate`,
    ),
  // ---- Admin: user management -----------------------------------------
  listUsers: () => request<UserListResponse>("/api/users"),
  createUser: (username: string, initial_password: string, role: Role) =>
    request<UserSummary>("/api/users", {
      method: "POST",
      body: JSON.stringify({ username, initial_password, role }),
    }),
  updateUser: (
    userId: string,
    body: { username?: string; role?: Role },
  ) =>
    request<UserSummary>(`/api/users/${userId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  resetUserPassword: (userId: string, new_password: string) =>
    request<UserSummary>(`/api/users/${userId}/reset-password`, {
      method: "POST",
      body: JSON.stringify({ new_password }),
    }),
  deleteUser: (userId: string) =>
    request<void>(`/api/users/${userId}`, { method: "DELETE" }),
  disableUser: (userId: string) =>
    request<UserSummary>(`/api/users/${userId}/disable`, { method: "POST" }),
  enableUser: (userId: string) =>
    request<UserSummary>(`/api/users/${userId}/enable`, { method: "POST" }),
};

export function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        refetchOnWindowFocus: false,
        staleTime: 5_000,
        retry: (failureCount, error) => {
          if (error instanceof ApiError && error.status === 401) return false;
          return failureCount < 2;
        },
      },
    },
  });
}

// React Query hooks --------------------------------------------------------

export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: () => api.me(),
    retry: false,
  });
}

export function useUsers(enabled = true) {
  return useQuery({
    queryKey: ["users"],
    queryFn: () => api.listUsers(),
    enabled,
  });
}

export function useRuns(params: Parameters<typeof api.listRuns>[0] = {}) {
  return useQuery({
    queryKey: ["runs", params],
    queryFn: () => api.listRuns(params),
    refetchInterval: (query) => {
      const page = query.state.data;
      if (!page) return false;
      return page.items.some((r) => r.status === "in_progress") ? 3000 : false;
    },
  });
}

export function useRun(runId: string, filters?: FindingFilters) {
  return useQuery({
    // Include the filter object in the queryKey so toggling coder chips
    // re-fetches with the new ``coder_status`` selection. React Query
    // serializes objects with stable key ordering so identical filter
    // states cache-hit, distinct ones miss.
    queryKey: ["run", runId, filters ?? null],
    queryFn: () => api.getRun(runId, filters),
    refetchInterval: (query) => {
      const run = query.state.data;
      return run && run.status === "in_progress" ? 3000 : false;
    },
  });
}

export function useRunProgress(runId: string, enabled = true) {
  return useQuery({
    queryKey: ["run-progress", runId],
    queryFn: () => api.runProgress(runId),
    enabled,
    refetchInterval: 3000,
  });
}

export function useFindings(
  runId: string,
  filters: FindingFilters,
  options: { pollWhileRunning?: boolean } = {},
) {
  const { pollWhileRunning = false } = options;
  return useQuery({
    queryKey: ["findings", runId, filters],
    queryFn: () => api.listFindings(runId, filters),
    // React Query contract: `refetchInterval` runs background fetches only —
    // it does NOT reset local component state, does NOT collapse expanded
    // finding cards, and does NOT flash a loading spinner. The caller owns
    // the decision of when to poll (typically `runStatus === "in_progress"`).
    // Keep this contract intact across future refactors.
    refetchInterval: pollWhileRunning ? 3000 : false,
  });
}

export function useFinding(findingId: string) {
  return useQuery({
    queryKey: ["finding", findingId],
    queryFn: () => api.getFinding(findingId),
  });
}

interface CoverageHookOptions {
  pollWhileRunning?: boolean;
}

export function useCoverageSummary(
  runId: string,
  options: CoverageHookOptions = {},
) {
  const { pollWhileRunning = false } = options;
  return useQuery({
    queryKey: ["coverage-summary", runId],
    queryFn: () => api.runCoverageSummary(runId),
    enabled: Boolean(runId),
    refetchInterval: pollWhileRunning ? 3000 : false,
  });
}

export function useCoverageModules(
  runId: string,
  params: CoverageListParams,
  options: CoverageHookOptions = {},
) {
  const { pollWhileRunning = false } = options;
  return useQuery({
    queryKey: ["coverage-modules", runId, params],
    queryFn: () => api.runCoverageModules(runId, params),
    enabled: Boolean(runId),
    // Background refresh contract (same as useFindings): do not reset
    // component state, do not flash a spinner, keep previous data visible
    // while paging. Preserves filter / status / scroll state on every poll.
    placeholderData: (previous) => previous,
    refetchInterval: pollWhileRunning ? 3000 : false,
  });
}

export function useCoverageFiles(
  runId: string,
  params: CoverageListParams,
  options: CoverageHookOptions = {},
) {
  const { pollWhileRunning = false } = options;
  return useQuery({
    queryKey: ["coverage-files", runId, params],
    queryFn: () => api.runCoverageFiles(runId, params),
    enabled: Boolean(runId),
    placeholderData: (previous) => previous,
    refetchInterval: pollWhileRunning ? 3000 : false,
  });
}

export function useCoverageFunctions(
  runId: string,
  params: CoverageListParams,
  options: CoverageHookOptions = {},
) {
  const { pollWhileRunning = false } = options;
  return useQuery({
    queryKey: ["coverage-functions", runId, params],
    queryFn: () => api.runCoverageFunctions(runId, params),
    enabled: Boolean(runId),
    placeholderData: (previous) => previous,
    refetchInterval: pollWhileRunning ? 3000 : false,
  });
}

export function useEffectiveConfig() {
  return useQuery({
    queryKey: ["config", "effective"],
    queryFn: () => api.effectiveConfig(),
  });
}

export function useEffectiveLLMConfig() {
  return useQuery({
    queryKey: ["config", "effective", "llm"],
    queryFn: () => api.effectiveLLMConfig(),
  });
}

export function useProjects(params: { limit?: number; offset?: number }) {
  const onFirstPage = (params.offset ?? 0) === 0;
  return useQuery({
    queryKey: ["projects", params],
    queryFn: () => api.listProjects(params),
    // Poll every 3s on page 1 (newly-started audits surface as the
    // `RunningAuditsBadge`) OR when any visible row reports
    // `running_audit_runs > 0` (badge count keeps up with starts/stops).
    // Pages >=2 with only idle rows are historical views and do not poll.
    // Mirrors the `useBuildsForProject` / `useRunsForBuild` rule.
    refetchInterval: (query) => {
      const page = query.state.data;
      if (!page) return false;
      if (onFirstPage) return 3000;
      return page.items.some((p) => p.running_audit_runs > 0) ? 3000 : false;
    },
  });
}

export function useBuildsForProject(
  projectKey: string,
  params: { limit?: number; offset?: number },
) {
  const onFirstPage = (params.offset ?? 0) === 0;
  return useQuery({
    queryKey: ["builds", projectKey, params],
    queryFn: () => api.listBuilds(projectKey, params),
    enabled: Boolean(projectKey),
    // Poll every 3s when the user is on page 1 (new builds appear at the
    // top) OR when any row on the current page is still in_progress (live
    // status / last-run updates). Pages >=2 with only terminal rows are
    // historical views; re-fetching returns identical bytes.
    refetchInterval: (query) => {
      const page = query.state.data;
      if (!page) return false;
      if (onFirstPage) return 3000;
      return page.items.some((b) => b.last_run_status === "in_progress")
        ? 3000
        : false;
    },
  });
}

export function useRunsForBuild(
  projectKey: string,
  buildFingerprint: string,
  params: { limit?: number; offset?: number },
) {
  const onFirstPage = (params.offset ?? 0) === 0;
  return useQuery({
    queryKey: ["build-runs", projectKey, buildFingerprint, params],
    queryFn: () => api.listBuildRuns(projectKey, buildFingerprint, params),
    enabled: Boolean(projectKey) && Boolean(buildFingerprint),
    // Poll every 3s when the user is on page 1 (new runs appear at the
    // top — critical for `xauditor audit` starts in another shell to show
    // up without a manual reload) OR when any row on the current page is
    // still in_progress (live progress-percent updates on existing rows).
    // Pages >=2 with only terminal rows are historical and do not poll.
    refetchInterval: (query) => {
      const page = query.state.data;
      if (!page) return false;
      if (onFirstPage) return 3000;
      return page.items.some((r) => r.status === "in_progress") ? 3000 : false;
    },
  });
}

export function useFindingDebate(runId: string, findingId: string, enabled: boolean) {
  return useQuery({
    queryKey: ["finding-debate", runId, findingId],
    queryFn: () => api.findingDebate(runId, findingId),
    enabled,
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 404) return false;
      return failureCount < 1;
    },
  });
}

export function useSaveFeedback(findingId: string, runId?: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (input: {
      label: FeedbackLabel;
      note?: string;
      existing: boolean;
      duplicateOfFindingId?: string | null;
    }) => {
      if (input.existing) {
        return api.updateFeedback(
          findingId,
          input.label,
          input.note,
          input.duplicateOfFindingId,
        );
      }
      return api.createFeedback(
        findingId,
        input.label,
        input.note,
        input.duplicateOfFindingId,
      );
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["finding", findingId] });
      qc.invalidateQueries({ queryKey: ["findings"] });
      // Also refresh the run header — its Valid/FP breakdowns depend on
      // this finding's annotation. `useRun` doesn't poll terminal runs, so
      // without this invalidate a reviewer would see stale header numbers
      // after labeling on a `completed` / `failed` / `cancelled` run.
      if (runId) {
        qc.invalidateQueries({ queryKey: ["run", runId] });
      }
      // Reverse-query lists shown on the canonical finding's
      // expanded card need to refresh too.
      qc.invalidateQueries({ queryKey: ["finding", findingId, "duplicates"] });
    },
  });
}

export function useIncomingDuplicates(
  findingId: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ["finding", findingId, "duplicates"],
    queryFn: () => api.listIncomingDuplicates(findingId),
    enabled: enabled && Boolean(findingId),
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 404) return false;
      return failureCount < 1;
    },
  });
}


export function useSaveSnapshot() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (args: { body: Record<string, unknown>; note?: string }) =>
      api.saveConfigSnapshot(args.body, args.note),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["config", "effective"] });
    },
  });
}
