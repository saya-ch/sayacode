// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { ThreadSnapshot } from "../api/types";
import { SessionPanel } from "./session";

const snapshot: ThreadSnapshot = {
  thread_id: "session",
  workspace_id: "workspace",
  title: "测试会话",
  status: "completed",
  effective_model: "thread-model",
  effective_protocol: "anthropic_messages",
  messages: [],
  active_skills: [],
  todos: [],
  tasks: [],
};
let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  vi.spyOn(api, "snapshot").mockResolvedValue(snapshot);
  vi.spyOn(api, "checkpoints").mockResolvedValue([
    {
      checkpoint_id: "middle",
      next: ["tools"],
      message_count: 2,
      created_at: null,
      rewind_available: false,
    },
    {
      checkpoint_id: "complete",
      next: [],
      message_count: 4,
      created_at: null,
      rewind_available: true,
    },
  ]);
  vi.spyOn(api, "traceThread").mockResolvedValue([]);
  vi.spyOn(api, "threadTools").mockResolvedValue([]);
  vi.spyOn(api, "threadMemorySettings").mockResolvedValue({
    thread_id: "session",
    use: true,
    learn: "off",
    use_override: null,
    learn_override: null,
  });
  vi.spyOn(api, "hooks").mockResolvedValue({
    workspace: "workspace",
    project_trusted: false,
    user_hooks: 0,
    project_hooks: 0,
    hooks: [],
    warnings: [],
  });
  vi.spyOn(api, "hookAudit").mockResolvedValue([]);
  vi.spyOn(api, "models").mockResolvedValue({ active_profile: null, profiles: [] });
  vi.spyOn(api, "rewindThread").mockResolvedValue({
    rewound: true,
    checkpoint_id: "complete",
    fork_checkpoint_id: "branch",
  });
  vi.spyOn(api, "compactThread").mockResolvedValue({ compacted: false });
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
});

it("有待执行步骤或排队消息时不允许压缩和回退", async () => {
  vi.mocked(api.snapshot).mockResolvedValue({
    ...snapshot,
    pending_steps: true,
    queued_messages: [
      {
        message_id: "queued",
        thread_id: "session",
        text: "稍后继续",
        status: "queued",
        created_at: "",
        attachments: [],
      },
    ],
  });
  await mount();
  expect(
    [...container.querySelectorAll<HTMLButtonElement>("button")]
      .filter((item) => ["压缩上下文", "回退到此处"].includes(item.textContent?.trim() ?? ""))
      .every((item) => item.disabled),
  ).toBe(true);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});
async function mount() {
  await act(async () => root.render(<SessionPanel workspaceId="workspace" threadId="session" />));
}
async function click(text: string) {
  const button = [...container.querySelectorAll<HTMLButtonElement>("button")].find(
    (item) => !item.disabled && item.textContent?.trim() === text,
  );
  expect(button).toBeDefined();
  await act(async () => button?.click());
}

it("中间工具节点不能触发回退；只有后端标记的完成轮次可以确认并回退", async () => {
  await mount();
  const buttons = [...container.querySelectorAll<HTMLButtonElement>("button")].filter(
    (item) => item.textContent?.trim() === "回退到此处",
  );
  expect(buttons[0]?.disabled).toBe(true);
  expect(buttons[0]?.title).toBe("仅已完成轮次可回退");
  await act(async () => buttons[0]?.click());
  expect(api.rewindThread).not.toHaveBeenCalled();
  expect(container.textContent).not.toContain("确认回退到此检查点？");
  expect(buttons[1]?.disabled).toBe(false);
  await act(async () => buttons[1]?.click());
  await click("确认回退");
  expect(api.rewindThread).toHaveBeenCalledWith("session", "complete");
  expect(container.querySelector('[role="status"]')?.textContent).toContain("已回退到检查点");
});

it("线程已有模型时不依赖全局默认；压缩无操作给出准确反馈且不请求模型目录", async () => {
  await mount();
  await click("压缩上下文");
  expect(api.compactThread).toHaveBeenCalledWith("session", null);
  expect(api.models).not.toHaveBeenCalled();
  expect(container.querySelector('[role="status"]')?.textContent).toBe("消息不足，未执行压缩。");
});

it("面板打开后线程开始运行，点击回退时重新检查并阻止请求", async () => {
  await mount();
  await click("回退到此处");
  vi.mocked(api.snapshot).mockResolvedValueOnce({
    ...snapshot,
    status: "running",
    active_run: { run_id: "active", status: "running", started_at: "2026-10-11T00:00:00Z" },
  });
  await click("确认回退");
  expect(api.rewindThread).not.toHaveBeenCalled();
  expect(container.querySelector('[role="alert"]')?.textContent).toContain("当前不能回退或压缩");
});
