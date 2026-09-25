import { lazy, Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  ArrowUp,
  ChevronDown,
  ChevronRight,
  CircleCheck,
  CircleDashed,
  CirclePause,
  Clock3,
  ListTodo,
  Menu,
  PanelRight,
  Play,
  Radio,
  ShieldAlert,
  Square,
  Waypoints,
} from "lucide-react";
import type { AgentMessage, Attachment, Todo } from "../api/types";
import type { WorkspaceState } from "../state/useWorkspace";
import { agentColor, displaySessionTitle, elapsed, roleLabel, statusLabel } from "../lib/format";
import { descendantTasks } from "../lib/tasks";
import shared from "../styles/shared.module.css";
import styles from "./Conversation.module.css";
import { useI18n } from "../i18n";
import { ComposerActions } from "./ComposerActions";
import { InlineActivity } from "./InlineActivity";
import { QueueDock } from "./QueueDock";

const Trajectory = lazy(() =>
  import("./Trajectory").then((module) => ({ default: module.Trajectory })),
);
const MarkdownContent = lazy(() =>
  import("./MarkdownContent").then((module) => ({ default: module.MarkdownContent })),
);

export async function validateTextAttachment(file: File): Promise<"ok" | "too_large" | "not_text"> {
  if (file.size > 32 * 1024 * 1024) return "too_large";
  if (/^(image|audio|video)\//.test(file.type) || /^(application\/(pdf|zip|gzip))$/.test(file.type))
    return "not_text";
  try {
    const content = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
    return content.includes("\0") ? "not_text" : "ok";
  } catch {
    return "not_text";
  }
}

interface ConversationProps {
  state: WorkspaceState;
  onOpenSidebar: () => void;
  onOpenInspector: () => void;
  onOpenApproval: () => void;
  onOpenProducts: () => void;
}

function useNow(active: boolean): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

function ChatMessage({
  message,
  agentName,
  color,
}: {
  message: AgentMessage;
  agentName: string;
  color: string;
}) {
  const { t, language } = useI18n();
  if (message.role === "system") return null;
  const user = message.role === "human" || message.role === "user";
  const tool = message.role === "tool";
  const inbox = message.role === "agent_inbox";
  if (tool) {
    const args = message.tool_input ?? {};
    const target = ["path", "file_path", "target_path", "command", "pattern", "query", "url"]
      .map((key) => args[key])
      .find((value): value is string => typeof value === "string" && Boolean(value));
    const failed = message.status === "error";
    return (
      <article className={styles.toolInline} data-status={failed ? "failed" : "completed"}>
        <details>
          <summary>
            <span>{failed ? t("失败") : t("已完成")}</span>
            <strong>{message.tool_name || t("工具调用")}</strong>
            {target && <code title={target}>{target}</code>}
            <ChevronRight size={14} aria-hidden="true" />
          </summary>
          {message.tool_input && (
            <div className={styles.toolDetail}>
              <small>{t("调用参数")}</small>
              <pre>{JSON.stringify(message.tool_input, null, 2)}</pre>
            </div>
          )}
          <div className={styles.toolDetail}>
            <small>{t("工具结果")}</small>
            <pre>{message.text}</pre>
          </div>
        </details>
      </article>
    );
  }
  return (
    <article
      className={`${styles.message} ${user ? styles.userMessage : ""} ${inbox ? styles.inboxMessage : ""}`}
    >
      <div
        className={styles.avatar}
        style={!user ? ({ "--avatar-color": color } as React.CSSProperties) : undefined}
        aria-hidden="true"
      >
        {user ? t("你") : inbox ? "N" : agentName.slice(0, 1)}
      </div>
      <div className={styles.messageBody}>
        <div className={styles.messageHeader}>
          <strong>{user ? t("你") : inbox ? t("子 Agent 通知") : agentName}</strong>
          {message.created_at && (
            <time dateTime={message.created_at}>
              {new Date(message.created_at).toLocaleTimeString(
                language === "en" ? "en-US" : "zh-CN",
                { hour12: false },
              )}
            </time>
          )}
        </div>
        <div className={styles.markdown}>
          {inbox ? (
            <p>{message.text}</p>
          ) : (
            <Suspense fallback={<p>{message.text}</p>}>
              <MarkdownContent text={message.text} />
            </Suspense>
          )}
        </div>
      </div>
    </article>
  );
}

function TodoDock({ todos, label = "计划" }: { todos: Todo[]; label?: string }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  if (!todos.length) return null;
  const done = todos.filter((item) => item.status === "completed").length;
  const active = todos.find((item) => item.status === "in_progress");
  return (
    <section className={styles.todoDock} aria-label={t("当前计划")}>
      <button
        className={styles.todoToggle}
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        <ListTodo size={15} aria-hidden="true" />
        <strong>{t(label)}</strong>
        <span>
          {done}/{todos.length}
        </span>
        <span className={styles.todoCurrent}>
          {active?.content ?? t(done === todos.length ? "全部完成" : "等待开始")}
        </span>
        {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
      </button>
      {open && (
        <ol className={styles.todoList}>
          {todos.map((todo) => (
            <li key={todo.id} data-status={todo.status}>
              {todo.status === "completed" ? (
                <CircleCheck size={14} />
              ) : todo.status === "in_progress" ? (
                <CircleDashed size={14} />
              ) : (
                <CirclePause size={14} />
              )}
              <span>{todo.content}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

export function Conversation({
  state,
  onOpenSidebar,
  onOpenInspector,
  onOpenApproval,
  onOpenProducts,
}: ConversationProps) {
  const { t } = useI18n();
  const [view, setView] = useState<"chat" | "trajectory">("chat");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [attachmentsByThread, setAttachmentsByThread] = useState<Record<string, Attachment[]>>({});
  const [uploadsByThread, setUploadsByThread] = useState<Record<string, number>>({});
  const [submitting, setSubmitting] = useState(false);
  const submittingRef = useRef(false);
  const selectedThread = useRef(state.threadId);
  const uploadGeneration = useRef(0);
  const retryIds = useRef(new Map<string, { signature: string; id: string }>());
  const chatScroll = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  const composer = useRef<HTMLTextAreaElement>(null);
  const selectedTask = state.tasks.find((task) => task.thread_id === state.threadId);
  const name = selectedTask?.title || "SAYA";
  const color = selectedTask ? agentColor(selectedTask.thread_id) : "var(--agent-saya)";
  const currentStatus = state.snapshot?.status ?? "idle";
  const threadId = state.threadId;
  const draft = threadId ? (drafts[threadId] ?? "") : "";
  const attachments = threadId ? (attachmentsByThread[threadId] ?? []) : [];
  const uploading = threadId ? (uploadsByThread[threadId] ?? 0) > 0 : false;
  const hasModel = Boolean(state.snapshot?.effective_model || state.settings?.active_profile);
  const running = currentStatus === "running";
  const rootSnapshot =
    state.parentSnapshot?.thread_id === state.sessionId
      ? state.parentSnapshot
      : state.snapshot?.thread_id === state.sessionId
        ? state.snapshot
        : null;
  const childrenRunning = descendantTasks(state.tasks, state.sessionId).some((task) =>
    ["pending", "running", "stopping"].includes(task.status),
  );
  const canStopFamily =
    Boolean(state.sessionId) &&
    (["running", "stopping"].includes(rootSnapshot?.status ?? "") || childrenRunning);
  const now = useNow(running);
  const duration = elapsed(state.snapshot?.active_run?.started_at, now);
  const canSend = Boolean(
    threadId &&
    (draft.trim() || attachments.length > 0) &&
    !state.busy &&
    !submitting &&
    !uploading &&
    hasModel,
  );

  useLayoutEffect(() => {
    selectedThread.current = threadId;
    uploadGeneration.current += 1;
  }, [threadId]);

  const updateDraft = (value: string) => {
    if (!threadId) return;
    retryIds.current.delete(threadId);
    setDrafts((current) => ({ ...current, [threadId]: value }));
  };

  const visibleMessages = useMemo(
    () =>
      state.snapshot?.messages.filter(
        (message) =>
          message.role !== "system" &&
          !(["ai", "assistant"].includes(message.role) && !message.text && message.has_tool_calls),
      ) ?? [],
    [state.snapshot?.messages],
  );
  const rowCount = visibleMessages.length + (state.liveText ? 1 : 0);
  const messageVirtualizer = useVirtualizer({
    count: rowCount,
    getScrollElement: () => chatScroll.current,
    estimateSize: () => 120,
    getItemKey: (index) => visibleMessages[index]?.id ?? "live-response",
    overscan: 5,
    useFlushSync: false,
  });

  useEffect(() => {
    if (view !== "chat" || !rowCount || !followLatest.current) return;
    const frame = requestAnimationFrame(() =>
      messageVirtualizer.scrollToIndex(rowCount - 1, { align: "end" }),
    );
    return () => cancelAnimationFrame(frame);
  }, [rowCount, state.liveText, view, messageVirtualizer]);

  useEffect(() => {
    if (view !== "chat" || !followLatest.current) return;
    const frame = requestAnimationFrame(() => {
      const element = chatScroll.current;
      if (element) element.scrollTop = element.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [state.liveEvents.length, view]);

  const submit = () => {
    if (!threadId || submittingRef.current) return;
    const text = draft.trim();
    if ((!text && !attachments.length) || !canSend) return;
    const selected = attachments;
    const signature = JSON.stringify([text, selected.map((item) => item.id)]);
    const retry = retryIds.current.get(threadId);
    const messageId = retry?.signature === signature ? retry.id : crypto.randomUUID();
    retryIds.current.set(threadId, { signature, id: messageId });
    submittingRef.current = true;
    setSubmitting(true);
    setDrafts((current) => ({ ...current, [threadId]: "" }));
    setAttachmentsByThread((current) => ({ ...current, [threadId]: [] }));
    void state
      .send(
        text,
        selected.map((item) => item.id),
        messageId,
      )
      .then(() => {
        if (retryIds.current.get(threadId)?.id === messageId) retryIds.current.delete(threadId);
      })
      .catch(() => {
        setDrafts((current) => ({ ...current, [threadId]: text }));
        setAttachmentsByThread((current) => ({ ...current, [threadId]: selected }));
      })
      .finally(() => {
        submittingRef.current = false;
        setSubmitting(false);
      });
  };

  const addFiles = async (files: FileList | File[]) => {
    if (!threadId) return;
    const owner = threadId;
    const generation = uploadGeneration.current;
    const discard = state.discardAttachment;
    retryIds.current.delete(owner);
    setUploadsByThread((current) => ({ ...current, [owner]: (current[owner] ?? 0) + 1 }));
    try {
      const chosen = Array.from(files);
      for (const file of chosen) {
        if (selectedThread.current !== owner || uploadGeneration.current !== generation) return;
        const result = await validateTextAttachment(file);
        if (result !== "ok") {
          throw new Error(
            `${file.name}：${t(
              result === "too_large" ? "单个文件不能超过 32 MiB" : "只支持 UTF-8 文本文件",
            )}`,
          );
        }
      }
      for (const file of chosen) {
        if (selectedThread.current !== owner || uploadGeneration.current !== generation) break;
        const uploaded = await state.uploadAttachment(file);
        if (selectedThread.current !== owner || uploadGeneration.current !== generation) {
          await discard(uploaded.id).catch(() => {});
          continue;
        }
        setAttachmentsByThread((current) => ({
          ...current,
          [owner]: [...(current[owner] ?? []), uploaded],
        }));
      }
    } finally {
      setUploadsByThread((current) => ({
        ...current,
        [owner]: Math.max(0, (current[owner] ?? 1) - 1),
      }));
    }
  };
  const removeAttachment = async (id: string) => {
    if (!threadId) return;
    const owner = threadId;
    await state.discardAttachment(id);
    retryIds.current.delete(owner);
    setAttachmentsByThread((current) => ({
      ...current,
      [owner]: (current[owner] ?? []).filter((item) => item.id !== id),
    }));
  };

  return (
    <main className={styles.main} aria-label={t("Agent 对话")}>
      <header className={styles.header}>
        <button
          className={`${shared.iconButton} ${styles.mobileButton} ${styles.sidebarButton}`}
          data-control="open-sidebar"
          onClick={onOpenSidebar}
          aria-label={t("打开工作区列表")}
        >
          <Menu size={19} />
        </button>
        <div className={styles.headerTitle}>
          <div className={styles.breadcrumb}>
            <span>{t("工作区")}</span>
            <ChevronRight size={13} />
            <span>
              {state.workspaces.find((item) => item.id === state.workspaceId)?.name ?? t("未选择")}
            </span>
            {selectedTask && (
              <>
                <ChevronRight size={13} />
                <span>SAYA</span>
              </>
            )}
          </div>
          <h1>
            <span className={styles.agentStripe} style={{ background: color }} />
            {selectedTask ? name : displaySessionTitle(state.snapshot?.title, t)}
          </h1>
        </div>
        <div className={styles.headerActions}>
          <span className={styles.modelChip}>
            {state.snapshot?.effective_model || t("模型未配置")}
          </span>
          <span className={styles.runBadge} data-status={currentStatus}>
            <span className={shared.statusDot} data-status={currentStatus} />
            {statusLabel(currentStatus, t)}
            {running && duration && <b>{duration}</b>}
          </span>
          {canStopFamily && (
            <button
              type="button"
              className={styles.runAction}
              onClick={() => void state.stopSession().catch(() => {})}
              disabled={state.busy || rootSnapshot?.status === "stopping"}
              aria-label={t("停止本会话及全部子 Agent")}
            >
              <Square size={14} aria-hidden="true" /> {t("停止")}
            </button>
          )}
          {rootSnapshot?.status === "stopped" && rootSnapshot.resume_available && (
            <button
              type="button"
              className={styles.runAction}
              onClick={() => void state.resumeSession().catch(() => {})}
              disabled={state.busy}
            >
              <Play size={14} aria-hidden="true" />
              {t(rootSnapshot.pending_steps ? "继续原运行" : "处理排队消息")}
            </button>
          )}
          <button
            className={`${shared.iconButton} ${styles.mobileButton} ${styles.inspectorButton}`}
            data-control="open-inspector"
            onClick={onOpenInspector}
            aria-label={t("打开 Agent 检查器")}
          >
            <PanelRight size={19} />
          </button>
        </div>
      </header>

      <div className={styles.viewTabs} role="tablist" aria-label={t("对话视图")}>
        <button
          role="tab"
          aria-selected={view === "chat"}
          className={view === "chat" ? styles.selectedTab : ""}
          onClick={() => setView("chat")}
        >
          {t("对话")}
        </button>
        <button
          role="tab"
          aria-selected={view === "trajectory"}
          className={view === "trajectory" ? styles.selectedTab : ""}
          onClick={() => setView("trajectory")}
        >
          {t("运行轨迹")}
          <span>
            {(state.snapshot?.activity?.length ?? 0) +
              state.liveEvents.filter((item) => item.type !== "assistant.delta").length}
          </span>
        </button>
        {selectedTask && (
          <span className={styles.childContext} style={{ color }}>
            {roleLabel(selectedTask.role, t)} Agent · {selectedTask.title}
          </span>
        )}
      </div>

      <div className={styles.contentArea}>
        {view === "chat" ? (
          <div
            ref={chatScroll}
            className={styles.chatScroll}
            role="tabpanel"
            aria-label={t("对话")}
            onScroll={(event) => {
              const element = event.currentTarget;
              followLatest.current =
                element.scrollHeight - element.scrollTop - element.clientHeight < 120;
            }}
          >
            <div className={styles.chatContent}>
              {!state.threadId ? (
                <div className={styles.welcome}>
                  <div className={styles.welcomeGlyph}>S</div>
                  <span className={shared.eyebrow}>SAYACODE</span>
                  <h2>{t("开始一个工作会话")}</h2>
                  <p>{t("选择左侧工作区并新建会话，运行过程会在这里持续更新。")}</p>
                </div>
              ) : !state.snapshot ? (
                <div className={shared.empty}>
                  <span>{t("正在读取会话…")}</span>
                </div>
              ) : state.snapshot.messages.length === 0 && !state.liveText ? (
                <div className={styles.welcome}>
                  <div className={styles.welcomeGlyph}>S</div>
                  <span className={shared.eyebrow}>SAYACODE</span>
                  <h2>{t("从一个具体任务开始")}</h2>
                  <p>{t("工具与子 Agent 的活动会显示在对话中；展开运行轨迹可查看完整细节。")}</p>
                  <div className={styles.welcomeMeta}>
                    <span>
                      <Waypoints size={14} /> {t("多 Agent 协作")}
                    </span>
                    <span>
                      <Radio size={14} /> {t("实时事件")}
                    </span>
                  </div>
                </div>
              ) : (
                <div
                  className={styles.virtualCanvas}
                  style={{ height: messageVirtualizer.getTotalSize() }}
                >
                  {messageVirtualizer.getVirtualItems().map((item) => (
                    <div
                      key={item.key}
                      ref={messageVirtualizer.measureElement}
                      data-index={item.index}
                      className={styles.virtualRow}
                      style={{ transform: `translateY(${item.start}px)` }}
                    >
                      {visibleMessages[item.index] ? (
                        <ChatMessage
                          message={visibleMessages[item.index]!}
                          agentName={name}
                          color={color}
                        />
                      ) : (
                        <article className={styles.message} aria-live="polite">
                          <div
                            className={styles.avatar}
                            style={{ "--avatar-color": color } as React.CSSProperties}
                            aria-hidden="true"
                          >
                            {name.slice(0, 1)}
                          </div>
                          <div className={styles.messageBody}>
                            <div className={styles.messageHeader}>
                              <strong>{name}</strong>
                              <span className={styles.streamingTag}>{t("正在回复")}</span>
                            </div>
                            <div className={styles.streamingText}>{state.liveText}</div>
                          </div>
                        </article>
                      )}
                    </div>
                  ))}
                </div>
              )}
              <InlineActivity
                activity={state.snapshot?.activity ?? []}
                liveEvents={state.liveEvents}
                agentName={name}
                agentColor={color}
                onOpenTrajectory={() => setView("trajectory")}
              />
            </div>
          </div>
        ) : (
          <div role="tabpanel" className={styles.trajectoryPane}>
            <Suspense fallback={<div className={shared.empty}>{t("正在读取轨迹…")}</div>}>
              <Trajectory
                activity={state.snapshot?.activity ?? []}
                liveEvents={state.liveEvents}
                agentName={name}
                agentColor={color}
              />
            </Suspense>
          </div>
        )}
      </div>

      <div className={styles.bottom}>
        <div className={styles.bottomExtras}>
          {!hasModel && (
            <button className={styles.modelCallout} onClick={onOpenProducts}>
              <span>{t("尚未配置模型，先添加一个连接。")}</span>
              <strong>{t("打开模型设置")}</strong>
              <ChevronRight size={15} />
            </button>
          )}
          {state.streamResynced && (
            <div className={styles.streamNotice} role="status">
              {t("实时片段已重同步，完整回答以检查点为准。")}
            </div>
          )}
          {state.snapshot?.pending_approval && (
            <button className={styles.approvalCallout} onClick={onOpenApproval}>
              <ShieldAlert size={18} />
              <span>
                <strong>
                  {t("{count} 个操作等待批准", {
                    count: state.snapshot.pending_approval.actions.length,
                  })}
                </strong>
                <small>{t("逐项查看工具名称与参数后继续")}</small>
              </span>
              <ChevronRight size={16} />
            </button>
          )}
          <TodoDock
            todos={
              state.parentSnapshot?.thread_id === state.sessionId
                ? state.parentSnapshot.todos
                : state.snapshot?.thread_id === state.sessionId
                  ? state.snapshot.todos
                  : []
            }
            label="SAYA 计划"
          />
          {selectedTask && Boolean(state.snapshot?.todos.length) && (
            <TodoDock todos={state.snapshot?.todos ?? []} label={t("{name} 计划", { name })} />
          )}
          <QueueDock
            rows={state.snapshot?.queued_messages ?? []}
            busy={state.busy}
            canSteer={
              !["paused", "stopping", "stopped", "interrupted"].includes(currentStatus) &&
              !state.snapshot?.pending_approval
            }
            onSteer={state.steerQueuedMessage}
            onEdit={state.editQueuedMessage}
            onDelete={state.removeQueuedMessage}
          />
        </div>
        <div className={styles.composerFrame}>
          <label className={styles.composerLabel} htmlFor="agent-composer">
            {t("发送给")} <strong style={{ color }}>{name}</strong>
            {selectedTask && <span>· {t("子 Agent 后续指令")}</span>}
          </label>
          <textarea
            id="agent-composer"
            ref={composer}
            value={draft}
            onChange={(event) => updateDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                submit();
              }
            }}
            placeholder={t(
              !state.threadId
                ? "请先选择会话"
                : running || state.snapshot?.pending_approval
                  ? "运行中发送会先进入队列"
                  : "描述任务，Enter 发送 · Shift+Enter 换行",
            )}
            disabled={!state.threadId || submitting}
            rows={2}
          />
          {uploading && (
            <p className={styles.uploadStatus} role="status">
              {t("正在上传文件…")}
            </p>
          )}
          <div className={styles.composerBottom}>
            <ComposerActions
              state={state}
              attachments={attachments}
              onAddFiles={addFiles}
              onRemoveAttachment={removeAttachment}
              onOpenSettings={onOpenProducts}
            />
            <button
              className={`${shared.button} ${styles.sendButton}`}
              onClick={submit}
              disabled={!canSend}
              aria-label={t("发送消息")}
              title={running ? t("排队发送") : t("发送消息")}
            >
              <ArrowUp size={17} strokeWidth={2.5} />
            </button>
          </div>
        </div>
        <div className={styles.statusBar}>
          <span role="status" aria-live="polite" aria-atomic="true">
            <span
              className={shared.statusDot}
              data-status={state.connection === "connected" ? "running" : "paused"}
              aria-hidden="true"
            />
            {t(state.connection === "connected" ? "事件已连接" : "事件重连中")} ·{" "}
            {t("{count} 个子 Agent 运行中", {
              count: state.tasks.filter((task) => task.status === "running").length,
            })}
          </span>
          <span>{state.status.protocol ?? t("未配置协议")}</span>
          {running && (
            <span>
              <Clock3 size={12} /> {t("当前请求")} {duration || "0s"}
            </span>
          )}
        </div>
      </div>
    </main>
  );
}
