// @vitest-environment jsdom
import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { Session, StatusResponse, StreamEvent, Task, ThreadSnapshot } from "../api/types";
import { useWorkspace, type WorkspaceState } from "./useWorkspace";

const status: StatusResponse = { workspace_id: "workspace", session_id: "session" };
const session = (title: string): Session => ({
  id: "session",
  workspace_id: "workspace",
  title,
  status: "idle",
  updated_at: null,
});
const snapshot = (title: string, todos: ThreadSnapshot["todos"] = []): ThreadSnapshot => ({
  thread_id: "session",
  workspace_id: "workspace",
  title,
  status: "completed",
  messages: [],
  todos,
  tasks: [],
  activity: [],
});
const task = (title: string): Task => ({
  id: "task",
  thread_id: "child",
  parent_thread_id: "session",
  workspace_id: "workspace",
  title,
  role: "builder",
  status: "running",
  created_at: null,
  updated_at: null,
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((finish, fail) => {
    resolve = finish;
    reject = fail;
  });
  return { promise, resolve, reject };
}

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((message: { data: string }) => void) | null = null;
  constructor(public readonly url: string) {
    FakeEventSource.instances.push(this);
  }
  close() {}
  emit(event: StreamEvent) {
    this.onmessage?.({ data: JSON.stringify(event) });
  }
}

function event(seq: number, type: string, data: Record<string, unknown> = {}): StreamEvent {
  return {
    instance_id: "instance",
    seq,
    workspace_id: "workspace",
    thread_id: "session",
    type,
    data,
  };
}

let root: Root | null = null;
let container: HTMLDivElement | null = null;

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  vi.stubGlobal("EventSource", FakeEventSource);
  FakeEventSource.instances = [];
  history.replaceState(null, "", "/");
  vi.spyOn(api, "workspaces").mockResolvedValue([
    { id: "workspace", name: "项目", path: "C:/project", active_session_id: "session" },
  ]);
  vi.spyOn(api, "settings").mockResolvedValue({});
  vi.spyOn(api, "status").mockResolvedValue(status);
  vi.spyOn(api, "sessions").mockResolvedValue([session("原会话")]);
  vi.spyOn(api, "tasks").mockResolvedValue([]);
  vi.spyOn(api, "snapshot").mockResolvedValue(snapshot("原快照"));
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  container?.remove();
  container = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function mount() {
  let state!: WorkspaceState;
  function Probe() {
    const current = useWorkspace(status);
    useEffect(() => {
      state = current;
    });
    return null;
  }
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(<Probe />);
  });
  expect(FakeEventSource.instances).toHaveLength(1);
  return () => state;
}

async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

it("首次订阅在快照之后建立时，按 ready 水位重读并保留重读期间的新运行事件", async () => {
  const current = await mount();
  expect(current().snapshot?.title).toBe("原快照");
  const lateSnapshot = deferred<ThreadSnapshot>();
  vi.mocked(api.snapshot).mockReturnValueOnce(lateSnapshot.promise);

  await act(async () => {
    FakeEventSource.instances[0]?.emit(event(10, "stream.ready"));
    FakeEventSource.instances[0]?.emit({
      ...event(11, "run.started", { source: "message" }),
      run_id: "new-run",
    });
    FakeEventSource.instances[0]?.emit(event(12, "tool.started", { tool_call_id: "new-tool" }));
  });
  await act(async () => lateSnapshot.resolve(snapshot("重读快照")));

  expect(current().snapshot).toMatchObject({
    title: "重读快照",
    status: "running",
    active_run: { run_id: "new-run" },
  });
  expect(current().liveEvents.filter((item) => item.type === "tool.started")).toHaveLength(1);
  expect(FakeEventSource.instances.at(-1)?.url).toContain("after=instance%3A12");
});

it("快照与首次 ready 之间已开始的运行由重读恢复，ready 不跳过随后补发事件", async () => {
  const current = await mount();
  expect(current().snapshot?.status).toBe("completed");
  vi.mocked(api.snapshot).mockResolvedValueOnce({
    ...snapshot("运行已开始"),
    status: "running",
    active_run: {
      run_id: "gap-run",
      started_at: "2026-09-28T10:00:00Z",
      status: "running",
      source: "message",
    },
  });

  await act(async () => FakeEventSource.instances[0]?.emit(event(10, "stream.ready")));
  expect(current().snapshot).toMatchObject({
    title: "运行已开始",
    status: "running",
    active_run: { run_id: "gap-run" },
  });
  const reconnected = FakeEventSource.instances.at(-1)!;
  expect(reconnected.url).toContain("after=instance%3A10");

  await act(async () => {
    reconnected.emit(event(12, "stream.ready"));
    reconnected.emit(event(11, "tool.started", { tool_call_id: "first" }));
    reconnected.emit(event(12, "tool.completed", { tool_call_id: "first" }));
  });
  expect(
    current()
      .liveEvents.filter((item) => item.type.startsWith("tool."))
      .map((item) => item.seq),
  ).toEqual([11, 12]);
});

it("首次水位重读失败后保留旧游标，下一次权威重读仍能恢复运行状态", async () => {
  vi.useFakeTimers();
  const current = await mount();
  vi.mocked(api.tasks).mockRejectedValueOnce(new Error("暂时不可用"));
  vi.mocked(api.snapshot)
    .mockResolvedValueOnce(snapshot("失败前快照"))
    .mockResolvedValueOnce({
      ...snapshot("恢复后快照"),
      status: "running",
      active_run: {
        run_id: "gap-run",
        started_at: "2026-09-28T10:00:00Z",
        status: "running",
        source: "message",
      },
    });

  await act(async () => FakeEventSource.instances[0]?.emit(event(10, "stream.ready")));
  expect(FakeEventSource.instances).toHaveLength(1);
  await tick(999);
  expect(FakeEventSource.instances).toHaveLength(1);
  await tick(1);
  expect(FakeEventSource.instances.at(-1)?.url).not.toContain("after=");
  await act(async () => FakeEventSource.instances.at(-1)?.emit(event(20, "stream.ready")));
  expect(current().snapshot).toMatchObject({
    title: "恢复后快照",
    status: "running",
    active_run: { run_id: "gap-run" },
  });
});

it("缓冲缺口的重读失败后，从原游标重新订阅", async () => {
  vi.useFakeTimers();
  await mount();
  await act(async () => FakeEventSource.instances[0]?.emit(event(1, "tool.started")));
  vi.mocked(api.tasks).mockRejectedValueOnce(new Error("暂时不可用"));

  await act(async () => FakeEventSource.instances[0]?.emit(event(10, "stream.resync_required")));
  expect(FakeEventSource.instances).toHaveLength(1);
  await tick(1000);
  expect(FakeEventSource.instances.at(-1)?.url).toContain("after=instance%3A1");
});

it("持续重读失败逐步退避，卸载后取消下一次重连", async () => {
  vi.useFakeTimers();
  await mount();
  vi.mocked(api.tasks).mockRejectedValue(new Error("持续不可用"));

  await act(async () => FakeEventSource.instances[0]?.emit(event(10, "stream.ready")));
  await tick(999);
  expect(FakeEventSource.instances).toHaveLength(1);
  await tick(1);
  expect(FakeEventSource.instances).toHaveLength(2);

  await act(async () => FakeEventSource.instances[1]?.emit(event(20, "stream.ready")));
  await tick(1999);
  expect(FakeEventSource.instances).toHaveLength(2);
  await tick(1);
  expect(FakeEventSource.instances).toHaveLength(3);

  await act(async () => FakeEventSource.instances[2]?.emit(event(30, "stream.ready")));
  await act(async () => root?.unmount());
  root = null;
  await tick(4000);
  expect(FakeEventSource.instances).toHaveLength(3);
});

it("游标回退后重放同一事件不会重复加入实时轨迹", async () => {
  vi.useFakeTimers();
  const current = await mount();
  await act(async () => FakeEventSource.instances[0]?.emit(event(1, "tool.started")));
  const oldTasks = deferred<Task[]>();
  vi.mocked(api.tasks).mockReturnValueOnce(oldTasks.promise);

  await act(async () => FakeEventSource.instances[0]?.emit(event(10, "stream.resync_required")));
  await act(async () =>
    FakeEventSource.instances[0]?.emit(event(11, "tool.started", { tool_call_id: "repeat" })),
  );
  await act(async () => oldTasks.reject(new Error("暂时不可用")));
  await tick(1000);
  const reconnected = FakeEventSource.instances.at(-1)!;
  expect(reconnected.url).toContain("after=instance%3A1");
  await act(async () => {
    reconnected.emit(event(12, "stream.ready"));
    reconnected.emit(event(11, "tool.started", { tool_call_id: "repeat" }));
  });
  expect(current().liveEvents.filter((item) => item.seq === 11)).toHaveLength(1);
});

it("删除事件丢失后，重同步先查会话目录，再读取替代会话快照", async () => {
  const current = await mount();
  vi.mocked(api.sessions).mockResolvedValueOnce([{ ...session("可用会话"), id: "replacement" }]);
  vi.mocked(api.snapshot).mockClear();
  vi.mocked(api.snapshot).mockImplementation(async (id) => {
    if (id === "session") throw new Error("会话已删除");
    return { ...snapshot("替代会话快照"), thread_id: id };
  });

  await act(async () => FakeEventSource.instances[0]?.emit(event(10, "stream.resync_required")));
  expect(current().sessionId).toBe("replacement");
  expect(current().threadId).toBe("replacement");
  expect(current().snapshot?.title).toBe("替代会话快照");
  const requested = vi.mocked(api.snapshot).mock.calls.map(([id]) => id);
  expect(requested).toContain("replacement");
  expect(requested).not.toContain("session");
});

it("手动刷新期间的新运行事件不被迟到的旧快照改回已完成", async () => {
  const current = await mount();
  const oldSnapshot = deferred<ThreadSnapshot>();
  vi.mocked(api.snapshot).mockReturnValueOnce(oldSnapshot.promise);

  let refresh!: Promise<void>;
  await act(async () => {
    refresh = current().refresh();
  });
  await act(async () =>
    FakeEventSource.instances[0]?.emit({
      ...event(1, "run.started"),
      run_id: "new-run",
    }),
  );
  expect(current().snapshot?.status).toBe("running");
  await act(async () => {
    oldSnapshot.resolve(snapshot("过期快照"));
    await refresh;
  });
  expect(current().snapshot).toMatchObject({
    status: "running",
    active_run: { run_id: "new-run" },
  });
});

it("事件触发的普通重读期间又开始新运行时，迟到的完整快照不回滚状态", async () => {
  vi.useFakeTimers();
  const current = await mount();
  const oldSnapshot = deferred<ThreadSnapshot>();
  vi.mocked(api.snapshot).mockReturnValueOnce(oldSnapshot.promise);

  await act(async () => FakeEventSource.instances[0]?.emit(event(1, "run.completed")));
  await tick(180);
  await act(async () =>
    FakeEventSource.instances[0]?.emit({
      ...event(2, "run.started"),
      run_id: "next-run",
    }),
  );
  expect(current().snapshot?.status).toBe("running");
  await act(async () => oldSnapshot.resolve(snapshot("上一轮结束")));
  expect(current().snapshot).toMatchObject({
    status: "running",
    active_run: { run_id: "next-run" },
  });
});

it("运行事件缓冲溢出时重新读快照，并继续接纳重读期间的新事件", async () => {
  const current = await mount();
  const oldSnapshot = deferred<ThreadSnapshot>();
  const retrySnapshot = deferred<ThreadSnapshot>();
  vi.mocked(api.snapshot)
    .mockReturnValueOnce(oldSnapshot.promise)
    .mockReturnValueOnce(retrySnapshot.promise);
  let refresh!: Promise<void>;
  await act(async () => {
    refresh = current().refresh();
  });

  await act(async () => {
    for (let seq = 1; seq <= 513; seq += 1) {
      FakeEventSource.instances[0]?.emit({
        ...event(seq, "run.started"),
        run_id: `current-${seq}`,
      });
    }
  });
  await act(async () => {
    oldSnapshot.resolve(snapshot("过期快照"));
    await refresh;
  });

  await act(async () =>
    FakeEventSource.instances[0]?.emit({
      ...event(514, "run.started"),
      run_id: "latest-run",
    }),
  );
  await act(async () => retrySnapshot.resolve(snapshot("重读时已过期")));
  expect(current().snapshot).toMatchObject({
    status: "running",
    active_run: { run_id: "latest-run" },
  });
});

it("其他线程的运行事件不会挤掉当前线程的快照交接缓冲", async () => {
  const current = await mount();
  const slowSnapshot = deferred<ThreadSnapshot>();
  vi.mocked(api.snapshot).mockClear();
  vi.mocked(api.snapshot).mockReturnValueOnce(slowSnapshot.promise);
  let refresh!: Promise<void>;
  await act(async () => {
    refresh = current().refresh();
  });
  await act(async () => {
    for (let seq = 1; seq <= 513; seq += 1)
      FakeEventSource.instances[0]?.emit({
        ...event(seq, "run.started"),
        thread_id: "other-thread",
        run_id: `other-${seq}`,
      });
  });
  await act(async () => {
    slowSnapshot.resolve(snapshot("当前线程快照"));
    await refresh;
  });
  expect(current().snapshot?.title).toBe("当前线程快照");
  expect(api.snapshot).toHaveBeenCalledTimes(1);
});

it("两个会话列表刷新乱序返回时，旧响应不覆盖新列表", async () => {
  vi.useFakeTimers();
  const current = await mount();
  const oldResponse = deferred<Session[]>();
  const newResponse = deferred<Session[]>();
  vi.mocked(api.sessions)
    .mockReturnValueOnce(oldResponse.promise)
    .mockReturnValueOnce(newResponse.promise);

  await act(async () => FakeEventSource.instances[0]?.emit(event(1, "run.completed")));
  await tick(180);
  await act(async () => FakeEventSource.instances[0]?.emit(event(2, "run.failed")));
  await tick(180);
  await act(async () => newResponse.resolve([session("新会话")]));
  expect(current().sessions[0]?.title).toBe("新会话");
  await act(async () => oldResponse.resolve([session("旧会话")]));
  expect(current().sessions[0]?.title).toBe("新会话");
});

it("切换工作区后，旧工作区的会话与任务响应不能写进新工作区", async () => {
  vi.useFakeTimers();
  vi.mocked(api.workspaces).mockResolvedValue([
    { id: "workspace", name: "旧项目", path: "C:/old", active_session_id: "session" },
    { id: "other", name: "新项目", path: "C:/new", active_session_id: "other-session" },
  ]);
  const current = await mount();
  const oldSessions = deferred<Session[]>();
  const oldTasks = deferred<Task[]>();
  vi.mocked(api.sessions).mockImplementation((id) =>
    id === "workspace"
      ? oldSessions.promise
      : Promise.resolve([{ ...session("新项目会话"), id: "other-session", workspace_id: "other" }]),
  );
  vi.mocked(api.tasks).mockImplementation((id) =>
    id === "workspace"
      ? oldTasks.promise
      : Promise.resolve([{ ...task("新项目任务"), workspace_id: "other" }]),
  );

  await act(async () => FakeEventSource.instances[0]?.emit(event(1, "run.completed")));
  await tick(180);
  await act(async () => current().selectWorkspace("other"));
  expect(current().sessions[0]?.title).toBe("新项目会话");
  expect(current().tasks[0]?.title).toBe("新项目任务");
  await act(async () => {
    oldSessions.resolve([session("旧项目会话")]);
    oldTasks.resolve([task("旧项目任务")]);
  });
  expect(current().sessions[0]?.title).toBe("新项目会话");
  expect(current().tasks[0]?.title).toBe("新项目任务");
});

it("切换工作区后，旧工作区异步刷新失败不显示在新页面", async () => {
  vi.useFakeTimers();
  vi.mocked(api.workspaces).mockResolvedValue([
    { id: "workspace", name: "旧项目", path: "C:/old", active_session_id: "session" },
    { id: "other", name: "新项目", path: "C:/new", active_session_id: "other-session" },
  ]);
  const current = await mount();
  const oldTasks = deferred<Task[]>();
  vi.mocked(api.tasks).mockImplementation((id) =>
    id === "workspace" ? oldTasks.promise : Promise.resolve([]),
  );
  vi.mocked(api.sessions).mockImplementation((id) =>
    Promise.resolve(
      id === "workspace"
        ? [session("旧会话")]
        : [{ ...session("新会话"), id: "other-session", workspace_id: "other" }],
    ),
  );

  await act(async () => FakeEventSource.instances[0]?.emit(event(1, "task.running")));
  await tick(220);
  await act(async () => current().selectWorkspace("other"));
  await act(async () => oldTasks.reject(new Error("旧工作区任务请求失败")));
  expect(current().workspaceId).toBe("other");
  expect(current().error).toBeNull();
});

it("旧的全局刷新不覆盖随后保存的设置与新建工作区", async () => {
  const current = await mount();
  const oldWorkspaces = deferred<Awaited<ReturnType<typeof api.workspaces>>>();
  const oldSettings = deferred<Awaited<ReturnType<typeof api.settings>>>();
  vi.mocked(api.workspaces)
    .mockReturnValueOnce(oldWorkspaces.promise)
    .mockResolvedValueOnce([
      { id: "workspace", name: "旧项目", path: "C:/old" },
      { id: "new", name: "新项目", path: "C:/new" },
    ]);
  vi.mocked(api.settings).mockReturnValueOnce(oldSettings.promise);
  vi.spyOn(api, "updateSettings").mockResolvedValue({ language: "en" });
  vi.spyOn(api, "addWorkspace").mockResolvedValue({ id: "new", name: "新项目", path: "C:/new" });

  let oldRefresh!: Promise<void>;
  await act(async () => {
    oldRefresh = current().refresh();
  });
  await act(async () => {
    await current().updateSettings({ language: "en" });
    await current().addWorkspace("C:/new", "新项目");
  });
  expect(current().settings?.language).toBe("en");
  expect(current().workspaces.map((item) => item.id)).toContain("new");

  await act(async () => {
    oldWorkspaces.resolve([{ id: "workspace", name: "旧项目", path: "C:/old" }]);
    oldSettings.resolve({ language: "zh" });
    await oldRefresh;
  });
  expect(current().settings?.language).toBe("en");
  expect(current().workspaces.map((item) => item.id)).toContain("new");
});

it("两个任务刷新乱序返回时，旧响应不覆盖新任务", async () => {
  vi.useFakeTimers();
  const current = await mount();
  const oldResponse = deferred<Task[]>();
  const newResponse = deferred<Task[]>();
  vi.mocked(api.tasks)
    .mockReturnValueOnce(oldResponse.promise)
    .mockReturnValueOnce(newResponse.promise);

  await act(async () => FakeEventSource.instances[0]?.emit(event(1, "task.running")));
  await tick(220);
  await act(async () => FakeEventSource.instances[0]?.emit(event(2, "task.completed")));
  await tick(220);
  await act(async () => newResponse.resolve([task("新任务")]));
  expect(current().tasks[0]?.title).toBe("新任务");
  await act(async () => oldResponse.resolve([task("旧任务")]));
  expect(current().tasks[0]?.title).toBe("新任务");
});

it("两个待办刷新乱序返回时，旧响应不覆盖新待办", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-09-28T10:00:00Z"));
  const current = await mount();
  const oldResponse = deferred<ThreadSnapshot>();
  const newResponse = deferred<ThreadSnapshot>();
  vi.mocked(api.snapshot)
    .mockReturnValueOnce(oldResponse.promise)
    .mockReturnValueOnce(newResponse.promise);

  await act(async () =>
    FakeEventSource.instances[0]?.emit(event(1, "tool.completed", { tool_name: "write_todos" })),
  );
  await tick(0);
  await act(async () =>
    FakeEventSource.instances[0]?.emit(event(2, "tool.completed", { tool_name: "write_todos" })),
  );
  await tick(2000);
  await act(async () =>
    newResponse.resolve(snapshot("新快照", [{ id: "new", content: "新待办", status: "pending" }])),
  );
  expect(current().snapshot?.todos[0]?.content).toBe("新待办");
  await act(async () =>
    oldResponse.resolve(snapshot("旧快照", [{ id: "old", content: "旧待办", status: "pending" }])),
  );
  expect(current().snapshot?.todos[0]?.content).toBe("新待办");
});

it("旧待办请求在新的完整快照之后返回时，保留完整快照中的待办", async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-09-28T10:00:00Z"));
  const current = await mount();
  const oldResponse = deferred<ThreadSnapshot>();
  vi.mocked(api.snapshot)
    .mockReturnValueOnce(oldResponse.promise)
    .mockResolvedValueOnce(
      snapshot("完整快照", [{ id: "new", content: "完整快照的新待办", status: "completed" }]),
    );

  await act(async () =>
    FakeEventSource.instances[0]?.emit(event(1, "tool.completed", { tool_name: "write_todos" })),
  );
  await tick(0);
  await act(async () => FakeEventSource.instances[0]?.emit(event(2, "run.completed")));
  await tick(180);
  expect(current().snapshot?.todos[0]?.content).toBe("完整快照的新待办");

  await act(async () =>
    oldResponse.resolve(snapshot("旧快照", [{ id: "old", content: "旧待办", status: "pending" }])),
  );
  expect(current().snapshot?.todos[0]?.content).toBe("完整快照的新待办");
});
