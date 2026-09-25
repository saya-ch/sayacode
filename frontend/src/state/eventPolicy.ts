import type { StreamEvent, Task, ThreadSnapshot } from "../api/types";
import { descendantTasks } from "../lib/tasks";

export type RefreshPlan = "full" | "tasks" | "todos" | "none";

/** 主线程只额外接收本会话树的子任务状态，不混入子任务的模型与工具流。 */
export function belongsToSelectedTimeline(
  event: StreamEvent,
  selected: string | null,
  root: string | null,
  tasks: Task[],
): boolean {
  if (!selected) return false;
  if (event.thread_id === selected) return true;
  if (
    event.task_id &&
    tasks.some((task) => task.id === event.task_id && task.thread_id === selected)
  )
    return true;
  return (
    selected === root &&
    event.type.startsWith("task.") &&
    descendantTasks(tasks, root).some(
      (task) => task.id === event.task_id || task.thread_id === event.thread_id,
    )
  );
}

/** 同一条运行事件更新选中线程与主线程快照，避免两个视图状态分叉。 */
export function applyRunEvent(
  snapshot: ThreadSnapshot | null,
  event: StreamEvent,
): ThreadSnapshot | null {
  if (!snapshot || snapshot.thread_id !== event.thread_id) return snapshot;
  if (event.type === "run.started" && event.run_id) {
    return {
      ...snapshot,
      status: "running",
      active_run: {
        run_id: event.run_id,
        started_at:
          snapshot.active_run?.run_id === event.run_id
            ? snapshot.active_run.started_at
            : (event.at ?? new Date().toISOString()),
        status: "running",
        source:
          typeof event.data.source === "string"
            ? event.data.source
            : snapshot.active_run?.run_id === event.run_id
              ? snapshot.active_run.source
              : null,
      },
    };
  }
  if (event.type === "run.stopping") {
    return {
      ...snapshot,
      status: "stopping",
      active_run: snapshot.active_run ? { ...snapshot.active_run, status: "stopping" } : null,
    };
  }
  if (["run.completed", "run.failed", "run.paused", "run.stopped"].includes(event.type)) {
    return {
      ...snapshot,
      status: event.type.slice(4) as ThreadSnapshot["status"],
      active_run: null,
    };
  }
  return snapshot;
}

/** 异步请求结束后只允许更新它所属且仍被选中的线程。 */
export function applyThreadSnapshot(
  current: ThreadSnapshot | null,
  selectedThreadId: string | null,
  requestedThreadId: string,
  result: ThreadSnapshot,
): ThreadSnapshot | null {
  return selectedThreadId === requestedThreadId && result.thread_id === requestedThreadId
    ? result
    : current;
}

export function refreshPlan(
  event: StreamEvent,
  selectedThreadId: string | null = null,
  rootThreadId: string | null = null,
): RefreshPlan {
  if (
    [
      "run.completed",
      "run.failed",
      "run.paused",
      "run.stopped",
      "run.stopping",
      "message.queued",
      "message.steered",
      "message.delivered",
      "message.queue_updated",
      "message.queue_removed",
      "session.deleted",
      "approval.requested",
      "thread.compacted",
      "thread.rewound",
      "thread.resumed",
    ].includes(event.type)
  )
    return "full";
  if (event.type.startsWith("agent.wake.") && !event.type.endsWith(".started")) return "full";
  if (event.type.startsWith("task.")) {
    // 子 Agent 的轮次由任务事件结算；它没有主会话使用的 run.completed 事件。
    return selectedThreadId &&
      selectedThreadId !== rootThreadId &&
      event.thread_id === selectedThreadId
      ? "full"
      : "tasks";
  }
  if (event.type === "tool.completed" && event.data.tool_name === "write_todos") return "todos";
  return "none";
}
