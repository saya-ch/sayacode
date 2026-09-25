export type ThreadStatus =
  | "idle"
  | "pending"
  | "running"
  | "stopping"
  | "paused"
  | "completed"
  | "rewound"
  | "stopped"
  | "interrupted"
  | "error"
  | "failed";

export type TrustLevel = "read_only" | "ask" | "workspace_auto" | "full";

export interface Workspace {
  id: string;
  path: string;
  name: string;
  active_session_id?: string | null;
}

export interface DirectoryListing {
  path: string;
  parent: string | null;
  roots: string[];
  directories: { name: string; path: string }[];
  truncated: boolean;
}

export interface Session {
  id: string;
  title: string;
  status: ThreadStatus;
  updated_at: string | null;
  workspace_id: string;
}

export interface SessionDeletionPreview {
  thread_id: string;
  allowed: boolean;
  blockers: string[];
  warnings: string[];
  child_task_count: number;
  keeps_audit: boolean;
  keeps_long_term_memory: boolean;
  keeps_workspace_files: boolean;
}

export interface SessionDeletionResult {
  deleted: boolean;
  thread_id: string;
  next_session_id: string | null;
  warnings?: string[];
}

export interface Attachment {
  id: string;
  name: string;
  size: number;
  path: string;
  kind: "text" | "binary";
  media_type: string;
}

export interface QueuedMessage {
  message_id: string;
  thread_id: string;
  text: string;
  status: "queued" | "pending";
  created_at: string;
  attachments: Attachment[];
}

export interface AgentMessage {
  id: string;
  role: "human" | "user" | "assistant" | "ai" | "tool" | "system" | "agent_inbox";
  text: string;
  created_at?: string | null;
  agent_name?: string | null;
  tool_call_id?: string | null;
  tool_name?: string | null;
  tool_input?: Record<string, unknown> | null;
  status?: string | null;
  has_tool_calls?: boolean;
}

export interface Todo {
  id: string;
  content: string;
  status: "pending" | "in_progress" | "completed";
}

export interface ApprovalAction {
  name: string;
  args: Record<string, unknown>;
  description?: string;
}

export interface PendingApproval {
  checkpoint_id: string;
  actions: ApprovalAction[];
}

export interface Activity {
  id: string;
  type: string;
  at?: string | null;
  summary?: string | null;
  tool_name?: string | null;
  status?: string | null;
  duration_ms?: number | null;
  data?: unknown;
  thread_id?: string | null;
  run_id?: string | null;
  task_id?: string | null;
}

export interface ActiveRun {
  run_id: string;
  started_at: string;
  status: string;
  source?: string | null;
}

export interface Task {
  id: string;
  thread_id: string;
  parent_thread_id: string | null;
  title: string;
  role: string;
  status: ThreadStatus;
  workspace_id: string;
  created_at: string | null;
  updated_at: string | null;
  result?: string | null;
  error?: string | null;
  last_outcome?: string | null;
  stopped_reason?: string | null;
  unconfirmed_effects?: boolean;
  recovery_note?: string | null;
  delivery_state?: string | null;
  worktree_enabled?: boolean;
}

export interface ThreadSnapshot {
  thread_id: string;
  workspace_id: string;
  title: string;
  status: ThreadStatus;
  trust_level?: string | null;
  effective_model?: string | null;
  model_source?: "thread" | "task" | "default";
  profile_override_name?: string | null;
  queued_messages?: QueuedMessage[];
  resume_available?: boolean;
  pending_steps?: boolean;
  messages: AgentMessage[];
  todos: Todo[];
  pending_approval?: PendingApproval | null;
  tasks: Task[];
  activity?: Activity[];
  active_run?: ActiveRun | null;
}

export interface StreamEvent {
  instance_id: string;
  seq: number;
  type: string;
  workspace_id: string;
  thread_id?: string | null;
  task_id?: string | null;
  run_id?: string | null;
  at?: string | null;
  data: Record<string, unknown>;
}

export interface RunReceipt {
  run_id: string;
  thread_id: string;
  status: string;
}

export interface TaskActionResult {
  task?: Task | null;
  diff?: string | null;
  status?: string | null;
  message?: string | null;
  result?: string | null;
}

export interface ApprovalDecision {
  type: "approve" | "reject";
  message?: string;
}

export interface ApprovalGrant {
  index: number;
  tool_name: string;
}

export interface StatusResponse {
  model?: string | null;
  protocol?: string | null;
  trust_level?: string | null;
  workspace_id?: string | null;
  session_id?: string | null;
  running_tasks?: number;
  running_agents?: number;
  csrf_token?: string;
}

export interface SettingsResponse {
  default_trust?: TrustLevel;
  active_profile?: string | null;
  language?: string;
  memory_enabled?: boolean;
  output_limit_bytes?: number;
  task_notice_limit_bytes?: number;
  max_consecutive_wakes?: number;
  shutdown_grace_seconds?: number;
}

export interface ModelProfile {
  name: string;
  protocol: string;
  base_url: string;
  model_id: string;
  context_length: number;
  max_output_tokens: number;
  has_api_key: boolean;
  summary_trigger_ratio?: number | null;
}

export interface ModelCatalog {
  active_profile: string | null;
  profiles: ModelProfile[];
}

export interface ModelInput {
  name?: string;
  protocol: string;
  base_url: string;
  api_key: string | null;
  model_id: string;
  context_length: number;
  max_output_tokens: number;
}

export interface McpServer {
  name: string;
  scope: string;
  status: string;
  tools?: string[] | null;
  error?: string | null;
}

export interface McpCatalog {
  trusted_project: boolean;
  servers: McpServer[];
  available_tools?: string[];
}

export interface Skill {
  name: string;
  description: string;
  source: string;
  path: string;
  active?: boolean | null;
}

export interface MemoryRecord {
  id: string;
  scope: "user" | "project";
  subject: string;
  text: string;
  state: string;
  pinned: boolean;
  updated_at?: string | null;
}

export interface MemoryEvidence {
  source_ref: string;
  message_id: string;
  role: string;
  preview: string;
}

export interface MemoryDetail extends MemoryRecord {
  validity_reason: string | null;
  evidence: MemoryEvidence[];
}

export interface MemorySettings {
  enabled: boolean;
  use: boolean;
  learn: "off" | "explicit" | "auto";
  model_profile: string | null;
  idle_seconds: number;
  context_ratio: number;
  max_context_tokens: number;
}

export interface GitStatus {
  branch: string | null;
  clean: boolean;
  staged: string[];
  unstaged: string[];
  untracked: string[];
  text?: string | null;
}

export interface SymbolItem {
  name: string;
  kind: string;
  path: string;
  line?: number | null;
}

export interface DoctorCheck {
  name: string;
  status: string;
  detail?: string | null;
}

export interface DoctorResult {
  ok: boolean;
  checks: DoctorCheck[];
  summary?: string | null;
}
