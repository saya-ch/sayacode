import { expect, it } from "vitest";
import type { StreamEvent, ThreadSnapshot } from "../api/types";
import { applyThreadSnapshot, refreshPlan } from "./eventPolicy";

const frame = (type: string, data: StreamEvent["data"] = {}): StreamEvent => ({
  instance_id: "i",
  seq: 1,
  workspace_id: "w",
  type,
  data,
});

it("高频模型与工具进度不重复拉取完整长对话", () => {
  expect(refreshPlan(frame("model.started"))).toBe("none");
  expect(refreshPlan(frame("tool.started"))).toBe("none");
  expect(refreshPlan(frame("tool.completed", { tool_name: "read_file" }))).toBe("none");
  expect(refreshPlan(frame("assistant.delta", { delta: "x" }))).toBe("none");
  expect(refreshPlan(frame("tool.completed", { tool_name: "write_todos" }))).toBe("todos");
  expect(refreshPlan(frame("task.running"))).toBe("tasks");
  expect(refreshPlan(frame("run.completed"))).toBe("full");
  expect(refreshPlan(frame("approval.requested"))).toBe("full");
  expect(refreshPlan(frame("thread.compacted"))).toBe("full");
  expect(refreshPlan(frame("thread.rewound"))).toBe("full");
  expect(refreshPlan(frame("thread.resumed"))).toBe("full");
});

it("当前子 Agent 每轮开始和结算都同步对话，其他任务只更新任务列表", () => {
  for (const type of ["task.running", "task.idle", "task.failed", "task.paused"]) {
    const event = { ...frame(type), thread_id: "child" };
    expect(refreshPlan(event, "child", "root")).toBe("full");
    expect(refreshPlan(event, "root", "root")).toBe("tasks");
    expect(refreshPlan(event, "other-child", "root")).toBe("tasks");
  }
});

it("切换到 B 后，A 的迟到请求不能覆盖 B，但仍能更新 A 的父会话快照", () => {
  const snapshot = (threadId: string, title: string): ThreadSnapshot => ({
    thread_id: threadId,
    workspace_id: "w",
    title,
    status: "idle",
    messages: [],
    todos: [],
    tasks: [],
  });
  const selectedB = snapshot("B", "当前子线程");
  const oldParentA = snapshot("A", "旧父会话");
  const lateA = snapshot("A", "新父会话");

  expect(applyThreadSnapshot(selectedB, "B", "A", lateA)).toBe(selectedB);
  expect(applyThreadSnapshot(oldParentA, "A", "A", lateA)).toBe(lateA);
  expect(applyThreadSnapshot(oldParentA, "A", "A", selectedB)).toBe(oldParentA);
});
