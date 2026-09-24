import { expect, it } from "vitest";
import type { StreamEvent, Task, ThreadSnapshot } from "../api/types";
import { activityTarget, projectActivity, reconcileActivity } from "./activity";
import { applyRunEvent, belongsToSelectedTimeline } from "./eventPolicy";

const thread = (
  status: ThreadSnapshot["status"],
  activity: ThreadSnapshot["activity"],
): ThreadSnapshot => ({
  thread_id: "t",
  workspace_id: "w",
  title: "SAYA",
  status,
  messages: [],
  todos: [],
  tasks: [],
  activity,
});
const event = (type: string): StreamEvent => ({
  instance_id: "i",
  seq: 1,
  workspace_id: "w",
  thread_id: "t",
  type,
  data: {},
});

it("运行中保留切入时审计历史和当前实时事件，终态交给审计去重", () => {
  const previous = thread("running", [{ id: "older", type: "model.completed" }]);
  const live = [
    event("model.started"),
    event("tool.completed"),
    event("run.completed"),
    event("approval.requested"),
  ];
  const running = reconcileActivity(
    previous,
    thread("running", [
      { id: "older", type: "model.completed" },
      { id: "duplicate", type: "model.started" },
    ]),
    live,
  );
  expect(running.snapshot.activity?.map((row) => row.id)).toEqual(["older"]);
  expect(running.live).toEqual(live);
  const complete = reconcileActivity(
    running.snapshot,
    thread("completed", [
      { id: "older", type: "model.completed" },
      { id: "settled", type: "tool.completed" },
    ]),
    running.live,
  );
  expect(complete.snapshot.activity?.map((row) => row.id)).toEqual(["older", "settled"]);
  expect(complete.live.map((row) => row.type)).toEqual(["approval.requested"]);
});

it("缓冲缺口强制交接快照，再接收新实时事件", () => {
  const result = reconcileActivity(
    thread("running", []),
    thread("running", [{ id: "recovered", type: "tool.completed" }]),
    [event("tool.completed")],
    true,
  );
  expect(result.snapshot.activity?.[0]?.id).toBe("recovered");
  expect(result.live).toEqual([]);
});

it("按调用标识配对并发工具，合并起止详情且保持原顺序", () => {
  const groups = projectActivity(
    [
      {
        id: "start-a",
        type: "tool.started",
        run_id: "a",
        data: { name: "read_file", tool_input: { path: "a.py" } },
      },
      {
        id: "start-b",
        type: "tool.started",
        run_id: "b",
        data: { name: "read_file", tool_input: { path: "b.py" } },
      },
      {
        id: "end-b",
        type: "tool.completed",
        run_id: "b",
        data: { tool_output: "B", duration_ms: 240 },
      },
      {
        id: "end-a",
        type: "tool.failed",
        run_id: "a",
        data: { error_type: "OSError", duration_ms: 280 },
      },
      { id: "end-run", type: "run.failed" },
    ] as ThreadSnapshot["activity"] & Array<{ run_id?: string }>,
    [],
  );
  expect(groups).toHaveLength(1);
  expect(groups[0]?.status).toBe("failed");
  expect(groups[0]?.steps.map((step) => [step.id, step.status])).toEqual([
    ["start-a", "failed"],
    ["start-b", "completed"],
  ]);
  expect(groups[0]?.steps[1]?.data).toMatchObject({
    tool_input: { path: "b.py" },
    tool_output: "B",
  });
});

it("实时工具调用只占一行，下一轮开始会分组", () => {
  const frame = (seq: number, type: string, data: Record<string, unknown> = {}): StreamEvent => ({
    ...event(type),
    seq,
    run_id: "web-run",
    data,
  });
  const groups = projectActivity(
    [],
    [
      frame(1, "run.started"),
      frame(2, "tool.started", {
        tool_call_id: "call-1",
        tool_name: "write_file",
        tool_input: { path: "a.py" },
      }),
      frame(3, "tool.completed", { tool_call_id: "call-1", tool_output: "ok" }),
      frame(4, "run.completed"),
      frame(5, "run.started"),
      frame(6, "model.started", { model_run_id: "model-2" }),
    ],
  );
  expect(groups).toHaveLength(2);
  expect(groups[0]?.steps).toHaveLength(1);
  expect(groups[0]?.steps[0]?.data.tool_output).toBe("ok");
  expect(groups[1]?.steps[0]?.status).toBe("running");
});

it("缺少调用标识时不按工具名猜测并发配对", () => {
  const groups = projectActivity(
    [
      { id: "a", type: "tool.started", tool_name: "read_file" },
      { id: "b", type: "tool.started", tool_name: "read_file" },
      { id: "c", type: "tool.completed", tool_name: "read_file" },
    ],
    [],
  );
  expect(groups[0]?.steps.map((step) => step.id)).toEqual(["a", "b", "c"]);
});

it("运行结束却没有工具结果时明确保留未确认状态", () => {
  const groups = projectActivity(
    [
      { id: "start", type: "tool.started", run_id: "one", data: { name: "execute_command" } },
      { id: "end", type: "run.stopped" },
    ] as ThreadSnapshot["activity"] & Array<{ run_id?: string }>,
    [],
  );
  expect(groups[0]?.status).toBe("stopped");
  expect(groups[0]?.steps[0]?.status).toBe("unknown");
});

it("请求停止期间保留未结算工具和运行时间，直到真正终态", () => {
  const frame = (seq: number, type: string, data: Record<string, unknown> = {}): StreamEvent => ({
    ...event(type),
    seq,
    at: new Date(seq * 1000).toISOString(),
    data,
  });
  const pending = [
    frame(1, "run.started"),
    frame(2, "tool.started", { tool_call_id: "call", tool_name: "read_file" }),
    frame(3, "run.stopping"),
  ];
  const stopping = projectActivity([], pending);
  expect(stopping).toHaveLength(1);
  expect(stopping[0]?.status).toBe("running");
  expect(stopping[0]?.endedAt).toBeNull();
  expect(stopping[0]?.steps[0]?.status).toBe("running");
  const settled = projectActivity(
    [],
    [
      ...pending,
      frame(4, "tool.completed", { tool_call_id: "call", tool_output: "ok" }),
      frame(5, "run.stopped"),
    ],
  );
  expect(settled).toHaveLength(1);
  expect(settled[0]?.status).toBe("stopped");
  expect(settled[0]?.steps[0]?.status).toBe("completed");
  expect(settled[0]?.steps[0]?.data.tool_output).toBe("ok");
  expect(settled[0]?.endedAt).toBe(frame(5, "run.stopped").at);
  const reconciled = reconcileActivity(thread("running", []), thread("stopping", []), [
    frame(2, "tool.started", { tool_call_id: "call" }),
  ]);
  expect(reconciled.live).toHaveLength(1);
});

it("子任务运行与空闲事件不被误判为已完成，并保留任务标题", () => {
  const running = { ...event("task.running"), seq: 7, data: { title: "后端检查" } };
  const idle = { ...event("task.idle"), seq: 8, data: { title: "后端检查" } };
  const groups = projectActivity([], [running, idle]);
  expect(groups[0]?.steps.map((step) => step.status)).toEqual(["running", "pending"]);
  expect(groups[0]?.steps.map(activityTarget)).toEqual(["后端检查", "后端检查"]);
});

it("主线程事件可独立更新主快照，停止中保留运行句柄", () => {
  const root = { ...thread("idle", []), thread_id: "root" };
  const started = applyRunEvent(root, {
    ...event("run.started"),
    thread_id: "root",
    run_id: "run-1",
    at: "2026-09-25T00:00:00Z",
  });
  expect(started?.status).toBe("running");
  expect(started?.active_run?.run_id).toBe("run-1");
  const stopping = applyRunEvent(started, { ...event("run.stopping"), thread_id: "root" });
  expect(stopping?.status).toBe("stopping");
  expect(stopping?.active_run?.status).toBe("stopping");
  const childEvent = applyRunEvent(stopping, { ...event("run.failed"), thread_id: "child" });
  expect(childEvent).toBe(stopping);
  const stopped = applyRunEvent(stopping, { ...event("run.stopped"), thread_id: "root" });
  expect(stopped?.status).toBe("stopped");
  expect(stopped?.active_run).toBeNull();
});

it("主时间线只收同一会话树的子任务状态，不混入子任务工具流", () => {
  const task = (id: string, parent: string): Task => ({
    id,
    thread_id: id,
    parent_thread_id: parent,
    title: id,
    role: "builder",
    status: "running",
    workspace_id: "w",
    created_at: null,
    updated_at: null,
  });
  const tasks = [task("child", "root"), task("grandchild", "child"), task("other", "elsewhere")];
  const childStatus = { ...event("task.running"), task_id: "grandchild", thread_id: "grandchild" };
  expect(belongsToSelectedTimeline(childStatus, "root", "root", tasks)).toBe(true);
  expect(
    belongsToSelectedTimeline(
      { ...event("tool.started"), task_id: "grandchild", thread_id: "grandchild" },
      "root",
      "root",
      tasks,
    ),
  ).toBe(false);
  expect(
    belongsToSelectedTimeline(
      { ...event("task.running"), task_id: "other", thread_id: "other" },
      "root",
      "root",
      tasks,
    ),
  ).toBe(false);
  expect(belongsToSelectedTimeline(childStatus, "grandchild", "root", tasks)).toBe(true);
});
