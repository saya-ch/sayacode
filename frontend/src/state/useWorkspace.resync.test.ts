import { expect, it } from "vitest";
import type { StreamEvent, ThreadSnapshot } from "../api/types";
import { reconcileResyncedEvents, replayResyncedRuns } from "./useWorkspace";

function frame(seq: number, type: string, data: Record<string, unknown> = {}): StreamEvent {
  return {
    instance_id: "instance-1",
    seq,
    workspace_id: "workspace",
    thread_id: "thread",
    type,
    data,
  };
}

it("重同步快照读取期间到达的新工具事件保留，已入审计的调用不重复", () => {
  const snapshot: ThreadSnapshot = {
    thread_id: "thread",
    workspace_id: "workspace",
    title: "SAYA",
    status: "running",
    messages: [],
    todos: [],
    tasks: [],
    activity: [
      { id: "audit-model", type: "model.started", run_id: "model-1" },
      { id: "audit-tool", type: "tool.completed", data: { tool_call_id: "old-tool" } },
    ],
  };
  const events = [
    frame(9, "model.started", { model_run_id: "old-model" }),
    frame(11, "model.started", { model_run_id: "model-1" }),
    frame(12, "tool.completed", { tool_call_id: "old-tool" }),
    // 快照已经读完审计、请求尚未返回时到达；不能被强制交接清除。
    frame(13, "tool.started", { tool_call_id: "new-tool" }),
    frame(14, "model.started", { model_run_id: "model-2" }),
    frame(15, "task.running", { title: "reviewer" }),
  ];

  expect(
    reconcileResyncedEvents(snapshot, events, { instanceId: "instance-1", seq: 10 }).map(
      (event) => event.seq,
    ),
  ).toEqual([13, 14, 15]);
});

it("只有相同生产者且高于交接水位的运行事件进入新实时段", () => {
  const snapshot: ThreadSnapshot = {
    thread_id: "thread",
    workspace_id: "workspace",
    title: "SAYA",
    // 旧轮次结束的快照先返回，窗口内可能已经开始新轮次。
    status: "completed",
    messages: [],
    todos: [],
    tasks: [],
    activity: [],
  };
  const oldInstance = {
    ...frame(20, "tool.started", { tool_call_id: "other" }),
    instance_id: "old",
  };
  const current = { ...frame(21, "run.started", { source: "resume" }), run_id: "new-run" };
  const newTool = frame(22, "tool.started", { tool_call_id: "new-tool" });

  expect(
    reconcileResyncedEvents(snapshot, [oldInstance, current, newTool], {
      instanceId: "instance-1",
      seq: 20,
    }),
  ).toEqual([current, newTool]);
  expect(
    replayResyncedRuns(snapshot, [current, newTool], {
      instanceId: "instance-1",
      seq: 20,
    }),
  ).toMatchObject({ status: "running", active_run: { run_id: current.run_id } });
});
