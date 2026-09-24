import type { Task } from "../api/types";

export function descendantTasks(tasks: Task[], rootThreadId: string | null): Task[] {
  if (!rootThreadId) return [];
  const children = new Map<string, Task[]>();
  for (const task of tasks) {
    if (!task.parent_thread_id) continue;
    children.set(task.parent_thread_id, [...(children.get(task.parent_thread_id) ?? []), task]);
  }
  const result: Task[] = [];
  const visited = new Set<string>([rootThreadId]);
  const queue = [rootThreadId];
  for (const parent of queue) {
    for (const task of children.get(parent) ?? []) {
      if (visited.has(task.thread_id)) continue;
      visited.add(task.thread_id);
      result.push(task);
      queue.push(task.thread_id);
    }
  }
  return result;
}
