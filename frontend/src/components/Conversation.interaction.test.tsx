// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { WorkspaceState } from "../state/useWorkspace";
import { ChatMessage, Conversation } from "./Conversation";

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) =>
    setTimeout(callback, 0),
  );
  vi.stubGlobal("cancelAnimationFrame", clearTimeout);
  vi.spyOn(api, "models").mockResolvedValue({ profiles: [], active_profile: null });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
function snapshot(status = "idle") {
  return {
    thread_id: "session",
    workspace_id: "workspace",
    title: "测试会话",
    status,
    trust_level: "ask",
    effective_model: "anthropic-model",
    effective_protocol: "anthropic_messages",
    messages: [],
    todos: [],
    tasks: [],
    active_skills: [],
  };
}
function state(status = "idle") {
  return {
    status: { protocol: "openai_chat_completions" },
    workspaceId: "workspace",
    sessionId: "session",
    threadId: "session",
    snapshot: snapshot(status),
    parentSnapshot: snapshot(status),
    settings: { active_profile: "openai-default" },
    workspaces: [{ id: "workspace", name: "项目" }],
    sessions: [],
    tasks: [],
    liveEvents: [],
    liveText: "",
    busy: false,
    connection: "connected",
    contextFeedback: null,
    send: vi.fn().mockResolvedValue(undefined),
    stopSession: vi.fn().mockResolvedValue(undefined),
    resumeSession: vi.fn().mockResolvedValue(undefined),
    taskAction: vi.fn().mockResolvedValue({}),
  } as unknown as WorkspaceState;
}
async function render(current: WorkspaceState) {
  await act(async () =>
    root.render(
      <Conversation
        state={current}
        onOpenSidebar={() => {}}
        onOpenInspector={() => {}}
        onOpenApproval={() => {}}
        onOpenProducts={() => {}}
      />,
    ),
  );
}
async function click(label: string) {
  const button = [...container.querySelectorAll<HTMLButtonElement>("button")].find(
    (item) => item.getAttribute("aria-label") === label,
  );
  expect(button, label).toBeDefined();
  await act(async () => button?.click());
}
async function type(value: string) {
  const input = container.querySelector<HTMLTextAreaElement>("#agent-composer")!;
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!.call(
      input,
      value,
    );
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

it("底栏协议跟随当前线程模型切换；没有线程协议时不回退显示全局默认", async () => {
  const current = state();
  await render(current);
  expect(container.textContent).toContain("anthropic_messages");
  expect(container.textContent).not.toContain("openai_chat_completions");
  await render({
    ...current,
    threadId: "child",
    snapshot: {
      ...current.snapshot!,
      thread_id: "child",
      effective_model: "gemini",
      effective_protocol: "gemini_generate_content",
    },
  });
  expect(container.textContent).toContain("gemini_generate_content");
  expect(container.textContent).not.toContain("anthropic_messages");
  await render({ ...current, snapshot: { ...current.snapshot!, effective_protocol: null } });
  expect(container.textContent).toContain("未配置协议");
  expect(container.textContent).not.toContain("openai_chat_completions");
});

it("运行中空输入停止整个会话树，输入内容后切换为排队发送", async () => {
  const current = state("running");
  await render(current);
  await click("停止本会话及全部子 Agent");
  expect(current.stopSession).toHaveBeenCalledOnce();
  await type("补充约束");
  await click("发送消息");
  expect(current.send).toHaveBeenCalledWith("补充约束", [], expect.any(String));
});

it("停止后空输入恢复原会话，有输入仍可排队而不触发恢复", async () => {
  const current = state("stopped");
  await render(current);
  await click("恢复会话");
  expect(current.resumeSession).toHaveBeenCalledOnce();
  await type("恢复后检查测试");
  await click("发送消息");
  expect(current.send).toHaveBeenCalledWith("恢复后检查测试", [], expect.any(String));
  expect(current.resumeSession).toHaveBeenCalledTimes(1);
});

it("主子线程草稿独立，切回主线程后原草稿仍存在", async () => {
  const current = state();
  await render(current);
  await type("主线程草稿");
  await render({
    ...current,
    threadId: "child",
    snapshot: { ...current.snapshot!, thread_id: "child" },
  });
  expect((container.querySelector("textarea") as HTMLTextAreaElement).value).toBe("");
  await type("子线程草稿");
  await render(current);
  expect((container.querySelector("textarea") as HTMLTextAreaElement).value).toBe("主线程草稿");
});

it("入队请求失败保留原草稿，重试复用相同消息标识避免重复发送", async () => {
  const current = state();
  vi.mocked(current.send).mockRejectedValueOnce(new Error("网络失败"));
  await render(current);
  await type("保留这段任务");
  await click("发送消息");
  expect((container.querySelector("textarea") as HTMLTextAreaElement).value).toBe("保留这段任务");
  await click("发送消息");
  expect(current.send).toHaveBeenCalledTimes(2);
  expect(vi.mocked(current.send).mock.calls[0]?.[2]).toBe(
    vi.mocked(current.send).mock.calls[1]?.[2],
  );
});

it("上下文摘要以可展开的摘要行显示，不冒充用户或主 Agent 发言", async () => {
  const current = state();
  current.snapshot = {
    ...current.snapshot!,
    messages: [
      {
        id: "summary",
        role: "context_summary",
        text: "已完成的上下文与当前约束",
      },
    ],
  };
  await act(async () =>
    root.render(
      <ChatMessage message={current.snapshot!.messages[0]!} agentName="SAYA" color="black" />,
    ),
  );
  expect(container.querySelector('[aria-label="上下文摘要"]')).toBeTruthy();
  expect(container.querySelector("summary")?.textContent).toBe("上下文摘要");
  expect(container.textContent).toContain("已完成的上下文与当前约束");
  expect(container.textContent).not.toContain("你");
  expect(container.textContent).not.toContain("SAYA");
});
