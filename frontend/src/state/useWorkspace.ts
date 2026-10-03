import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { advanceCursor, connectEvents, type EventCursor } from "../api/events";
import { reconcileActivity } from "./activity";
import { appendUserMessage } from "./messages";
import { submitQueuedInput } from "./queuedInput";
import { RequestOrder } from "./requestOrder";
import {
  applyRunEvent,
  applyThreadSnapshot,
  belongsToSelectedTimeline,
  refreshPlan,
} from "./eventPolicy";
import { emptyLiveText, reduceLiveText } from "./liveText";
import type {
  Activity,
  ApprovalDecision,
  ApprovalGrant,
  Attachment,
  Session,
  SettingsResponse,
  StatusResponse,
  StreamEvent,
  Task,
  TaskActionResult,
  ThreadSnapshot,
  Todo,
  TrustLevel,
  Workspace,
} from "../api/types";

type WorkspaceResponse<T> = {
  workspaceId: string;
  generation: number;
  sequence: number;
  value: T;
};

type SharedResponse<T> = { key: string; sequence: number; value: T };

type TodoResponse = {
  threadId: string;
  generation: number;
  sequence: number;
  readSequence: number;
  value: ThreadSnapshot;
};

type RunTransition = { revision: number; event: StreamEvent };

function lifecycleKey(type: string, id: unknown): string | null {
  return typeof id === "string" && id ? `${type}:${id}` : null;
}

/** 重同步期间仍在到达的事件，以快照覆盖的事实为界交接给实时流。 */
export function reconcileResyncedEvents(
  snapshot: ThreadSnapshot,
  live: StreamEvent[],
  boundary: EventCursor,
): StreamEvent[] {
  const audited = new Set(
    (snapshot.activity ?? [])
      .map((row: Activity) => {
        const data =
          row.data && typeof row.data === "object" && !Array.isArray(row.data)
            ? (row.data as Record<string, unknown>)
            : {};
        return row.type.startsWith("model.")
          ? lifecycleKey(row.type, row.run_id ?? data.run_id)
          : row.type.startsWith("tool.")
            ? lifecycleKey(row.type, data.tool_call_id)
            : null;
      })
      .filter((key): key is string => key !== null),
  );
  return live.filter((event) => {
    if (
      !event.type.startsWith("model.") &&
      !event.type.startsWith("tool.") &&
      !event.type.startsWith("run.")
    )
      return true;
    if (event.instance_id !== boundary.instanceId || event.seq <= boundary.seq) return false;
    const key = event.type.startsWith("model.")
      ? lifecycleKey(event.type, event.data.model_run_id)
      : event.type.startsWith("tool.")
        ? lifecycleKey(event.type, event.data.tool_call_id)
        : null;
    return !key || !audited.has(key);
  });
}

/** 快照读取后开始的新轮次不能被较旧的快照状态盖掉。 */
export function replayResyncedRuns(
  snapshot: ThreadSnapshot,
  live: StreamEvent[],
  boundary: EventCursor,
): ThreadSnapshot {
  return reconcileResyncedEvents(snapshot, live, boundary).reduce(
    (current, event) => applyRunEvent(current, event) ?? current,
    snapshot,
  );
}

function messageFrom(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败，请稍后重试。";
}

function selectionFromUrl(): {
  workspaceId: string | null;
  sessionId: string | null;
  threadId: string | null;
} {
  const search = new URLSearchParams(location.search);
  return {
    workspaceId: search.get("workspace"),
    sessionId: search.get("session"),
    threadId: search.get("thread"),
  };
}

function saveSelection(
  workspaceId: string | null,
  sessionId: string | null,
  threadId: string | null,
) {
  const url = new URL(location.href);
  for (const [key, value] of [
    ["workspace", workspaceId],
    ["session", sessionId],
    ["thread", threadId],
  ] as const) {
    if (value) url.searchParams.set(key, value);
    else url.searchParams.delete(key);
  }
  history.replaceState(null, "", url);
}

export interface WorkspaceState {
  status: StatusResponse;
  settings: SettingsResponse | null;
  workspaces: Workspace[];
  sessions: Session[];
  tasks: Task[];
  snapshot: ThreadSnapshot | null;
  parentSnapshot: ThreadSnapshot | null;
  workspaceId: string | null;
  sessionId: string | null;
  threadId: string | null;
  liveEvents: StreamEvent[];
  liveText: string;
  streamResynced: boolean;
  connection: "connecting" | "connected" | "reconnecting";
  loading: boolean;
  busy: boolean;
  error: string | null;
  selectWorkspace(id: string): void;
  selectSession(id: string): void;
  selectThread(id: string): void;
  clearError(): void;
  refresh(): Promise<void>;
  addWorkspace(path: string, name?: string): Promise<void>;
  renameWorkspace(id: string, name: string): Promise<void>;
  newSession(): Promise<void>;
  renameSession(id: string, title: string): Promise<void>;
  deleteSession(id: string): Promise<void>;
  send(message: string, attachmentIds?: string[], messageId?: string): Promise<void>;
  steerQueuedMessage(id: string): Promise<void>;
  editQueuedMessage(id: string, text: string): Promise<void>;
  removeQueuedMessage(id: string): Promise<void>;
  stopSession(): Promise<void>;
  resumeSession(): Promise<void>;
  uploadAttachment(file: File): Promise<Attachment>;
  discardAttachment(id: string): Promise<void>;
  compactCurrent(focus?: string): Promise<void>;
  activateSkill(name: string): Promise<void>;
  approve(decisions: ApprovalDecision[], grants?: ApprovalGrant[]): Promise<void>;
  setTrust(level: TrustLevel): Promise<void>;
  setThreadModel(name: string): Promise<void>;
  setDefaultTrust(level: TrustLevel): Promise<void>;
  updateSettings(patch: Partial<SettingsResponse>): Promise<void>;
  spawnTask(input: {
    role: string;
    prompt: string;
    title?: string;
    worktree_enabled?: boolean;
  }): Promise<void>;
  taskAction(
    taskId: string,
    action: string,
    data?: Record<string, unknown>,
  ): Promise<TaskActionResult>;
}

export function useWorkspace(status: StatusResponse): WorkspaceState {
  const initial = useRef(selectionFromUrl());
  const [statusState, setStatusState] = useState(status);
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [snapshot, setSnapshot] = useState<ThreadSnapshot | null>(null);
  const [parentSnapshot, setParentSnapshot] = useState<ThreadSnapshot | null>(null);
  const [workspaceId, setWorkspaceId] = useState<string | null>(
    initial.current.workspaceId ?? status.workspace_id ?? null,
  );
  const [sessionId, setSessionId] = useState<string | null>(
    initial.current.sessionId ?? status.session_id ?? null,
  );
  const [threadId, setThreadId] = useState<string | null>(
    initial.current.threadId ?? status.session_id ?? null,
  );
  const [liveEvents, setLiveEvents] = useState<StreamEvent[]>([]);
  const [liveTextState, setLiveTextState] = useState(emptyLiveText);
  const [connection, setConnection] = useState<"connecting" | "connected" | "reconnecting">(
    "connecting",
  );
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const cursor = useRef<EventCursor | null>(null);
  const [streamEpoch, setStreamEpoch] = useState(0);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const taskRefreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const todoRefreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastTodoRefresh = useRef(0);
  const lastStartedRun = useRef<string | null>(null);
  const workspaceRef = useRef(workspaceId);
  const threadRef = useRef(threadId);
  const sessionRef = useRef(sessionId);
  const tasksRef = useRef(tasks);
  const snapshotRef = useRef(snapshot);
  const parentSnapshotRef = useRef(parentSnapshot);
  const snapshotRequests = useRef(new RequestOrder()).current;
  const collectionRequests = useRef(new RequestOrder()).current;
  const runRevision = useRef(new Map<string, number>()).current;
  const runTransitions = useRef(new Map<string, RunTransition[]>()).current;
  const resyncFailures = useRef(0);
  const snapshotReadSequence = useRef(0);
  const committedTodos = useRef(new Map<string, { sequence: number; todos: Todo[] }>()).current;
  const workspaceGeneration = useRef(0);
  const viewGeneration = useRef(0);
  workspaceRef.current = workspaceId;
  threadRef.current = threadId;
  sessionRef.current = sessionId;
  tasksRef.current = tasks;
  snapshotRef.current = snapshot;
  parentSnapshotRef.current = parentSnapshot;

  const report = useCallback((reason: unknown) => setError(messageFrom(reason)), []);

  const requestShared = async <T>(
    key: string,
    read: () => Promise<T>,
  ): Promise<SharedResponse<T>> => {
    const sequence = collectionRequests.begin(`shared:${key}`);
    const value = await read();
    return { key, sequence, value };
  };
  const latestShared = <T>(response: SharedResponse<T>): T | null =>
    collectionRequests.isLatest(`shared:${response.key}`, response.sequence)
      ? response.value
      : null;

  const requestSessions = async (
    requestedWorkspaceId: string,
  ): Promise<WorkspaceResponse<Session[]>> => {
    const sequence = collectionRequests.begin(`sessions:${requestedWorkspaceId}`);
    const generation = workspaceGeneration.current;
    const value = await api.sessions(requestedWorkspaceId);
    return { workspaceId: requestedWorkspaceId, generation, sequence, value };
  };
  const requestTasks = async (requestedWorkspaceId: string): Promise<WorkspaceResponse<Task[]>> => {
    const sequence = collectionRequests.begin(`tasks:${requestedWorkspaceId}`);
    const generation = workspaceGeneration.current;
    const value = await api.tasks(requestedWorkspaceId);
    return { workspaceId: requestedWorkspaceId, generation, sequence, value };
  };
  const latestCollection = <T>(
    kind: "sessions" | "tasks",
    response: WorkspaceResponse<T> | null,
  ): T | null =>
    response &&
    workspaceRef.current === response.workspaceId &&
    workspaceGeneration.current === response.generation &&
    collectionRequests.isLatest(`${kind}:${response.workspaceId}`, response.sequence)
      ? response.value
      : null;
  const requestTodos = async (requestedThreadId: string): Promise<TodoResponse> => {
    const sequence = collectionRequests.begin(`todos:${requestedThreadId}`);
    const readSequence = ++snapshotReadSequence.current;
    const generation = viewGeneration.current;
    const value = await api.snapshot(requestedThreadId);
    return { threadId: requestedThreadId, generation, sequence, readSequence, value };
  };
  const latestTodos = (response: TodoResponse | null): ThreadSnapshot | null => {
    if (
      !response ||
      response.generation !== viewGeneration.current ||
      !collectionRequests.isLatest(`todos:${response.threadId}`, response.sequence) ||
      (committedTodos.get(response.threadId)?.sequence ?? 0) > response.readSequence
    )
      return null;
    committedTodos.set(response.threadId, {
      sequence: response.readSequence,
      todos: response.value.todos,
    });
    return response.value;
  };

  const preserveNewerTodos = (
    requestedThreadId: string,
    readSequence: number,
    result: ThreadSnapshot,
  ): ThreadSnapshot => {
    const committed = committedTodos.get(requestedThreadId);
    if (committed && committed.sequence > readSequence)
      return { ...result, todos: committed.todos };
    committedTodos.set(requestedThreadId, { sequence: readSequence, todos: result.todos });
    return result;
  };

  const ensureSession = (
    requestedWorkspaceId: string,
    nextSessions: Session[],
    replaceMissing = false,
  ) => {
    if (workspaceRef.current !== requestedWorkspaceId) return;
    const currentSession = sessionRef.current;
    if (
      currentSession &&
      (!replaceMissing || nextSessions.some((item) => item.id === currentSession))
    )
      return;
    const workspace = workspaces.find((item) => item.id === requestedWorkspaceId);
    const chosen =
      nextSessions.find((item) => item.id === workspace?.active_session_id) ?? nextSessions[0];
    const nextId = chosen?.id ?? null;
    if (nextId === currentSession && (nextId !== null || threadRef.current === null)) return;
    viewGeneration.current += 1;
    sessionRef.current = nextId;
    threadRef.current = nextId;
    setSessionId(nextId);
    setThreadId(nextId);
    setSnapshot(null);
  };

  const commitThreadSnapshot = (requestedThreadId: string, result: ThreadSnapshot) => {
    setSnapshot((current) =>
      applyThreadSnapshot(current, threadRef.current, requestedThreadId, result),
    );
    setParentSnapshot((current) =>
      applyThreadSnapshot(current, sessionRef.current, requestedThreadId, result),
    );
  };
  const requestSnapshot = async (requestedThreadId: string) => {
    const sequence = snapshotRequests.begin(requestedThreadId);
    const readSequence = ++snapshotReadSequence.current;
    const startedRunRevision = runRevision.get(requestedThreadId) ?? 0;
    const result = await api.snapshot(requestedThreadId);
    return { threadId: requestedThreadId, sequence, readSequence, startedRunRevision, result };
  };
  const latestSnapshot = (
    response: Awaited<ReturnType<typeof requestSnapshot>> | null,
  ): ThreadSnapshot | null => {
    if (!response || !snapshotRequests.isLatest(response.threadId, response.sequence)) return null;
    const transitions = runTransitions.get(response.threadId) ?? [];
    const firstRetained = transitions[0]?.revision;
    if (firstRetained && response.startedRunRevision < firstRetained - 1) {
      // 极长请求期间事件缓冲已裁剪，重新读取以免猜测运行状态。
      if (response.threadId === threadRef.current || response.threadId === sessionRef.current)
        queueMicrotask(() => void refreshThreadSnapshot(response.threadId).catch(report));
      return null;
    }
    const result = preserveNewerTodos(response.threadId, response.readSequence, response.result);
    return transitions.reduce(
      (current, item) =>
        item.revision > response.startedRunRevision
          ? (applyRunEvent(current, item.event) ?? current)
          : current,
      result,
    );
  };
  const refreshThreadSnapshot = async (requestedThreadId: string) => {
    const result = latestSnapshot(await requestSnapshot(requestedThreadId));
    if (result) commitThreadSnapshot(requestedThreadId, result);
  };

  const refresh = useCallback(async () => {
    const [
      workspacesResponse,
      settingsResponse,
      statusResponse,
      sessionsResponse,
      tasksResponse,
      threadResponse,
      parentResponse,
    ] = await Promise.all([
      requestShared("workspaces", api.workspaces),
      requestShared("settings", api.settings),
      requestShared("status", api.status),
      workspaceId ? requestSessions(workspaceId) : Promise.resolve(null),
      workspaceId ? requestTasks(workspaceId) : Promise.resolve(null),
      threadId ? requestSnapshot(threadId) : Promise.resolve(null),
      sessionId && sessionId !== threadId ? requestSnapshot(sessionId) : Promise.resolve(null),
    ]);
    const nextThread = latestSnapshot(threadResponse);
    const nextParent = latestSnapshot(parentResponse);
    const nextWorkspaces = latestShared(workspacesResponse);
    const nextSettings = latestShared(settingsResponse);
    const nextStatus = latestShared(statusResponse);
    if (nextWorkspaces) setWorkspaces(nextWorkspaces);
    if (nextSettings) setSettings(nextSettings);
    if (nextStatus) setStatusState(nextStatus);
    const nextSessions = latestCollection("sessions", sessionsResponse);
    const nextTasks = latestCollection("tasks", tasksResponse);
    if (workspaceId && nextSessions) {
      setSessions(nextSessions);
      ensureSession(workspaceId, nextSessions);
    }
    if (nextTasks) setTasks(nextTasks);
    if (nextThread && threadRef.current === threadId) {
      setSnapshot(nextThread);
      setLiveTextState(emptyLiveText);
      setLiveEvents((items) => reconcileActivity(null, nextThread, items, true).live);
    }
    if (sessionRef.current === sessionId) {
      if (nextParent) setParentSnapshot(nextParent);
      else if (threadId === sessionId && nextThread) setParentSnapshot(nextThread);
    }
  }, [workspaceId, threadId, sessionId]);

  useEffect(() => {
    let alive = true;
    Promise.all([
      requestShared("workspaces", api.workspaces),
      requestShared("settings", api.settings),
    ])
      .then(([workspacesResponse, settingsResponse]) => {
        if (!alive) return;
        const items = latestShared(workspacesResponse);
        const value = latestShared(settingsResponse);
        if (items) setWorkspaces(items);
        if (value) setSettings(value);
        if (!items) return;
        const chosen =
          items.find((item) => item.id === workspaceId) ??
          items.find((item) => item.id === status.workspace_id) ??
          items[0];
        if (chosen && chosen.id !== workspaceRef.current) {
          workspaceGeneration.current += 1;
          workspaceRef.current = chosen.id;
          setWorkspaceId(chosen.id);
        }
      })
      .catch((reason: unknown) => {
        if (alive) report(reason);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
    // 初始化只执行一次；后续更新通过显式操作与事件流进入。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!workspaceId) {
      setSessions([]);
      setTasks([]);
      return;
    }
    let alive = true;
    Promise.all([requestSessions(workspaceId), requestTasks(workspaceId)])
      .then(([sessionsResponse, tasksResponse]) => {
        if (!alive) return;
        const nextSessions = latestCollection("sessions", sessionsResponse);
        const nextTasks = latestCollection("tasks", tasksResponse);
        if (nextSessions) {
          setSessions(nextSessions);
          ensureSession(workspaceId, nextSessions, true);
        }
        if (nextTasks) setTasks(nextTasks);
      })
      .catch((reason: unknown) => {
        if (alive) report(reason);
      });
    return () => {
      alive = false;
    };
  }, [workspaceId, workspaces, report]);

  useEffect(() => {
    if (!threadId) {
      setSnapshot(null);
      return;
    }
    let alive = true;
    lastStartedRun.current = null;
    setSnapshot(null);
    setLiveTextState(emptyLiveText);
    setLiveEvents([]);
    requestSnapshot(threadId)
      .then((response) => {
        const value = latestSnapshot(response);
        if (!alive || !value) return;
        commitThreadSnapshot(threadId, value);
        if (
          value.status === "running" &&
          value.active_run &&
          lastStartedRun.current !== value.active_run.run_id
        ) {
          // 已在运行的线程可能缺少连接前的正文增量，等检查点给出完整结果。
          setLiveTextState((old) => reduceLiveText(old, "resync"));
        }
      })
      .catch((reason: unknown) => {
        if (alive) report(reason);
      });
    return () => {
      alive = false;
    };
  }, [threadId, report]);

  useEffect(() => {
    if (!sessionId) {
      setParentSnapshot(null);
      return;
    }
    setParentSnapshot(null);
    let alive = true;
    requestSnapshot(sessionId)
      .then((response) => {
        const value = latestSnapshot(response);
        if (alive && value) commitThreadSnapshot(sessionId, value);
      })
      .catch(report);
    return () => {
      alive = false;
    };
  }, [sessionId, report]);

  useEffect(
    () => saveSelection(workspaceId, sessionId, threadId),
    [workspaceId, sessionId, threadId],
  );

  useEffect(() => {
    for (const owner of runTransitions.keys()) {
      if (owner === threadId || owner === sessionId) continue;
      runTransitions.delete(owner);
      runRevision.delete(owner);
    }
  }, [threadId, sessionId, runTransitions, runRevision]);

  useEffect(() => {
    if (!workspaceId) return;
    let alive = true;
    let resyncRevision = 0;
    let resyncBoundary: EventCursor | null = null;
    let resyncRuns: StreamEvent[] = [];
    let resyncBaseline: EventCursor | null = null;
    let resyncActive = false;
    let resyncRetryTimer: ReturnType<typeof setTimeout> | null = null;
    let todoRevision = 0;
    setConnection("connecting");
    const reportCurrent = (reason: unknown) => {
      if (alive && workspaceRef.current === workspaceId) report(reason);
    };
    const hydrate = async (
      forceActivity = false,
      boundary?: EventCursor,
      revision?: number,
    ): Promise<boolean> => {
      // 会话可能已在另一页面删除；先确认目录，再读取仍存在的线程。
      const sessionsResponse = await requestSessions(workspaceId);
      if (!alive || (revision !== undefined && revision !== resyncRevision)) return false;
      const nextSessions = latestCollection("sessions", sessionsResponse);
      if (!nextSessions) return false;
      setSessions(nextSessions);
      ensureSession(workspaceId, nextSessions, true);
      const selected = threadRef.current;
      const root = sessionRef.current;
      const [selectedResponse, rootResponse, tasksResponse] = await Promise.all([
        selected ? requestSnapshot(selected) : Promise.resolve(null),
        root && root !== selected ? requestSnapshot(root) : Promise.resolve(null),
        requestTasks(workspaceId),
      ]);
      if (!alive || (revision !== undefined && revision !== resyncRevision)) return false;
      if (selected !== threadRef.current || root !== sessionRef.current) return false;
      const nextSnapshot = latestSnapshot(selectedResponse);
      const nextRoot = latestSnapshot(rootResponse);
      // 被更新请求取代的快照尚未完成交接，不能确认 SSE 水位。
      if ((selected && !nextSnapshot) || (root && root !== selected && !nextRoot)) return false;
      if (nextSnapshot && threadRef.current === selected) {
        const projectedSnapshot = boundary
          ? replayResyncedRuns(nextSnapshot, resyncRuns, boundary)
          : nextSnapshot;
        setSnapshot((previous) => {
          const reconciled = reconcileActivity(previous, nextSnapshot, [], forceActivity).snapshot;
          return boundary ? replayResyncedRuns(reconciled, resyncRuns, boundary) : reconciled;
        });
        if (selected === root) setParentSnapshot(projectedSnapshot);
        if (projectedSnapshot?.status !== "running")
          setLiveTextState((old) => reduceLiveText(old, "settled"));
        if (nextSnapshot.status !== "running" || forceActivity) {
          setLiveEvents((items) =>
            boundary
              ? reconcileResyncedEvents(nextSnapshot, items, boundary)
              : reconcileActivity(null, nextSnapshot, items, forceActivity).live,
          );
        }
      }
      if (nextRoot && root === sessionRef.current) {
        setParentSnapshot(boundary ? replayResyncedRuns(nextRoot, resyncRuns, boundary) : nextRoot);
      }
      const nextTasks = latestCollection("tasks", tasksResponse);
      if (nextTasks) setTasks(nextTasks);
      return true;
    };
    const retryResync = () => {
      resyncFailures.current += 1;
      const delay = Math.min(1000 * 2 ** (resyncFailures.current - 1), 8000);
      resyncRetryTimer = setTimeout(() => {
        resyncRetryTimer = null;
        if (alive) setStreamEpoch((value) => value + 1);
      }, delay);
    };
    const startResync = (boundary: EventCursor, resetText: boolean) => {
      if (!resyncActive) resyncBaseline = cursor.current;
      resyncActive = true;
      const revision = ++resyncRevision;
      resyncBoundary = boundary;
      resyncRuns = [];
      cursor.current = boundary;
      if (resetText) setLiveTextState((old) => reduceLiveText(old, "resync"));
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
      void hydrate(true, boundary, revision)
        .then((complete) => {
          if (!alive || revision !== resyncRevision) return;
          if (!complete) {
            cursor.current = resyncBaseline;
            retryResync();
            return;
          }
          resyncFailures.current = 0;
          setStreamEpoch((value) => value + 1);
        })
        .catch((reason: unknown) => {
          if (!alive || revision !== resyncRevision) return;
          // 重读失败时从原游标重新订阅，直到权威状态完成交接。
          cursor.current = resyncBaseline;
          reportCurrent(reason);
          retryResync();
        });
    };
    const refreshCurrent = () => {
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
      refreshTimer.current = setTimeout(() => {
        void hydrate().catch(reportCurrent);
      }, 180);
    };
    const refreshTasks = () => {
      if (taskRefreshTimer.current) clearTimeout(taskRefreshTimer.current);
      taskRefreshTimer.current = setTimeout(() => {
        void requestTasks(workspaceId)
          .then((response) => {
            const next = latestCollection("tasks", response);
            if (alive && next) setTasks(next);
          })
          .catch(reportCurrent);
      }, 220);
    };
    const refreshTodos = () => {
      todoRevision += 1;
      if (todoRefreshTimer.current) return;
      const wait = Math.max(0, 2000 - (Date.now() - lastTodoRefresh.current));
      todoRefreshTimer.current = setTimeout(() => {
        todoRefreshTimer.current = null;
        lastTodoRefresh.current = Date.now();
        const revision = todoRevision;
        const selected = threadRef.current;
        const root = sessionRef.current;
        void Promise.all([
          // 待办只更新 todos 字段，不应使正在进行的完整快照请求失效。
          selected ? requestTodos(selected) : Promise.resolve(null),
          root && root !== selected ? requestTodos(root) : Promise.resolve(null),
        ])
          .then(([currentResponse, parentResponse]) => {
            if (!alive || revision !== todoRevision) return;
            const current = latestTodos(currentResponse);
            const parent = latestTodos(parentResponse);
            if (current && selected === threadRef.current) {
              setSnapshot((old) => (old ? { ...old, todos: current.todos } : current));
              if (selected === root)
                setParentSnapshot((old) => (old ? { ...old, todos: current.todos } : current));
            }
            if (parent && root === sessionRef.current)
              setParentSnapshot((old) => (old ? { ...old, todos: parent.todos } : parent));
          })
          .catch(reportCurrent);
      }, wait);
    };
    const after = cursor.current ? `${cursor.current.instanceId}:${cursor.current.seq}` : null;
    const close = connectEvents(
      workspaceId,
      after,
      (event) => {
        if (resyncRetryTimer) return;
        if (event.type === "stream.ready") {
          if (after === null) {
            // 首次订阅没有缓冲回放；从 ready 水位再读一次持久状态，接住快照与订阅之间的事件。
            startResync({ instanceId: event.instance_id, seq: event.seq }, false);
          }
          return;
        }
        if (event.type === "stream.resync_required") {
          startResync({ instanceId: event.instance_id, seq: event.seq }, true);
          return;
        }
        const nextCursor = advanceCursor(cursor.current, event);
        if (!nextCursor.accepted) return;
        cursor.current = nextCursor.cursor;
        const replayedAfterBoundary =
          resyncBoundary &&
          event.instance_id === resyncBoundary.instanceId &&
          event.seq > resyncBoundary.seq;
        if (
          event.type.startsWith("run.") &&
          event.thread_id &&
          (event.thread_id === threadRef.current || event.thread_id === sessionRef.current)
        ) {
          // 普通快照可能早于此事件；提交时按线程重放期间的运行转移。
          const owner = event.thread_id;
          const revision = (runRevision.get(owner) ?? 0) + 1;
          runRevision.set(owner, revision);
          const transitions = runTransitions.get(owner) ?? [];
          transitions.push({ revision, event });
          if (transitions.length > 512) transitions.shift();
          runTransitions.set(owner, transitions);
        }
        if (replayedAfterBoundary && event.type.startsWith("run.")) {
          resyncRuns.push(event);
        }
        if (event.type === "session.deleted" && event.thread_id === sessionRef.current) {
          const next = event.data.next_session_id;
          const nextId = typeof next === "string" ? next : null;
          viewGeneration.current += 1;
          sessionRef.current = nextId;
          threadRef.current = nextId;
          setSessionId(nextId);
          setThreadId(nextId);
          setSnapshot(null);
        }
        const selected = threadRef.current;
        const root = sessionRef.current;
        if (
          event.type === "run.started" &&
          ((event.thread_id === selected && !snapshotRef.current) ||
            (event.thread_id === root && !parentSnapshotRef.current))
        )
          refreshCurrent();
        if (event.thread_id === root && event.type.startsWith("run.")) {
          setParentSnapshot((old) => applyRunEvent(old, event));
        }
        if (belongsToSelectedTimeline(event, selected, root, tasksRef.current)) {
          setLiveEvents((items) =>
            items.some((item) => item.instance_id === event.instance_id && item.seq === event.seq)
              ? items
              : [...items.slice(-399), event],
          );
          if (selected !== root && event.thread_id === selected) {
            if (event.type === "task.running") {
              setLiveTextState((old) => reduceLiveText(old, "new-run"));
            } else if (
              [
                "task.idle",
                "task.failed",
                "task.paused",
                "task.stopped",
                "task.interrupted",
              ].includes(event.type)
            ) {
              setLiveTextState((old) => reduceLiveText(old, "settled"));
            }
          }
          if (event.type === "assistant.delta" && typeof event.data.delta === "string") {
            setLiveTextState((old) => reduceLiveText(old, "delta", event.data.delta as string));
          }
          if (
            event.type === "message.user" &&
            event.run_id &&
            typeof event.data.text === "string" &&
            selected
          ) {
            setSnapshot((old) =>
              appendUserMessage(old, selected, event.run_id!, event.data.text as string),
            );
          }
          if (event.type === "run.started" && event.run_id) {
            lastStartedRun.current = event.run_id;
            setLiveTextState((old) => reduceLiveText(old, "new-run"));
          }
          if (event.type.startsWith("run.")) {
            setSnapshot((old) => applyRunEvent(old, event));
          }
        }
        const plan = refreshPlan(event, selected, root);
        if (plan === "full") refreshCurrent();
        else if (plan === "tasks") refreshTasks();
        else if (plan === "todos") refreshTodos();
      },
      setConnection,
    );
    return () => {
      alive = false;
      close();
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
      if (taskRefreshTimer.current) clearTimeout(taskRefreshTimer.current);
      if (todoRefreshTimer.current) clearTimeout(todoRefreshTimer.current);
      if (resyncRetryTimer) clearTimeout(resyncRetryTimer);
      todoRefreshTimer.current = null;
    };
  }, [workspaceId, report, streamEpoch]);

  const selectWorkspace = (id: string) => {
    workspaceGeneration.current += 1;
    viewGeneration.current += 1;
    runRevision.clear();
    runTransitions.clear();
    resyncFailures.current = 0;
    workspaceRef.current = id;
    sessionRef.current = null;
    threadRef.current = null;
    setWorkspaceId(id);
    setSessionId(null);
    setThreadId(null);
    setSnapshot(null);
    cursor.current = null;
  };
  const selectSession = (id: string) => {
    viewGeneration.current += 1;
    sessionRef.current = id;
    threadRef.current = id;
    setSessionId(id);
    setThreadId(id);
  };
  const selectThread = (id: string) => {
    viewGeneration.current += 1;
    threadRef.current = id;
    setThreadId(id);
  };

  async function operate(work: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (reason) {
      report(reason);
      throw reason;
    } finally {
      setBusy(false);
    }
  }

  const addWorkspace = async (path: string, name?: string) =>
    operate(async () => {
      const value = await api.addWorkspace(path, name);
      const response = await requestShared("workspaces", api.workspaces);
      const next = latestShared(response);
      if (next) setWorkspaces(next);
      selectWorkspace(value.id);
    });
  const renameWorkspace = async (id: string, name: string) =>
    operate(async () => {
      await api.renameWorkspace(id, name);
      const response = await requestShared("workspaces", api.workspaces);
      const next = latestShared(response);
      if (next) setWorkspaces(next);
    });
  const newSession = async () =>
    operate(async () => {
      if (!workspaceId) return;
      const previousSessionId = sessionRef.current;
      const value = await api.createSession(workspaceId);
      const response = await requestSessions(workspaceId);
      const nextSessions = latestCollection("sessions", response);
      if (nextSessions) {
        setSessions(nextSessions);
        if (sessionRef.current === previousSessionId) selectSession(value.id);
      }
    });
  const renameSession = async (id: string, title: string) =>
    operate(async () => {
      await api.renameThread(id, title);
      if (workspaceId) {
        const response = await requestSessions(workspaceId);
        const nextSessions = latestCollection("sessions", response);
        if (nextSessions) setSessions(nextSessions);
      }
      setSnapshot((value) => (value?.thread_id === id ? { ...value, title } : value));
      setParentSnapshot((value) => (value?.thread_id === id ? { ...value, title } : value));
    });
  const deleteSession = async (id: string) =>
    operate(async () => {
      const result = await api.deleteSession(id);
      const [sessionsResponse, workspacesResponse] = await Promise.all([
        workspaceId ? requestSessions(workspaceId) : Promise.resolve(null),
        requestShared("workspaces", api.workspaces),
      ]);
      const nextSessions = latestCollection("sessions", sessionsResponse);
      if (nextSessions) setSessions(nextSessions);
      const nextWorkspaces = latestShared(workspacesResponse);
      if (nextWorkspaces) setWorkspaces(nextWorkspaces);
      if (sessionRef.current === id) {
        viewGeneration.current += 1;
        sessionRef.current = result.next_session_id;
        threadRef.current = result.next_session_id;
        setSessionId(result.next_session_id);
        setThreadId(result.next_session_id);
        setSnapshot(null);
      }
      if (result.warnings?.length) setError(result.warnings.join("；"));
    });
  const send = async (message: string, attachmentIds: string[] = [], messageId?: string) => {
    const owner = threadId;
    if (!owner) return;
    setError(null);
    try {
      await submitQueuedInput(owner, message, attachmentIds, messageId ?? crypto.randomUUID(), {
        enqueue: api.queueMessage,
        refresh: refreshThreadSnapshot,
        onRefreshError: report,
      });
    } catch (reason) {
      report(reason);
      throw reason;
    }
  };
  const steerQueuedMessage = async (id: string) =>
    operate(async () => {
      if (!threadId) return;
      await api.steerQueuedMessage(threadId, id);
      await refreshThreadSnapshot(threadId);
    });
  const editQueuedMessage = async (id: string, text: string) =>
    operate(async () => {
      if (!threadId) return;
      await api.editQueuedMessage(threadId, id, text);
      await refreshThreadSnapshot(threadId);
    });
  const removeQueuedMessage = async (id: string) =>
    operate(async () => {
      if (!threadId) return;
      await api.removeQueuedMessage(threadId, id);
      await refreshThreadSnapshot(threadId);
    });
  const stopSession = async () =>
    operate(async () => {
      if (!sessionId) return;
      await api.stopSession(sessionId);
      const markStopping = (old: ThreadSnapshot | null): ThreadSnapshot | null =>
        old?.thread_id === sessionId
          ? {
              ...old,
              status: "stopping",
              active_run: old.active_run ? { ...old.active_run, status: "stopping" } : null,
            }
          : old;
      setParentSnapshot(markStopping);
      if (threadId === sessionId) setSnapshot(markStopping);
      if (workspaceId) {
        const response = await requestTasks(workspaceId);
        const nextTasks = latestCollection("tasks", response);
        if (nextTasks) setTasks(nextTasks);
      }
    });
  const resumeSession = async () =>
    operate(async () => {
      if (!sessionId) return;
      const owner = sessionId;
      await api.resumeRun(owner);
      await refreshThreadSnapshot(owner);
    });
  const uploadAttachment = async (file: File): Promise<Attachment> => {
    if (!threadId) throw new Error("请先选择会话");
    return api.uploadAttachment(threadId, file);
  };
  const discardAttachment = async (id: string) => {
    if (!threadId) return;
    await api.discardAttachment(threadId, id);
  };
  const compactCurrent = async (focus = "") =>
    operate(async () => {
      if (!threadId) return;
      await api.compactThread(threadId, focus.trim() || null);
      await refreshThreadSnapshot(threadId);
    });
  const activateSkill = async (name: string) =>
    operate(async () => {
      if (!threadId) return;
      await api.activateSkill(threadId, name);
      await refreshThreadSnapshot(threadId);
    });
  const approve = async (decisions: ApprovalDecision[], grants: ApprovalGrant[] = []) =>
    operate(async () => {
      if (!threadId || !snapshot?.pending_approval) return;
      await api.approve(threadId, snapshot.pending_approval.checkpoint_id, decisions, grants);
      await refreshThreadSnapshot(threadId);
    });
  const setTrust = async (level: TrustLevel) =>
    operate(async () => {
      if (!threadId) return;
      await api.setTrust(threadId, level);
      await refreshThreadSnapshot(threadId);
    });
  const setThreadModel = async (name: string) =>
    operate(async () => {
      if (!threadId) return;
      const next = await api.setThreadModel(threadId, name);
      snapshotRequests.begin(threadId);
      commitThreadSnapshot(threadId, next);
    });
  const setDefaultTrust = async (level: TrustLevel) =>
    operate(async () => {
      const next = await api.updateSettings({ default_trust: level });
      collectionRequests.begin("shared:settings");
      setSettings(next);
    });
  const updateSettings = async (patch: Partial<SettingsResponse>) =>
    operate(async () => {
      const next = await api.updateSettings(patch);
      collectionRequests.begin("shared:settings");
      setSettings(next);
    });
  const spawnTask = async (input: {
    role: string;
    prompt: string;
    title?: string;
    worktree_enabled?: boolean;
  }) =>
    operate(async () => {
      if (!threadId || !workspaceId) return;
      await api.createTask({ parent_thread_id: threadId, ...input });
      const response = await requestTasks(workspaceId);
      const nextTasks = latestCollection("tasks", response);
      if (nextTasks) setTasks(nextTasks);
    });
  const taskAction = async (taskId: string, action: string, data: Record<string, unknown> = {}) => {
    let result: TaskActionResult = {};
    await operate(async () => {
      result = await api.taskAction(taskId, action, data);
      if (workspaceId) {
        const response = await requestTasks(workspaceId);
        const nextTasks = latestCollection("tasks", response);
        if (nextTasks) setTasks(nextTasks);
      }
      if (threadId) await refreshThreadSnapshot(threadId);
    });
    return result;
  };

  return {
    status: statusState,
    settings,
    workspaces,
    sessions,
    tasks,
    snapshot,
    parentSnapshot,
    workspaceId,
    sessionId,
    threadId,
    liveEvents,
    liveText: liveTextState.text,
    streamResynced: liveTextState.resynced,
    connection,
    loading,
    busy,
    error,
    selectWorkspace,
    selectSession,
    selectThread,
    clearError: () => setError(null),
    refresh,
    addWorkspace,
    renameWorkspace,
    newSession,
    renameSession,
    deleteSession,
    send,
    steerQueuedMessage,
    editQueuedMessage,
    removeQueuedMessage,
    stopSession,
    resumeSession,
    uploadAttachment,
    discardAttachment,
    compactCurrent,
    activateSkill,
    approve,
    setTrust,
    setThreadModel,
    setDefaultTrust,
    updateSettings,
    spawnTask,
    taskAction,
  };
}
