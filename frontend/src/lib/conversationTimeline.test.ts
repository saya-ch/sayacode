import { expect, it } from "vitest";
import type { ActiveRun, AgentMessage } from "../api/types";
import type { ActivityGroup, ActivityStep } from "../state/activity";
import { projectConversationTimeline, type TimelineProcessRow } from "./conversationTimeline";

const run: ActiveRun = {
  run_id: "run-1",
  started_at: "2026-09-25T00:00:00Z",
  status: "running",
};

function message(id: string, role: AgentMessage["role"], extra = {}): AgentMessage {
  return { id, role, text: id, ...extra };
}

function step(
  id: string,
  kind: "tool" | "model",
  status: ActivityStep["status"],
  callId: string,
): ActivityStep {
  return {
    id,
    kind,
    type: `${kind}.${status === "running" ? "started" : "completed"}`,
    status,
    at: "2026-09-25T00:00:01Z",
    endedAt: status === "running" ? null : "2026-09-25T00:00:02Z",
    name: kind === "tool" ? "read_file" : null,
    summary: null,
    durationMs: status === "running" ? null : 1000,
    data: { [kind === "tool" ? "tool_call_id" : "model_run_id"]: callId },
  };
}

function group(steps: ActivityStep[], status: ActivityGroup["status"] = "running"): ActivityGroup {
  return {
    id: "activity-run-1",
    number: 1,
    status,
    at: run.started_at,
    endedAt: status === "running" ? null : "2026-09-25T00:00:03Z",
    steps,
  };
}

function processRows(rows: ReturnType<typeof projectConversationTimeline>): TimelineProcessRow[] {
  return rows.filter((row): row is TimelineProcessRow => row.kind === "process");
}

it("依检查点顺序穿插消息与连续工具结果，不按缺失或倒序时间戳重排", () => {
  const rows = projectConversationTimeline({
    messages: [
      message("hidden", "system"),
      message("user-1", "human", { created_at: "2026-09-25T00:00:10Z" }),
      message("call", "ai", { text: "", has_tool_calls: true }),
      message("result-a", "tool", { tool_call_id: "a", created_at: null }),
      message("result-b", "tool", { tool_call_id: "b", created_at: "2026-09-25T00:00:01Z" }),
      message("answer", "ai", { created_at: null }),
      message("inbox", "agent_inbox"),
    ],
    groups: [],
  });
  expect(rows.map((row) => row.key)).toEqual([
    "message:user-1",
    "process:tool:a",
    "message:answer",
    "message:inbox",
  ]);
  expect(processRows(rows)[0]?.items.map((item) => item.key)).toEqual(["tool:a", "tool:b"]);
});

it("用调用标识把进行中的工具事件合到已有结果，不重复显示同一调用", () => {
  const rows = projectConversationTimeline({
    messages: [
      message("user", "human"),
      message("result-a", "tool", { tool_call_id: "a" }),
      message("result-a-replayed", "tool", { tool_call_id: "a" }),
      message("result-b", "tool", { tool_call_id: "b" }),
    ],
    groups: [
      group([step("start-a", "tool", "completed", "a"), step("start-b", "tool", "running", "b")]),
    ],
    activeRun: run,
  });
  const [process] = processRows(rows);
  expect(process?.items.map((item) => item.key)).toEqual(["tool:a", "tool:b"]);
  expect(process?.items[0]?.message?.id).toBe("result-a-replayed");
  expect(process?.items[1]?.activity?.status).toBe("running");
  expect(process?.items[1]?.status).toBe("completed");
  expect(process?.active).toBe(true);
  expect(process?.awaitingEvent).toBe(true);
});

it("工具结果尚未入检查点时在最新用户输入后插入临时过程行", () => {
  const input = [
    message("old-user", "human"),
    message("old-answer", "ai"),
    message("new-user", "human"),
  ];
  const active = projectConversationTimeline({
    messages: input,
    groups: [
      group([
        step("tool-start", "tool", "running", "call-1"),
        step("model-start", "model", "running", "model-1"),
      ]),
    ],
    activeRun: run,
  });
  expect(active.map((row) => row.key)).toEqual([
    "message:old-user",
    "message:old-answer",
    "message:new-user",
    "process:tool:call-1",
  ]);
  expect(processRows(active)[0]?.items.map((item) => item.key)).toEqual([
    "tool:call-1",
    "model:model-1",
  ]);
  const withResult = projectConversationTimeline({
    messages: [...input, message("result", "tool", { tool_call_id: "call-1" })],
    groups: [group([step("tool-start", "tool", "completed", "call-1")])],
    activeRun: run,
  });
  expect(processRows(withResult)).toHaveLength(1);
  expect(processRows(withResult)[0]?.key).toBe("process:tool:call-1");
  expect(processRows(withResult)[0]?.items[0]?.message?.id).toBe("result");
});

it("重连后仅有 active_run 时显示等待事件；终态仍保留历史工具过程", () => {
  const messages = [message("user", "human"), message("answer", "ai")];
  const running = projectConversationTimeline({ messages, groups: [], activeRun: run });
  expect(running.map((row) => row.key)).toEqual([
    "message:user",
    "message:answer",
    "process:run:run-1",
  ]);
  expect(processRows(running)[0]).toMatchObject({ items: [], active: true, awaitingEvent: true });

  const settled = projectConversationTimeline({
    messages: [
      message("user", "human"),
      message("result", "tool", { tool_call_id: "call-1" }),
      message("answer", "ai"),
    ],
    groups: [group([step("tool-start", "tool", "completed", "call-1")], "completed")],
    activeRun: null,
  });
  expect(processRows(settled)).toHaveLength(1);
  expect(processRows(settled)[0]).toMatchObject({ active: false, awaitingEvent: false });
  expect(processRows(settled)[0]?.items[0]?.key).toBe("tool:call-1");
  expect(processRows(settled)[0]?.items[0]?.activity?.durationMs).toBe(1000);
  expect(processRows(settled)[0]?.items[0]?.status).toBe("completed");
});

it("非用户来源的续跑把新过程放在旧回答后，已有工具调用仍按标识归并", () => {
  const messages = [message("user", "human"), message("answer", "ai")];
  const resumed = projectConversationTimeline({
    messages,
    groups: [group([step("new-tool", "tool", "running", "call-new")])],
    activeRun: { ...run, source: "resume" },
  });
  expect(resumed.map((row) => row.key)).toEqual([
    "message:user",
    "message:answer",
    "process:tool:call-new",
  ]);

  const withMatchedResult = projectConversationTimeline({
    messages: [
      message("user", "human"),
      message("result", "tool", { tool_call_id: "call-new" }),
      message("answer", "ai"),
    ],
    groups: [group([step("new-tool", "tool", "completed", "call-new")])],
    activeRun: { ...run, source: "approval" },
  });
  expect(processRows(withMatchedResult)).toHaveLength(2);
  expect(processRows(withMatchedResult)[0]?.items[0]?.message?.id).toBe("result");
  expect(processRows(withMatchedResult)[0]?.active).toBe(false);
  expect(processRows(withMatchedResult)[1]).toMatchObject({
    active: true,
    awaitingEvent: true,
    items: [],
  });
});

it("并发工具结果逆序到达时，活动与结果仍按各自调用标识配对", () => {
  const rows = projectConversationTimeline({
    messages: [message("user", "human"), message("result-b", "tool", { tool_call_id: "b" })],
    groups: [
      group([step("start-a", "tool", "running", "a"), step("start-b", "tool", "completed", "b")]),
    ],
    activeRun: run,
  });
  const [process] = processRows(rows);
  expect(processRows(rows)).toHaveLength(1);
  expect(process?.items.map((item) => item.key)).toEqual(["tool:b", "tool:a"]);
  expect(process?.items[0]?.message?.id).toBe("result-b");
  expect(process?.items[1]?.status).toBe("running");
});

it("审计与实时流重复同一调用时合并详情，并以完成或失败终态为准", () => {
  const modelStarted = {
    ...step("audit-model-start", "model", "running", "model-1"),
    data: { model_run_id: "model-1", request: "开始" },
  };
  const modelFinished = {
    ...step("live-model-end", "model", "completed", "model-1"),
    data: { model_run_id: "model-1", usage: 120 },
  };
  const toolStarted = step("audit-tool-start", "tool", "running", "tool-1");
  const toolFailed = {
    ...step("live-tool-end", "tool", "failed", "tool-1"),
    data: { tool_call_id: "tool-1", error: "执行失败" },
  };
  const rows = projectConversationTimeline({
    messages: [message("user", "human")],
    groups: [
      group([modelStarted, toolStarted]),
      group([modelFinished, toolFailed, modelStarted, toolStarted]),
    ],
    activeRun: run,
  });
  const [process] = processRows(rows);
  expect(processRows(rows)).toHaveLength(1);
  expect(process?.items.map((item) => [item.key, item.status])).toEqual([
    ["model:model-1", "completed"],
    ["tool:tool-1", "failed"],
  ]);
  expect(process?.items[0]?.activity?.data).toMatchObject({ request: "开始", usage: 120 });
  expect(process?.items[0]?.activity?.durationMs).toBe(1000);
  expect(process?.items[1]?.activity?.data.error).toBe("执行失败");
  expect(process?.awaitingEvent).toBe(true);
});

it("旧工具结果后已有正文回复时，后续新工具过程只出现在对话末尾", () => {
  const rows = projectConversationTimeline({
    messages: [
      message("user", "human"),
      message("result-a", "tool", { tool_call_id: "a" }),
      message("answer-a", "ai"),
    ],
    groups: [
      group([step("done-a", "tool", "completed", "a"), step("start-b", "tool", "running", "b")]),
    ],
    activeRun: run,
  });
  expect(rows.map((row) => row.key)).toEqual([
    "message:user",
    "process:tool:a",
    "message:answer-a",
    "process:tool:b",
  ]);
  const processes = processRows(rows);
  expect(processes[0]?.active).toBe(false);
  expect(processes[0]?.items[0]?.activity?.durationMs).toBe(1000);
  expect(processes[1]?.active).toBe(true);
  expect(processes[1]?.items.map((item) => item.key)).toEqual(["tool:b"]);
});
