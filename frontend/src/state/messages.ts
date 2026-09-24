import type { ThreadSnapshot } from "../api/types";

export function appendUserMessage(
  snapshot: ThreadSnapshot | null,
  threadId: string,
  runId: string,
  text: string,
): ThreadSnapshot | null {
  if (!snapshot || snapshot.thread_id !== threadId) return snapshot;
  const id = `pending-${runId}`;
  if (snapshot.messages.some((message) => message.id === id)) return snapshot;
  return { ...snapshot, messages: [...snapshot.messages, { id, role: "human", text }] };
}
