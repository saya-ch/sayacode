import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { advanceCursor, connectEvents, type EventCursor } from "../api/events";
import { reconcileActivity } from "./activity";
import { refreshPlan } from "./eventPolicy";
import { emptyLiveText, reduceLiveText } from "./liveText";
import { appendUserMessage } from "./messages";
import type {
  ApprovalDecision,
  ApprovalGrant,
  Session,
  SettingsResponse,
  StatusResponse,
  StreamEvent,
  Task,
  TaskActionResult,
  ThreadSnapshot,
  Workspace,
} from "../api/types";

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
  send(message: string): Promise<void>;
  approve(decisions: ApprovalDecision[], grants?: ApprovalGrant[]): Promise<void>;
  setTrust(level: "read_only" | "ask" | "jev" | "full"): Promise<void>;
  setDefaultTrust(level: "read_only" | "ask" | "jev" | "full"): Promise<void>;
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
  const threadRef = useRef(threadId);
  const sessionRef = useRef(sessionId);
  const tasksRef = useRef(tasks);
  threadRef.current = threadId;
  sessionRef.current = sessionId;
  tasksRef.current = tasks;

  const report = useCallback((reason: unknown) => setError(messageFrom(reason)), []);

  const refresh = useCallback(async () => {
    const [newWorkspaces, newSettings, newStatus, nextSessions, nextTasks, nextThread, nextParent] =
      await Promise.all([
        api.workspaces(),
        api.settings(),
        api.status(),
        workspaceId ? api.sessions(workspaceId) : Promise.resolve([]),
        workspaceId ? api.tasks(workspaceId) : Promise.resolve([]),
        threadId ? api.snapshot(threadId) : Promise.resolve(null),
        sessionId && sessionId !== threadId ? api.snapshot(sessionId) : Promise.resolve(null),
      ]);
    setWorkspaces(newWorkspaces);
    setSettings(newSettings);
    setStatusState(newStatus);
    setSessions(nextSessions);
    setTasks(nextTasks);
    if (nextThread && threadRef.current === threadId) {
      setSnapshot(nextThread);
      setLiveTextState(emptyLiveText);
      setLiveEvents((items) => reconcileActivity(null, nextThread, items, true).live);
    }
    if (sessionRef.current === sessionId)
      setParentSnapshot(nextParent ?? (threadId === sessionId ? nextThread : null));
  }, [workspaceId, threadId, sessionId]);

  useEffect(() => {
    let alive = true;
    Promise.all([api.workspaces(), api.settings()])
      .then(([items, value]) => {
        if (!alive) return;
        setWorkspaces(items);
        setSettings(value);
        const chosen =
          items.find((item) => item.id === workspaceId) ??
          items.find((item) => item.id === status.workspace_id) ??
          items[0];
        if (chosen && chosen.id !== workspaceId) setWorkspaceId(chosen.id);
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
    Promise.all([api.sessions(workspaceId), api.tasks(workspaceId)])
      .then(([nextSessions, nextTasks]) => {
        if (!alive) return;
        setSessions(nextSessions);
        setTasks(nextTasks);
        const workspace = workspaces.find((item) => item.id === workspaceId);
        const chosen =
          nextSessions.find((item) => item.id === sessionId) ??
          nextSessions.find((item) => item.id === workspace?.active_session_id) ??
          nextSessions[0];
        if (chosen && chosen.id !== sessionId) {
          setSessionId(chosen.id);
          setThreadId(chosen.id);
        } else if (!chosen) {
          setSessionId(null);
          setThreadId(null);
          setSnapshot(null);
        }
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
    api
      .snapshot(threadId)
      .then((value) => {
        if (!alive) return;
        setSnapshot(value);
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
    api
      .snapshot(sessionId)
      .then((value) => {
        if (alive) setParentSnapshot(value);
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
    if (!workspaceId) return;
    let alive = true;
    setConnection("connecting");
    const hydrate = async (forceActivity = false) => {
      const selected = threadRef.current;
      const root = sessionRef.current;
      const [nextSnapshot, nextRoot, nextSessions, nextTasks] = await Promise.all([
        selected ? api.snapshot(selected) : Promise.resolve(null),
        root && root !== selected ? api.snapshot(root) : Promise.resolve(null),
        api.sessions(workspaceId),
        api.tasks(workspaceId),
      ]);
      if (!alive) return;
      if (nextSnapshot && threadRef.current === selected) {
        setSnapshot(
          (previous) => reconcileActivity(previous, nextSnapshot, [], forceActivity).snapshot,
        );
        if (selected === root) setParentSnapshot(nextSnapshot);
        if (nextSnapshot.status !== "running")
          setLiveTextState((old) => reduceLiveText(old, "settled"));
        if (nextSnapshot.status !== "running" || forceActivity) {
          setLiveEvents(
            (items) => reconcileActivity(null, nextSnapshot, items, forceActivity).live,
          );
        }
      }
      if (nextRoot && root === sessionRef.current) setParentSnapshot(nextRoot);
      setSessions(nextSessions);
      setTasks(nextTasks);
    };
    const refreshCurrent = () => {
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
      refreshTimer.current = setTimeout(() => {
        void hydrate().catch(report);
      }, 180);
    };
    const refreshTasks = () => {
      if (taskRefreshTimer.current) clearTimeout(taskRefreshTimer.current);
      taskRefreshTimer.current = setTimeout(() => {
        void api
          .tasks(workspaceId)
          .then((next) => {
            if (alive) setTasks(next);
          })
          .catch(report);
      }, 220);
    };
    const refreshTodos = () => {
      if (todoRefreshTimer.current) return;
      const wait = Math.max(0, 2000 - (Date.now() - lastTodoRefresh.current));
      todoRefreshTimer.current = setTimeout(() => {
        todoRefreshTimer.current = null;
        lastTodoRefresh.current = Date.now();
        const selected = threadRef.current;
        const root = sessionRef.current;
        void Promise.all([
          selected ? api.snapshot(selected) : Promise.resolve(null),
          root && root !== selected ? api.snapshot(root) : Promise.resolve(null),
        ])
          .then(([current, parent]) => {
            if (!alive) return;
            if (current && selected === threadRef.current) {
              setSnapshot((old) => (old ? { ...old, todos: current.todos } : current));
              if (selected === root)
                setParentSnapshot((old) => (old ? { ...old, todos: current.todos } : current));
            }
            if (parent && root === sessionRef.current)
              setParentSnapshot((old) => (old ? { ...old, todos: parent.todos } : parent));
          })
          .catch(report);
      }, wait);
    };
    const after = cursor.current ? `${cursor.current.instanceId}:${cursor.current.seq}` : null;
    const close = connectEvents(
      workspaceId,
      after,
      (event) => {
        if (event.type === "stream.resync_required") {
          cursor.current = { instanceId: event.instance_id, seq: event.seq };
          setLiveTextState((old) => reduceLiveText(old, "resync"));
          if (refreshTimer.current) clearTimeout(refreshTimer.current);
          void hydrate(true)
            .catch(report)
            .finally(() => {
              if (alive) setStreamEpoch((value) => value + 1);
            });
          return;
        }
        const nextCursor = advanceCursor(cursor.current, event);
        if (!nextCursor.accepted) return;
        cursor.current = nextCursor.cursor;
        const selected = threadRef.current;
        if (
          event.thread_id === selected ||
          (event.task_id &&
            tasksRef.current.some(
              (task) => task.id === event.task_id && task.thread_id === selected,
            ))
        ) {
          setLiveEvents((items) => [...items.slice(-399), event]);
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
            setSnapshot((old) =>
              old
                ? {
                    ...old,
                    status: "running",
                    active_run: {
                      run_id: event.run_id!,
                      started_at: event.at ?? new Date().toISOString(),
                      status: "running",
                    },
                  }
                : old,
            );
          }
          if (["run.completed", "run.failed", "run.paused", "run.stopped"].includes(event.type)) {
            const status = event.type.slice(4) as ThreadSnapshot["status"];
            setSnapshot((old) => (old ? { ...old, status, active_run: null } : old));
          }
        }
        const plan = refreshPlan(event);
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
      todoRefreshTimer.current = null;
    };
  }, [workspaceId, report, streamEpoch]);

  const selectWorkspace = (id: string) => {
    setWorkspaceId(id);
    setSessionId(null);
    setThreadId(null);
    setSnapshot(null);
    cursor.current = null;
  };
  const selectSession = (id: string) => {
    setSessionId(id);
    setThreadId(id);
  };
  const selectThread = (id: string) => setThreadId(id);

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
      setWorkspaces(await api.workspaces());
      selectWorkspace(value.id);
    });
  const renameWorkspace = async (id: string, name: string) =>
    operate(async () => {
      await api.renameWorkspace(id, name);
      setWorkspaces(await api.workspaces());
    });
  const newSession = async () =>
    operate(async () => {
      if (!workspaceId) return;
      const value = await api.createSession(workspaceId);
      setSessions(await api.sessions(workspaceId));
      selectSession(value.id);
    });
  const renameSession = async (id: string, title: string) =>
    operate(async () => {
      await api.renameThread(id, title);
      if (workspaceId) setSessions(await api.sessions(workspaceId));
      setSnapshot((value) => (value?.thread_id === id ? { ...value, title } : value));
      setParentSnapshot((value) => (value?.thread_id === id ? { ...value, title } : value));
    });
  const send = async (message: string) =>
    operate(async () => {
      if (!threadId) return;
      const child = tasks.find((task) => task.thread_id === threadId);
      if (child) {
        await api.taskAction(child.id, "followup", { message });
      } else {
        const receipt = await api.startRun(threadId, message);
        setSnapshot((value) => {
          const next = appendUserMessage(value, threadId, receipt.run_id, message);
          return next
            ? {
                ...next,
                status: "running",
                active_run: {
                  run_id: receipt.run_id,
                  started_at: new Date().toISOString(),
                  status: "running",
                },
              }
            : next;
        });
      }
    });
  const approve = async (decisions: ApprovalDecision[], grants: ApprovalGrant[] = []) =>
    operate(async () => {
      if (!threadId || !snapshot?.pending_approval) return;
      await api.approve(threadId, snapshot.pending_approval.checkpoint_id, decisions, grants);
      setSnapshot(await api.snapshot(threadId));
    });
  const setTrust = async (level: "read_only" | "ask" | "jev" | "full") =>
    operate(async () => {
      if (!threadId) return;
      await api.setTrust(threadId, level);
      setSnapshot(await api.snapshot(threadId));
    });
  const setDefaultTrust = async (level: "read_only" | "ask" | "jev" | "full") =>
    operate(async () => {
      setSettings(await api.updateSettings({ default_trust: level }));
    });
  const updateSettings = async (patch: Partial<SettingsResponse>) =>
    operate(async () => {
      setSettings(await api.updateSettings(patch));
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
      setTasks(await api.tasks(workspaceId));
    });
  const taskAction = async (taskId: string, action: string, data: Record<string, unknown> = {}) => {
    let result: TaskActionResult = {};
    await operate(async () => {
      result = await api.taskAction(taskId, action, data);
      if (workspaceId) setTasks(await api.tasks(workspaceId));
      if (threadId) setSnapshot(await api.snapshot(threadId));
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
    send,
    approve,
    setTrust,
    setDefaultTrust,
    updateSettings,
    spawnTask,
    taskAction,
  };
}
