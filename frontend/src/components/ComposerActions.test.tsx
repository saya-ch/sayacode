// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { Skill, ThreadSnapshot } from "../api/types";
import type { WorkspaceState } from "../state/useWorkspace";
import { ComposerActions } from "./ComposerActions";
import { SkillsPanel } from "../products/skills";

const skill = (name: string): Skill => ({
  name,
  description: "代码审查",
  source: "project",
  path: name,
});
const snapshot = (threadId = "thread", activeSkills: string[] = []): ThreadSnapshot => ({
  thread_id: threadId,
  workspace_id: "workspace",
  title: threadId,
  status: "idle",
  effective_model: "test",
  messages: [],
  active_skills: activeSkills,
  todos: [],
  tasks: [],
});
function state(threadId = "thread", activeSkills: string[] = []): WorkspaceState {
  return {
    workspaceId: "workspace",
    threadId,
    snapshot: snapshot(threadId, activeSkills),
    busy: false,
    settings: null,
    contextFeedback: null,
    activateSkill: vi.fn().mockResolvedValue(undefined),
    deactivateSkill: vi.fn().mockResolvedValue(undefined),
    compactCurrent: vi.fn().mockResolvedValue(undefined),
  } as unknown as WorkspaceState;
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((finish) => {
    resolve = finish;
  });
  return { promise, resolve };
}
let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) =>
    setTimeout(callback, 0),
  );
  vi.spyOn(api, "models").mockResolvedValue({ profiles: [], active_profile: null });
  vi.spyOn(api, "skills").mockResolvedValue([skill("code-review")]);
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
async function render(current: WorkspaceState, settings = false) {
  await act(async () =>
    root.render(
      settings ? (
        <SkillsPanel state={current} />
      ) : (
        <ComposerActions
          state={current}
          attachments={[]}
          onAddFiles={async () => {}}
          onRemoveAttachment={async () => {}}
          onOpenSettings={() => {}}
        />
      ),
    ),
  );
}
async function click(text: string) {
  const button = [...container.querySelectorAll<HTMLButtonElement>("button")].find(
    (item) => item.textContent?.includes(text) || item.getAttribute("aria-label") === text,
  );
  expect(button, text).toBeDefined();
  await act(async () => button?.click());
}

it("选择 Skill 走统一线程操作，成功后显示持续标签和提示，标签支持解除", async () => {
  const current = state();
  await render(current);
  await click("添加操作");
  await click("选择 Skill");
  await click("code-review");
  expect(current.activateSkill).toHaveBeenCalledWith("code-review");
  expect(api.skills).toHaveBeenCalledWith("workspace", "thread");
  expect(container.querySelector('[role="dialog"]')).toBeNull();

  await render({
    ...current,
    snapshot: snapshot("thread", ["code-review"]),
    contextFeedback: { thread_id: "thread", action: "skill_activated", name: "code-review" },
  });
  expect(container.querySelector('[aria-label="当前线程已激活的 Skill"]')?.textContent).toContain(
    "code-review",
  );
  expect(container.querySelector('[role="status"]')?.textContent).toBe("已激活 Skill：code-review");
  await click("解除 Skill：code-review");
  expect(current.deactivateSkill).toHaveBeenCalledWith("code-review");
});

it("切换线程后旧目录迟到不能覆盖新线程的 Skill 目录", async () => {
  const old = deferred<Skill[]>();
  const next = deferred<Skill[]>();
  vi.mocked(api.skills).mockReturnValueOnce(old.promise).mockReturnValueOnce(next.promise);
  await render(state("old"));
  await click("添加操作");
  await click("选择 Skill");
  await render(state("new"));
  await click("添加操作");
  await click("选择 Skill");
  await act(async () => next.resolve([skill("new-skill")]));
  await act(async () => old.resolve([skill("old-skill")]));
  expect(container.textContent).toContain("new-skill");
  expect(container.textContent).not.toContain("old-skill");
  expect(api.skills).toHaveBeenLastCalledWith("workspace", "new");
});

it("设置页从检查点名称显示激活并调用相同解除操作，线程忙时两个入口都禁止更改", async () => {
  const current = state("thread", ["code-review"]);
  await render(current, true);
  expect(container.textContent).toContain("已激活");
  await click("解除激活");
  expect(current.deactivateSkill).toHaveBeenCalledWith("code-review");
  const busy = { ...current, snapshot: { ...current.snapshot!, pending_steps: true } };
  await render(busy, true);
  expect(container.querySelector('button[aria-pressed="true"]')?.hasAttribute("disabled")).toBe(
    true,
  );
  await render(busy);
  expect(
    container.querySelector('[aria-label="解除 Skill：code-review"]')?.hasAttribute("disabled"),
  ).toBe(true);
  await click("添加操作");
  const select = [...container.querySelectorAll("button")].find((item) =>
    item.textContent?.includes("选择 Skill"),
  );
  expect(select?.disabled).toBe(true);
});
