import type {
  ApprovalDecision,
  ApprovalGrant,
  DoctorResult,
  GitStatus,
  McpCatalog,
  MemoryRecord,
  MemoryDetail,
  MemorySettings,
  ModelCatalog,
  ModelInput,
  ModelProfile,
  ReviewerStatus,
  RunReceipt,
  Session,
  SettingsResponse,
  StatusResponse,
  Task,
  TaskActionResult,
  ThreadSnapshot,
  Skill,
  SymbolItem,
  Workspace,
} from "./types";

const API = "/api";
let csrfToken: string | null = null;

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = init.method?.toUpperCase() ?? "GET";
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    if (!csrfToken && path !== "/auth") {
      throw new ApiError(401, "浏览器会话尚未完成认证，请重新从 SAYACODE 打开页面。");
    }
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers,
    credentials: "same-origin",
  });
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`;
    try {
      const payload = (await response.json()) as { detail?: string; message?: string };
      message = payload.detail ?? payload.message ?? message;
    } catch {
      // 非 JSON 错误仍保留 HTTP 状态，避免把服务器页面当作正文展示。
    }
    throw new ApiError(response.status, message);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function body(value: unknown): string {
  return JSON.stringify(value);
}

function query(params: Record<string, string | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value) search.set(key, value);
  });
  const value = search.toString();
  return value ? `?${value}` : "";
}

function encoded(value: string): string {
  return encodeURIComponent(value);
}

type HookStatus = {
  workspace: string;
  project_trusted: boolean;
  user_hooks: number;
  project_hooks: number;
  warnings: string[];
  hooks: { event: string; name: string; source: string; blocking: boolean; timeout: number }[];
};

export async function bootstrapAuth(): Promise<StatusResponse> {
  const hash = new URLSearchParams(window.location.hash.slice(1));
  const launchToken = hash.get("token");
  if (hash.has("token")) {
    history.replaceState(null, "", window.location.pathname + window.location.search);
  }
  if (launchToken) {
    const auth = await request<{ csrf_token: string }>("/auth", {
      method: "POST",
      body: body({ token: launchToken }),
    });
    csrfToken = auth.csrf_token;
  } else {
    const auth = await request<{ csrf_token: string }>("/auth");
    csrfToken = auth.csrf_token;
  }
  const status = await request<StatusResponse>("/status");
  csrfToken = status.csrf_token ?? csrfToken;
  if (!csrfToken) {
    throw new ApiError(401, "此页面没有有效的启动令牌。请从 SAYACODE 重新打开浏览器。");
  }
  return status;
}

export const api = {
  status: () => request<StatusResponse>("/status"),
  settings: () => request<SettingsResponse>("/settings"),
  updateSettings: (updates: Record<string, unknown>) =>
    request<SettingsResponse>("/settings", { method: "PATCH", body: body(updates) }),
  workspaces: async () => (await request<{ workspaces: Workspace[] }>("/workspaces")).workspaces,
  addWorkspace: (path: string, name?: string) =>
    request<Workspace>("/workspaces", { method: "POST", body: body({ path, name }) }),
  renameWorkspace: (id: string, name: string) =>
    request<Workspace>(`/workspaces/${encoded(id)}`, {
      method: "PATCH",
      body: body({ name }),
    }),
  sessions: async (workspaceId: string) =>
    (await request<{ sessions: Session[] }>(`/workspaces/${encoded(workspaceId)}/sessions`))
      .sessions,
  createSession: (workspaceId: string, title?: string) =>
    request<Session>(`/workspaces/${encoded(workspaceId)}/sessions`, {
      method: "POST",
      body: body({ title }),
    }),
  renameThread: (threadId: string, title: string) =>
    request<Session>(`/threads/${encoded(threadId)}`, {
      method: "PATCH",
      body: body({ title }),
    }),
  snapshot: (threadId: string) => request<ThreadSnapshot>(`/threads/${encoded(threadId)}/snapshot`),
  checkpoints: async (threadId: string) =>
    (
      await request<{
        checkpoints: {
          checkpoint_id: string;
          next: string[];
          message_count: number;
          created_at: string | null;
        }[];
      }>(`/threads/${encoded(threadId)}/checkpoints`)
    ).checkpoints,
  compactThread: (threadId: string, focus: string | null) =>
    request<{ compacted: boolean }>(`/threads/${encoded(threadId)}/compact`, {
      method: "POST",
      body: body({ focus }),
    }),
  rewindThread: (threadId: string, checkpointId: string) =>
    request<{ rewound: boolean; checkpoint_id: string; fork_checkpoint_id: string | null }>(
      `/threads/${encoded(threadId)}/rewind`,
      { method: "POST", body: body({ checkpoint_id: checkpointId }) },
    ),
  traceThread: async (threadId: string, runId?: string) =>
    (
      await request<{
        activity: {
          id: string;
          at: string | null;
          event: string;
          thread_id: string | null;
          task_id: string | null;
          run_id: string | null;
          details: Record<string, unknown> | null;
        }[];
      }>(`/threads/${encoded(threadId)}/trace${query({ run_id: runId })}`)
    ).activity,
  threadTools: async (threadId: string) =>
    (
      await request<{ tools: { name: string; description: string }[] }>(
        `/threads/${encoded(threadId)}/tools`,
      )
    ).tools,
  clearApprovalGrants: (threadId: string) =>
    request<{ cleared: boolean }>(`/threads/${encoded(threadId)}/approvals/grants`, {
      method: "DELETE",
      body: body({}),
    }),
  threadMemorySettings: (threadId: string) =>
    request<{
      thread_id: string;
      use_override: boolean | null;
      learn_override: "off" | "explicit" | "auto" | null;
      use: boolean;
      learn: "off" | "explicit" | "auto";
    }>(`/threads/${encoded(threadId)}/memory/settings`),
  updateThreadMemorySettings: (
    threadId: string,
    patch: { use?: boolean | null; learn?: "off" | "explicit" | "auto" | null },
  ) =>
    request<{
      thread_id: string;
      use_override: boolean | null;
      learn_override: "off" | "explicit" | "auto" | null;
      use: boolean;
      learn: "off" | "explicit" | "auto";
    }>(`/threads/${encoded(threadId)}/memory/settings`, {
      method: "PATCH",
      body: body(patch),
    }),
  startRun: (threadId: string, message: string) =>
    request<RunReceipt>(`/threads/${encoded(threadId)}/runs`, {
      method: "POST",
      body: body({ message }),
    }),
  approve: (
    threadId: string,
    checkpointId: string,
    decisions: ApprovalDecision[],
    grants: ApprovalGrant[] = [],
  ) =>
    request<RunReceipt>(`/threads/${encoded(threadId)}/approvals`, {
      method: "POST",
      body: body({ checkpoint_id: checkpointId, decisions, grants }),
    }),
  setTrust: (threadId: string, trustLevel: "read_only" | "ask" | "jev" | "full") =>
    request<{ trust_level: string }>(`/threads/${encoded(threadId)}/trust`, {
      method: "PATCH",
      body: body({ trust_level: trustLevel }),
    }),
  tasks: async (workspaceId: string) =>
    (await request<{ tasks: Task[] }>(`/tasks${query({ workspace_id: workspaceId })}`)).tasks,
  createTask: (input: {
    parent_thread_id: string;
    role: string;
    prompt: string;
    title?: string;
    worktree_enabled?: boolean;
  }) => request<Task>("/tasks", { method: "POST", body: body(input) }),
  taskAction: (taskId: string, action: string, data: Record<string, unknown> = {}) =>
    request<TaskActionResult>(`/tasks/${encoded(taskId)}/${encoded(action)}`, {
      method: "POST",
      body: body(data),
    }),
  models: () => request<ModelCatalog>("/models"),
  addModel: (input: ModelInput) =>
    request<ModelProfile>("/models", { method: "POST", body: body(input) }),
  updateModel: (name: string, patch: Record<string, unknown>) =>
    request<ModelProfile>(`/models/${encoded(name)}`, { method: "PATCH", body: body(patch) }),
  testModel: (name: string) =>
    request<{
      profile: string;
      ok: boolean;
      text: boolean;
      tool_calling: boolean;
      stream: boolean;
      errors: Record<string, string>;
    }>(`/models/${encoded(name)}/test`, { method: "POST", body: body({}) }),
  useModel: (name: string) =>
    request<{ active_profile: string | null }>(`/models/${encoded(name)}/use`, {
      method: "POST",
      body: body({}),
    }),
  deleteModel: (name: string) =>
    request<{ active_profile: string | null }>(`/models/${encoded(name)}`, {
      method: "DELETE",
      body: body({}),
    }),
  mcp: (workspaceId: string) => request<McpCatalog>(`/workspaces/${encoded(workspaceId)}/mcp`),
  hooks: (workspaceId: string) => request<HookStatus>(`/workspaces/${encoded(workspaceId)}/hooks`),
  trustHooks: (workspaceId: string, trusted: boolean) =>
    request<HookStatus>(`/workspaces/${encoded(workspaceId)}/hooks/trust`, {
      method: "PATCH",
      body: body({ trusted }),
    }),
  reloadHooks: (workspaceId: string) =>
    request<HookStatus>(`/workspaces/${encoded(workspaceId)}/hooks/reload`, {
      method: "POST",
      body: body({}),
    }),
  hookAudit: async (workspaceId: string) =>
    (
      await request<{
        activity: {
          id: string;
          at: string | null;
          event: string;
          name: string;
          source: string;
          returncode: number;
          blocked: boolean;
        }[];
      }>(`/workspaces/${encoded(workspaceId)}/hooks/audit`)
    ).activity,
  addMcp: (
    workspaceId: string,
    input: { name: string; scope: "user" | "project"; config: Record<string, unknown> },
  ) =>
    request<{ status: string }>(`/workspaces/${encoded(workspaceId)}/mcp/servers`, {
      method: "POST",
      body: body(input),
    }),
  removeMcp: (workspaceId: string, name: string, scope: "user" | "project") =>
    request<{ status: string }>(
      `/workspaces/${encoded(workspaceId)}/mcp/servers/${encoded(name)}${query({ scope })}`,
      { method: "DELETE", body: body({}) },
    ),
  trustMcp: (workspaceId: string, trusted: boolean) =>
    request<{ status: string }>(`/workspaces/${encoded(workspaceId)}/mcp/trust`, {
      method: "POST",
      body: body({ trusted }),
    }),
  reloadMcp: (workspaceId: string) =>
    request<{ status: string }>(`/workspaces/${encoded(workspaceId)}/mcp/reload`, {
      method: "POST",
      body: body({}),
    }),
  skills: async (workspaceId: string) =>
    (await request<{ skills: Skill[] }>(`/workspaces/${encoded(workspaceId)}/skills`)).skills,
  activateSkill: (threadId: string, name: string) =>
    request<{ name: string; active: boolean }>(
      `/threads/${encoded(threadId)}/skills/${encoded(name)}/activate`,
      { method: "POST", body: body({}) },
    ),
  memories: async (workspaceId: string, scope?: "user" | "project", search?: string) =>
    (
      await request<{ records: MemoryRecord[] }>(
        `/memory${query({ workspace_id: workspaceId, scope, query: search })}`,
      )
    ).records,
  memoryDetail: (workspaceId: string, id: string) =>
    request<MemoryDetail>(`/memory/${encoded(id)}${query({ workspace_id: workspaceId })}`),
  memorySettings: () => request<MemorySettings>("/memory/settings"),
  updateMemorySettings: (workspaceId: string, patch: Partial<MemorySettings>) =>
    request<MemorySettings>(`/memory/settings${query({ workspace_id: workspaceId })}`, {
      method: "PATCH",
      body: body(patch),
    }),
  remember: (workspaceId: string, scope: "user" | "project", text: string) =>
    request<MemoryRecord>("/memory", {
      method: "POST",
      body: body({ workspace_id: workspaceId, scope, text }),
    }),
  correctMemory: (workspaceId: string, id: string, text: string) =>
    request<MemoryRecord>(`/memory/${encoded(id)}${query({ workspace_id: workspaceId })}`, {
      method: "PATCH",
      body: body({ text }),
    }),
  confirmMemory: (workspaceId: string, id: string) =>
    request<MemoryRecord>(`/memory/${encoded(id)}/confirm${query({ workspace_id: workspaceId })}`, {
      method: "POST",
      body: body({}),
    }),
  pinMemory: (workspaceId: string, id: string, pinned: boolean) =>
    request<MemoryRecord>(`/memory/${encoded(id)}/pin${query({ workspace_id: workspaceId })}`, {
      method: "POST",
      body: body({ pinned }),
    }),
  forgetMemory: (workspaceId: string, id: string) =>
    request<MemoryRecord>(`/memory/${encoded(id)}${query({ workspace_id: workspaceId })}`, {
      method: "DELETE",
      body: body({}),
    }),
  gitStatus: (workspaceId: string) =>
    request<GitStatus>(`/workspaces/${encoded(workspaceId)}/git/status`),
  symbols: async (workspaceId: string, search: string) =>
    (
      await request<{ symbols: SymbolItem[] }>(
        `/workspaces/${encoded(workspaceId)}/symbols${query({ query: search })}`,
      )
    ).symbols,
  analysis: async (workspaceId: string) =>
    (await request<{ text: string }>(`/workspaces/${encoded(workspaceId)}/analysis`)).text,
  doctor: (workspaceId: string) =>
    request<DoctorResult>(`/workspaces/${encoded(workspaceId)}/doctor`),
  reviewer: () => request<ReviewerStatus>("/reviewer"),
  configureReviewer: (input: { base_url: string; api_key: string; model_id: string }) =>
    request<ReviewerStatus>("/reviewer", { method: "PUT", body: body(input) }),
  testReviewer: () =>
    request<{ ok: boolean; error?: string | null }>("/reviewer/test", {
      method: "POST",
      body: body({}),
    }),
  deleteReviewer: () => request<ReviewerStatus>("/reviewer", { method: "DELETE", body: body({}) }),
};
