import { afterEach, describe, expect, it, vi } from "vitest";
import { advanceCursor, connectEvents, isStreamEvent, type EventCursor } from "./events";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  closed = false;
  constructor(
    public url: string,
    public options: { withCredentials: boolean },
  ) {
    FakeEventSource.instances.push(this);
  }
  close() {
    this.closed = true;
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
  FakeEventSource.instances = [];
});

describe("本机事件传输", () => {
  it("拒绝缺少实例身份或非对象载荷的事件", () => {
    expect(isStreamEvent({ seq: 1, type: "run.started", workspace_id: "w", data: {} })).toBe(false);
    expect(
      isStreamEvent({ instance_id: "i", seq: 1, type: "run.started", workspace_id: "w", data: [] }),
    ).toBe(false);
    expect(
      isStreamEvent({ instance_id: "i", seq: 1, type: "run.started", workspace_id: "w", data: {} }),
    ).toBe(true);
  });

  it("带复合游标重新订阅，坏帧不影响后续事件", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const events: string[] = [];
    const states: string[] = [];
    const close = connectEvents(
      "workspace-1",
      "instance-A:42",
      (event) => events.push(event.type),
      (value) => states.push(value),
    );
    const source = FakeEventSource.instances[0]!;
    expect(source.url).toContain("workspace_id=workspace-1");
    expect(source.url).toContain("after=instance-A%3A42");
    expect(source.options.withCredentials).toBe(true);
    source.onopen?.();
    source.onmessage?.({ data: "not json" });
    source.onmessage?.({
      data: JSON.stringify({
        instance_id: "instance-A",
        seq: 43,
        type: "tool.started",
        workspace_id: "workspace-1",
        data: {},
      }),
    });
    source.onerror?.();
    expect(events).toEqual(["tool.started"]);
    expect(states).toEqual(["connected", "reconnecting"]);
    close();
    expect(source.closed).toBe(true);
  });

  it("ready 的较高水位不会吞掉随后补发的 6/7/8 号事件", () => {
    let cursor: EventCursor | null = { instanceId: "instance-A", seq: 5 };
    const ready = advanceCursor(cursor, {
      instance_id: "instance-A",
      seq: 8,
      type: "stream.ready",
      workspace_id: "w",
      data: {},
    });
    expect(ready.accepted).toBe(false);
    cursor = ready.cursor;
    const accepted: number[] = [];
    for (const seq of [6, 7, 8]) {
      const result = advanceCursor(cursor, {
        instance_id: "instance-A",
        seq,
        type: "tool.completed",
        workspace_id: "w",
        data: {},
      });
      cursor = result.cursor;
      if (result.accepted) accepted.push(seq);
    }
    expect(accepted).toEqual([6, 7, 8]);
    expect(cursor).toEqual({ instanceId: "instance-A", seq: 8 });
  });
});
