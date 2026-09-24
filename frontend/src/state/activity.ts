import type { Activity, StreamEvent, ThreadSnapshot } from "../api/types";

export type ActivityKind = "tool" | "model" | "approval" | "task" | "other";
export type ActivityStatus =
  "pending" | "running" | "completed" | "failed" | "paused" | "stopped" | "unknown";

export interface ActivityStep {
  id: string;
  type: string;
  kind: ActivityKind;
  status: ActivityStatus;
  at: string | null;
  endedAt: string | null;
  name: string | null;
  summary: string | null;
  durationMs: number | null;
  data: Record<string, unknown>;
}

export interface ActivityGroup {
  id: string;
  number: number;
  status: ActivityStatus;
  at: string | null;
  endedAt: string | null;
  steps: ActivityStep[];
}

interface ActivityEvent {
  id: string;
  type: string;
  at: string | null;
  summary: string | null;
  toolName: string | null;
  durationMs: number | null;
  data: Record<string, unknown>;
  correlationId: string | null;
}

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function string(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function kindOf(type: string): ActivityKind {
  if (type.startsWith("tool.")) return "tool";
  if (type.startsWith("model.")) return "model";
  if (type.startsWith("approval.") || type.startsWith("review.")) return "approval";
  if (type.startsWith("task.")) return "task";
  return "other";
}

function statusOf(type: string, data: Record<string, unknown> = {}): ActivityStatus {
  const taskStatus =
    type === "task.updated" ? string(data.status) : type.startsWith("task.") ? type.slice(5) : null;
  if (taskStatus) {
    if (taskStatus === "pending" || taskStatus === "idle") return "pending";
    if (taskStatus === "running" || taskStatus === "stopping") return "running";
    if (taskStatus === "paused") return "paused";
    if (taskStatus === "completed") return "completed";
    if (taskStatus === "failed" || taskStatus === "error") return "failed";
    if (taskStatus === "stopped" || taskStatus === "interrupted") return "stopped";
  }
  if (type === "run.stopping") return "running";
  if (type === "hook" && data.blocked === true) return "failed";
  if (type.endsWith(".failed") || type.endsWith(".error")) return "failed";
  if (type.endsWith(".completed")) return "completed";
  if (type.endsWith(".paused") || type.endsWith(".requested")) return "paused";
  if (type.endsWith(".stopped") || type.endsWith(".interrupted")) return "stopped";
  if (type.endsWith(".started")) return "running";
  return "completed";
}

function snapshotEvent(row: Activity): ActivityEvent {
  const data = object(row.data);
  // 审计 run_id 是 LangChain 回调的调用标识，和网页运行标识不混用。
  const callbackId =
    string((row as Activity & { run_id?: string | null }).run_id) ?? string(data.run_id);
  return {
    id: row.id,
    type: row.type,
    at: row.at ?? null,
    summary: row.summary && row.summary !== row.type ? row.summary : null,
    toolName: row.tool_name ?? string(data.tool_name) ?? string(data.name),
    durationMs: row.duration_ms ?? (typeof data.duration_ms === "number" ? data.duration_ms : null),
    data,
    correlationId: callbackId ? `audit:${callbackId}` : null,
  };
}

function liveEvent(row: StreamEvent): ActivityEvent {
  const data = object(row.data);
  const callId = row.type.startsWith("tool.")
    ? string(data.tool_call_id)
    : row.type.startsWith("model.")
      ? string(data.model_run_id)
      : null;
  return {
    id: `live-${row.instance_id}-${row.seq}`,
    type: row.type,
    at: row.at ?? null,
    summary: string(data.summary) ?? (row.type.startsWith("task.") ? string(data.title) : null),
    toolName: string(data.tool_name) ?? string(data.name),
    durationMs: typeof data.duration_ms === "number" ? data.duration_ms : null,
    data,
    correlationId: callId ? `live:${callId}` : null,
  };
}

function asStep(event: ActivityEvent): ActivityStep {
  return {
    id: event.id,
    type: event.type,
    kind: kindOf(event.type),
    status: statusOf(event.type, event.data),
    at: event.at,
    endedAt: null,
    name: event.toolName,
    summary: event.summary,
    durationMs: event.durationMs,
    data: event.data,
  };
}

/** 只整理可见事件；消息正文和待办仍由原生图快照提供。 */
export function projectActivity(activity: Activity[], liveEvents: StreamEvent[]): ActivityGroup[] {
  const visible = (type: string) =>
    type !== "assistant.delta" &&
    type !== "message.user" &&
    type !== "run.progress" &&
    !type.startsWith("stream.");
  const events = [
    ...activity.filter((event) => visible(event.type)).map(snapshotEvent),
    ...liveEvents.filter((event) => visible(event.type)).map(liveEvent),
  ];
  const groups: ActivityGroup[] = [];
  const pending = new Map<string, ActivityStep>();
  let current: ActivityGroup | null = null;

  function group(at: string | null): ActivityGroup {
    if (current) return current;
    current = {
      id: `activity-run-${groups.length + 1}`,
      number: groups.length + 1,
      status: "running",
      at,
      endedAt: null,
      steps: [],
    };
    groups.push(current);
    return current;
  }

  for (const event of events) {
    if (event.type === "run.started") {
      if (groups.at(-1)?.status === "running" && groups.at(-1)?.steps.length) {
        for (const step of pending.values()) step.status = "unknown";
        current = null;
      }
      group(event.at);
      pending.clear();
      continue;
    }
    if (event.type === "run.stopping") {
      // 停止请求并非结算；等待工具或模型回报及真正终态。
      group(event.at).status = "running";
      continue;
    }
    if (event.type.startsWith("run.") && event.type !== "run.progress") {
      const run = group(event.at);
      run.status = statusOf(event.type, event.data);
      run.endedAt = event.at;
      for (const step of pending.values()) step.status = "unknown";
      current = null;
      pending.clear();
      continue;
    }

    const run = group(event.at);
    const kind = kindOf(event.type);
    const key = kind === "tool" || kind === "model" ? event.correlationId : null;
    const started = key ? pending.get(key) : undefined;
    if (started && event.type.endsWith(".started")) {
      // 同一调用重发开始事件时保留第一条，不制造重复活动。
      continue;
    }
    if (started && !event.type.endsWith(".started")) {
      started.type = event.type;
      started.status = statusOf(event.type, event.data);
      started.endedAt = event.at;
      started.name = started.name ?? event.toolName;
      started.summary = event.summary ?? started.summary;
      started.durationMs = event.durationMs ?? started.durationMs;
      started.data = { ...started.data, ...event.data };
      pending.delete(key!);
      continue;
    }
    const step = asStep(event);
    run.steps.push(step);
    if (key && event.type.endsWith(".started")) pending.set(key, step);
  }
  return groups;
}

export function activityTarget(step: ActivityStep): string | null {
  const persistedTarget = string(step.data.target);
  if (persistedTarget) return persistedTarget.replace(/\s+/g, " ").trim();
  const inputText = string(step.data.tool_input);
  if (inputText) return inputText.replace(/\s+/g, " ").trim();
  const input = object(step.data.tool_input);
  for (const key of [
    "path",
    "file_path",
    "target_path",
    "command",
    "pattern",
    "query",
    "url",
    "directory",
  ]) {
    const value = string(input[key]);
    if (value) return value.replace(/\s+/g, " ").trim();
  }
  return step.summary;
}

export function reconcileActivity(
  previous: ThreadSnapshot | null,
  next: ThreadSnapshot,
  live: StreamEvent[],
  forceSnapshot = false,
): { snapshot: ThreadSnapshot; live: StreamEvent[] } {
  const running = next.status === "running" || next.status === "stopping";
  // 订阅后的即时事件继续展示；同一轮运行中的审计刷新不重新插入同一事实。
  const snapshot =
    running && !forceSnapshot && previous?.thread_id === next.thread_id
      ? { ...next, activity: previous.activity }
      : next;
  // 终态和重同步以审计为准；审批、任务通知等未进入审计的事件仍留在本页。
  const events =
    running && !forceSnapshot
      ? live
      : live.filter(
          (event) => !["model.", "tool.", "run."].some((prefix) => event.type.startsWith(prefix)),
        );
  return { snapshot, live: events };
}
