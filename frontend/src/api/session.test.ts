import { afterEach, expect, it, vi } from "vitest";
import { api, bootstrapAuth } from "./client";

afterEach(() => vi.unstubAllGlobals());

it("会话与 Hook 方法使用现有认证，并按路由发送类型化请求", async () => {
  const calls: { url: string; method: string; body: unknown; csrf: string | null }[] = [];
  vi.stubGlobal("window", { location: { hash: "#token=launch", pathname: "/", search: "" } });
  vi.stubGlobal("history", { replaceState: vi.fn() });
  const responses: Record<string, unknown> = {
    "/api/threads/thread%2Fone/checkpoints": {
      checkpoints: [{ checkpoint_id: "cp-1", next: [], message_count: 2, created_at: null }],
    },
    "/api/threads/thread%2Fone/compact": { compacted: true },
    "/api/threads/thread%2Fone/rewind": {
      rewound: true,
      checkpoint_id: "cp-1",
      fork_checkpoint_id: "cp-2",
    },
    "/api/threads/thread%2Fone/trace?run_id=run%2Fone": {
      activity: [
        {
          id: "a-1",
          event: "model.completed",
          at: null,
          thread_id: "thread/one",
          task_id: null,
          run_id: "run/one",
          details: null,
        },
      ],
    },
    "/api/threads/thread%2Fone/tools": {
      tools: [{ name: "read_file", description: "Read a file" }],
    },
    "/api/threads/thread%2Fone/approvals/grants": { cleared: true },
    "/api/threads/thread%2Fone/trust": { trust_level: "workspace_auto" },
    "/api/settings": { default_trust: "workspace_auto" },
    "/api/threads/thread%2Fone/memory/settings": {
      thread_id: "thread/one",
      use_override: null,
      learn_override: "off",
      use: true,
      learn: "off",
    },
    "/api/workspaces/workspace%2Fone/hooks": {
      workspace: "C:\\test",
      project_trusted: false,
      user_hooks: 0,
      project_hooks: 0,
      warnings: [],
      hooks: [],
    },
    "/api/workspaces/workspace%2Fone/hooks/trust": {
      workspace: "C:\\test",
      project_trusted: true,
      user_hooks: 0,
      project_hooks: 0,
      warnings: [],
      hooks: [],
    },
    "/api/workspaces/workspace%2Fone/hooks/reload": {
      workspace: "C:\\test",
      project_trusted: true,
      user_hooks: 0,
      project_hooks: 0,
      warnings: [],
      hooks: [],
    },
    "/api/workspaces/workspace%2Fone/hooks/audit": {
      activity: [
        {
          id: "h-1",
          at: null,
          event: "before_tool",
          name: "audit",
          source: "project",
          returncode: 0,
          blocked: false,
        },
      ],
    },
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      if (url === "/api/auth")
        return { ok: true, status: 200, json: async () => ({ csrf_token: "csrf-session" }) };
      if (url === "/api/status")
        return { ok: true, status: 200, json: async () => ({ model: "test" }) };
      calls.push({
        url,
        method: init.method ?? "GET",
        body: init.body ? (JSON.parse(init.body as string) as unknown) : null,
        csrf: new Headers(init.headers).get("X-CSRF-Token"),
      });
      expect(init.credentials).toBe("same-origin");
      expect(url in responses).toBe(true);
      return { ok: true, status: 200, json: async () => responses[url] };
    }),
  );

  await bootstrapAuth();
  const threadId = "thread/one";
  const workspaceId = "workspace/one";
  expect((await api.checkpoints(threadId))[0]?.checkpoint_id).toBe("cp-1");
  expect((await api.compactThread(threadId, "保留目标")).compacted).toBe(true);
  expect((await api.rewindThread(threadId, "cp-1")).fork_checkpoint_id).toBe("cp-2");
  expect((await api.traceThread(threadId, "run/one"))[0]?.event).toBe("model.completed");
  expect((await api.threadTools(threadId))[0]?.name).toBe("read_file");
  expect((await api.clearApprovalGrants(threadId)).cleared).toBe(true);
  expect((await api.setTrust(threadId, "workspace_auto")).trust_level).toBe("workspace_auto");
  expect((await api.updateSettings({ default_trust: "workspace_auto" })).default_trust).toBe(
    "workspace_auto",
  );
  expect((await api.threadMemorySettings(threadId)).learn).toBe("off");
  expect((await api.updateThreadMemorySettings(threadId, { use: null, learn: "auto" })).learn).toBe(
    "off",
  );
  expect((await api.hooks(workspaceId)).project_trusted).toBe(false);
  expect((await api.trustHooks(workspaceId, true)).project_trusted).toBe(true);
  expect((await api.reloadHooks(workspaceId)).project_trusted).toBe(true);
  expect((await api.hookAudit(workspaceId))[0]?.name).toBe("audit");

  expect(calls.map(({ url, method, body }) => [url, method, body])).toEqual([
    ["/api/threads/thread%2Fone/checkpoints", "GET", null],
    ["/api/threads/thread%2Fone/compact", "POST", { focus: "保留目标" }],
    ["/api/threads/thread%2Fone/rewind", "POST", { checkpoint_id: "cp-1" }],
    ["/api/threads/thread%2Fone/trace?run_id=run%2Fone", "GET", null],
    ["/api/threads/thread%2Fone/tools", "GET", null],
    ["/api/threads/thread%2Fone/approvals/grants", "DELETE", {}],
    ["/api/threads/thread%2Fone/trust", "PATCH", { trust_level: "workspace_auto" }],
    ["/api/settings", "PATCH", { default_trust: "workspace_auto" }],
    ["/api/threads/thread%2Fone/memory/settings", "GET", null],
    ["/api/threads/thread%2Fone/memory/settings", "PATCH", { use: null, learn: "auto" }],
    ["/api/workspaces/workspace%2Fone/hooks", "GET", null],
    ["/api/workspaces/workspace%2Fone/hooks/trust", "PATCH", { trusted: true }],
    ["/api/workspaces/workspace%2Fone/hooks/reload", "POST", {}],
    ["/api/workspaces/workspace%2Fone/hooks/audit", "GET", null],
  ]);
  expect(calls.filter((call) => call.method === "GET").every((call) => call.csrf === null)).toBe(
    true,
  );
  expect(
    calls.filter((call) => call.method !== "GET").every((call) => call.csrf === "csrf-session"),
  ).toBe(true);
});
