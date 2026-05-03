export type AuditMode = "single" | "team";

export type RunStatus =
  | "in_progress"
  | "completed"
  | "failed"
  | "cancelled";

export interface RunSummary {
  id: string;
  repo_root: string;
  project_name: string;
  build_fingerprint: string;
  mode: AuditMode;
  status: RunStatus;
  progress_percent: number;
  total_candidates: number;
  valid_findings: number;
  false_positives: number;
  unlabeled_findings: number;
  duplicate_findings: number;
  started_at: string;
  completed_at: string | null;
}

export interface FeedbackBreakdown {
  base: number;
  added_by_feedback: number;
  removed_by_feedback: number;
  duplicates_in_bucket: number;
  net: number;
}

export interface RunDetail extends RunSummary {
  report_dir: string | null;
  llm_providers_used: Record<string, unknown>;
  valid_findings_breakdown: FeedbackBreakdown;
  false_positives_breakdown: FeedbackBreakdown;
  valid_rate: number | null;
}

export interface RunsPage {
  items: RunSummary[];
  total: number;
}

export interface ProgressEvent {
  stage: string;
  heartbeat_kind: string;
  current_path_index: number | null;
  total_paths: number | null;
  message: string;
  timestamp: string;
}

export type FeedbackLabel =
  | "true_positive"
  | "false_positive"
  | "duplicate"
  | "unlabeled";

export interface DuplicateOfSummary {
  id: string;
  finding_id: string;
  name: string;
}

export interface IncomingDuplicate {
  id: string;
  finding_id: string;
  name: string;
  run_id: string;
  annotation_id: string;
}

export interface IncomingDuplicatesPage {
  items: IncomingDuplicate[];
  total: number;
}

export type CoderStatus =
  | "Verified"
  | "Not Verified"
  | "Inconclusive"
  | "Skipped"
  | "Pending"
  | "Fail";

export interface CoderEvidence {
  file_path: string;
  function_name: string | null;
  snippet: string;
  language: string | null;
  role: string;
  ordinal: number;
}

export interface FindingSummary {
  id: string;
  run_id: string;
  finding_id: string;
  finding_name: string;
  confidence_level: string;
  validation_status: string;
  exploitation_status: string;
  file_path: string | null;
  function_name: string | null;
  suspect_line: number | null;
  feedback_label: FeedbackLabel | null;
  has_debate: boolean;
  coder_status: CoderStatus;
  created_at: string;
}

export interface ProjectSummary {
  project_key: string;
  project_name: string;
  repo_root: string;
  total_graph_builds: number;
  total_audit_runs: number;
  added_at: string;
}

export interface ProjectsPage {
  items: ProjectSummary[];
  total: number;
}

export interface BuildSummary {
  build_fingerprint: string;
  built_at: string;
  total_audit_runs: number;
  last_run_started_at: string;
  last_run_status: RunStatus | "";
}

export interface BuildsPage {
  items: BuildSummary[];
  total: number;
}

export interface DebateRound {
  round_index: number;
  turns: Array<{
    subagent_index?: number;
    provider_name?: string;
    verdict?: string;
    rebuttal?: string;
    system_prompt?: string;
    user_message?: string;
    raw_response?: string;
  }>;
}

export interface FindingDebate {
  id: string;
  path_fingerprint: string;
  finding_ref: string;
  rounds: DebateRound[];
  final_verdict: string;
  convergence_state: string;
  configured_round_cap: number;
}

export interface LLMAgentEffectiveRow {
  agent: string;
  provider: string | null;
  model_name: string | null;
  temperature: number | null;
  top_p: number | null;
  top_k: number | null;
  repetition_penalty: number | null;
  thinking_enabled: boolean | null;
  sources: Record<string, ConfigSource>;
  request_timeout_seconds?: number | null;
}

export interface LLMEffectiveConfig {
  agents: LLMAgentEffectiveRow[];
  providers: string[];
  default_provider: string | null;
}

export interface FindingDetail extends FindingSummary {
  finding_description: string;
  analyzer_status: string | null;
  evidence_strength: string | null;
  analysis: string;
  reason: string;
  context: string;
  business_context: string;
  context_notes: string | null;
  exploitation_steps: string;
  validation_analysis: string;
  source_references: Array<{
    file_path: string;
    snippet: string;
    language: string | null;
    ordinal: number;
  }>;
  referenced_symbols: Array<Record<string, unknown>>;
  coder_analysis: string;
  coder_reason: string;
  coder_call_chain_evidence: CoderEvidence[];
  duplicate_of_finding_id?: string | null;
  duplicate_of?: DuplicateOfSummary | null;
}

export interface FeedbackPayload {
  label: FeedbackLabel;
  researcher_note?: string | null;
  reviewer_username: string;
  updated_at: string;
  created_at: string;
  duplicate_of_finding_id?: string | null;
  duplicate_of?: DuplicateOfSummary | null;
}

export type Role = "admin" | "auditor" | "viewer";

export interface Me {
  id: string;
  username: string;
  must_change_password: boolean;
  role: Role;
}

export interface UserSummary {
  id: string;
  username: string;
  role: Role;
  must_change_password: boolean;
  disabled_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface UserListResponse {
  items: UserSummary[];
  total: number;
}

export type ConfigSource = "yml" | "env" | "db" | "default";

export interface ConfigField {
  value: unknown;
  source: ConfigSource;
  overridden_by_yml: boolean;
}

export interface RedactedKey {
  present: boolean;
  source: ConfigSource;
}

export interface EffectiveConfig {
  values: Record<string, unknown>;
  fields: Record<string, ConfigField>;
  overridden_keys: string[];
  redacted_keys?: Record<string, RedactedKey>;
}

export interface ConfigSnapshot {
  id: string;
  version: number;
  note: string | null;
  created_at: string;
}

export interface FindingFilters {
  file?: string;
  function?: string;
  confidence?: string;
  validation_status?: string;
  exploitation_status?: string;
  feedback_label?: FeedbackLabel;
  q?: string;
}

export type CoverageStatus = "audited" | "unaudited" | "excluded" | "skipped";

export interface CoverageCategorySummary {
  total: number;
  by_status: Record<CoverageStatus, number>;
  percent_audited: number;
}

export interface CoverageSummary {
  modules: CoverageCategorySummary;
  files: CoverageCategorySummary;
  functions: CoverageCategorySummary;
}

export interface CoverageModuleItem {
  module_name: string;
  status: CoverageStatus | string;
  reason: string | null;
}

export interface CoverageFileItem {
  file_path: string;
  status: CoverageStatus | string;
  reason: string | null;
}

export interface CoverageFunctionItem {
  function_id: string;
  qualified_name: string;
  file_path: string;
  status: CoverageStatus | string;
  reason: string | null;
}

export interface CoveragePage<T> {
  items: T[];
  total: number;
}

export interface CoverageListParams {
  limit?: number;
  offset?: number;
  status?: CoverageStatus | string;
  q?: string;
}
