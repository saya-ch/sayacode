import type { StreamEvent } from "./types";

export function isStreamEvent(value: unknown): value is StreamEvent {
  if (!value || typeof value !== "object") return false;
  const event = value as Partial<StreamEvent>;
  return (
    typeof event.instance_id === "string" &&
    typeof event.seq === "number" &&
    Number.isSafeInteger(event.seq) &&
    event.seq >= 0 &&
    typeof event.type === "string" &&
    typeof event.workspace_id === "string" &&
    event.data !== null &&
    typeof event.data === "object" &&
    !Array.isArray(event.data)
  );
}

export interface EventCursor {
  instanceId: string;
  seq: number;
}

export function advanceCursor(
  current: EventCursor | null,
  event: StreamEvent,
): { accepted: boolean; cursor: EventCursor | null } {
  // ready 宣告生产者水位，不代表缓冲已交付；不能用它跳过后续补发帧。
  if (event.type === "stream.ready") return { accepted: false, cursor: current };
  if (current?.instanceId === event.instance_id && event.seq <= current.seq) {
    return { accepted: false, cursor: current };
  }
  return { accepted: true, cursor: { instanceId: event.instance_id, seq: event.seq } };
}

export function connectEvents(
  workspaceId: string,
  after: string | null,
  onEvent: (event: StreamEvent) => void,
  onConnection: (state: "connected" | "reconnecting") => void,
): () => void {
  const params = new URLSearchParams({ workspace_id: workspaceId });
  if (after) params.set("after", after);
  const source = new EventSource(`/api/events?${params}`, { withCredentials: true });
  source.onopen = () => onConnection("connected");
  source.onerror = () => onConnection("reconnecting");
  source.onmessage = (message) => {
    try {
      const value: unknown = JSON.parse(message.data);
      if (isStreamEvent(value)) onEvent(value);
    } catch {
      // 无效帧不影响后续事件，下一次快照仍以图与 Store 为准。
    }
  };
  return () => source.close();
}
