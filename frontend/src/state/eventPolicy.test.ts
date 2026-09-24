import { expect, it } from "vitest";
import type { StreamEvent } from "../api/types";
import { refreshPlan } from "./eventPolicy";

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
});
