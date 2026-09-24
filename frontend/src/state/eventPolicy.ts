import type { StreamEvent } from "../api/types";

export type RefreshPlan = "full" | "tasks" | "todos" | "none";

export function refreshPlan(event: StreamEvent): RefreshPlan {
  if (
    [
      "run.completed",
      "run.failed",
      "run.paused",
      "run.stopped",
      "approval.requested",
      "thread.compacted",
      "thread.rewound",
    ].includes(event.type)
  )
    return "full";
  if (event.type.startsWith("agent.wake.") && !event.type.endsWith(".started")) return "full";
  if (event.type.startsWith("task.")) return "tasks";
  if (event.type === "tool.completed" && event.data.tool_name === "write_todos") return "todos";
  return "none";
}
