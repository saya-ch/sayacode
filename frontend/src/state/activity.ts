import type { StreamEvent, ThreadSnapshot } from "../api/types";

export function reconcileActivity(
  previous: ThreadSnapshot | null,
  next: ThreadSnapshot,
  live: StreamEvent[],
  forceSnapshot = false,
): { snapshot: ThreadSnapshot; live: StreamEvent[] } {
  const running = next.status === "running";
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
