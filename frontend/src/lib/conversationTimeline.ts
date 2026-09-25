import type { ActiveRun, AgentMessage } from "../api/types";
import type { ActivityGroup, ActivityStatus, ActivityStep } from "../state/activity";

export interface TimelineMessageRow {
  kind: "message";
  key: string;
  message: AgentMessage;
}

export interface TimelineProcessItem {
  key: string;
  kind: "tool" | "model";
  callId: string | null;
  status: ActivityStatus;
  message: AgentMessage | null;
  activity: ActivityStep | null;
}

export interface TimelineProcessRow {
  kind: "process";
  key: string;
  items: TimelineProcessItem[];
  active: boolean;
  awaitingEvent: boolean;
}

export type ConversationTimelineRow = TimelineMessageRow | TimelineProcessRow;

interface TimelineInput {
  messages: AgentMessage[];
  groups: ActivityGroup[];
  activeRun?: ActiveRun | null;
}

function processKey(item: TimelineProcessItem): string {
  return `process:${item.key}`;
}

function toolItem(message: AgentMessage): TimelineProcessItem {
  const callId = message.tool_call_id || null;
  return {
    key: callId ? `tool:${callId}` : `tool-message:${message.id}`,
    kind: "tool",
    callId,
    status: message.status === "error" ? "failed" : "completed",
    message,
    activity: null,
  };
}

function activityItem(step: ActivityStep): TimelineProcessItem {
  const rawId = step.kind === "tool" ? step.data.tool_call_id : step.data.model_run_id;
  const callId = typeof rawId === "string" && rawId ? rawId : null;
  return {
    key: callId ? `${step.kind}:${callId}` : `${step.kind}-activity:${step.id}`,
    kind: step.kind === "tool" ? "tool" : "model",
    callId,
    status: step.status,
    message: null,
    activity: step,
  };
}

function finalStatus(status: ActivityStatus): boolean {
  return ["completed", "failed", "stopped", "paused", "unknown"].includes(status);
}

function mergeActivity(previous: ActivityStep, next: ActivityStep): ActivityStep {
  // 审计和实时流可能重复上报同一调用；后到的开始事件不能覆盖已确认的终态。
  const state = finalStatus(next.status) || !finalStatus(previous.status) ? next : previous;
  return {
    ...previous,
    type: state.type,
    status: state.status,
    at: previous.at ?? next.at,
    endedAt: next.endedAt ?? previous.endedAt,
    name: next.name ?? previous.name,
    summary: next.summary ?? previous.summary,
    durationMs: next.durationMs ?? previous.durationMs,
    data: { ...previous.data, ...next.data },
  };
}

function latestUserIndex(rows: ConversationTimelineRow[]): number {
  for (let index = rows.length - 1; index >= 0; index -= 1) {
    const row = rows[index];
    if (row?.kind === "message" && ["human", "user"].includes(row.message.role)) return index;
  }
  return -1;
}

/** 按检查点消息的原生顺序组织对话，不用可能缺失的时间戳重排历史。 */
export function projectConversationTimeline({
  messages,
  groups,
  activeRun,
}: TimelineInput): ConversationTimelineRow[] {
  const rows: ConversationTimelineRow[] = [];
  const toolItems = new Map<string, { row: TimelineProcessRow; item: TimelineProcessItem }>();
  let contiguous: TimelineProcessRow | null = null;

  for (const message of messages) {
    if (message.role === "system") continue;
    if (["ai", "assistant"].includes(message.role) && !message.text && message.has_tool_calls)
      continue;

    if (message.role === "tool") {
      const item = toolItem(message);
      const existing = toolItems.get(item.key);
      if (existing) {
        // 检查点重放同一调用时只更新结果，不增加第二条工具行。
        existing.item.message = message;
        existing.item.status = item.status;
        continue;
      }
      if (!contiguous) {
        contiguous = {
          kind: "process",
          key: processKey(item),
          items: [],
          active: false,
          awaitingEvent: false,
        };
        rows.push(contiguous);
      }
      contiguous.items.push(item);
      toolItems.set(item.key, { row: contiguous, item });
      continue;
    }

    contiguous = null;
    rows.push({ kind: "message", key: `message:${message.id}`, message });
  }

  const activityByKey = new Map<string, ActivityStep>();
  for (const group of groups) {
    for (const step of group.steps) {
      if (step.kind !== "tool" && step.kind !== "model") continue;
      const key = activityItem(step).key;
      const previous = activityByKey.get(key);
      activityByKey.set(key, previous ? mergeActivity(previous, step) : step);
    }
  }

  // 历史审计只补充耗时等元数据；工具结果与成败仍以检查点 ToolMessage 为准。
  for (const [key, result] of toolItems) {
    result.item.activity = activityByKey.get(key) ?? null;
  }

  if (!activeRun) return rows;

  const currentGroup = groups.at(-1);
  const currentKeys =
    currentGroup?.status === "running"
      ? new Set(
          currentGroup.steps
            .filter((step) => step.kind === "tool" || step.kind === "model")
            .map((step) => activityItem(step).key),
        )
      : new Set<string>();
  const steps = [...currentKeys]
    .map((key) => activityByKey.get(key))
    .filter((step): step is ActivityStep => Boolean(step));
  const userIndex = latestUserIndex(rows);
  const fromUser = !activeRun.source || activeRun.source === "user";
  const tail = rows.at(-1);
  const tailMatched =
    tail?.kind === "process" &&
    steps.some((step) => {
      if (step.kind !== "tool") return false;
      return toolItems.get(activityItem(step).key)?.row === tail;
    });
  const tailProcess =
    tail?.kind === "process" && (tailMatched || (fromUser && rows.length - 1 > userIndex))
      ? tail
      : null;
  let currentProcess: TimelineProcessRow | null = tailProcess;

  for (const step of steps) {
    const item = activityItem(step);
    const matched = item.kind === "tool" ? toolItems.get(item.key) : null;
    if (matched) {
      matched.item.activity = step;
      continue;
    }
    if (!currentProcess) {
      currentProcess = {
        kind: "process",
        key: processKey(item),
        items: [],
        active: true,
        awaitingEvent: false,
      };
      rows.push(currentProcess);
    }
    // 并发事件可重发；工具调用标识优先于事件序号去重。
    if (!currentProcess.items.some((entry) => entry.key === item.key)) {
      currentProcess.items.push(item);
    }
  }

  if (!currentProcess) {
    currentProcess = {
      kind: "process",
      key: `process:run:${activeRun.run_id}`,
      items: [],
      active: true,
      awaitingEvent: true,
    };
    rows.push(currentProcess);
  }
  currentProcess.active = true;
  currentProcess.awaitingEvent = !currentProcess.items.some((item) => item.status === "running");
  return rows;
}
