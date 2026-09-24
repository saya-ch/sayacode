import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
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
  Radio,
  ShieldAlert,
  Waypoints,
} from "lucide-react";
import type { AgentMessage, Todo } from "../api/types";
import type { WorkspaceState } from "../state/useWorkspace";
import { agentColor, displaySessionTitle, elapsed, roleLabel, statusLabel } from "../lib/format";
import shared from "../styles/shared.module.css";
import styles from "./Conversation.module.css";
import { useI18n } from "../i18n";

const Trajectory = lazy(() =>
  import("./Trajectory").then((module) => ({ default: module.Trajectory })),
);
const MarkdownContent = lazy(() =>
  import("./MarkdownContent").then((module) => ({ default: module.MarkdownContent })),
);

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
  return (
    <article
      className={`${styles.message} ${user ? styles.userMessage : ""} ${tool ? styles.toolMessage : ""} ${inbox ? styles.inboxMessage : ""}`}
    >
      <div
        className={styles.avatar}
        style={!user ? ({ "--avatar-color": color } as React.CSSProperties) : undefined}
        aria-hidden="true"
      >
        {user ? t("你") : tool ? "T" : inbox ? "N" : agentName.slice(0, 1)}
      </div>
      <div className={styles.messageBody}>
        <div className={styles.messageHeader}>
          <strong>
            {user ? t("你") : tool ? t("工具结果") : inbox ? t("子 Agent 通知") : agentName}
          </strong>
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
          {tool ? (
            <details>
              <summary>{t("查看工具输出")}</summary>
              <pre>{message.text}</pre>
            </details>
          ) : inbox ? (
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
  const [draft, setDraft] = useState("");
  const chatScroll = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  const composer = useRef<HTMLTextAreaElement>(null);
  const selectedTask = state.tasks.find((task) => task.thread_id === state.threadId);
  const name = selectedTask?.title || "SAYA";
  const color = selectedTask ? agentColor(selectedTask.thread_id) : "var(--agent-saya)";
  const currentStatus = state.snapshot?.status ?? "idle";
  const hasModel = Boolean(state.settings?.active_profile || state.status.model);
  const running = currentStatus === "running";
  const now = useNow(running);
  const duration = elapsed(state.snapshot?.active_run?.started_at, now);
  const canSend = Boolean(
    state.threadId &&
    draft.trim() &&
    !state.busy &&
    hasModel &&
    (selectedTask || (currentStatus !== "running" && !state.snapshot?.pending_approval)),
  );

  const visibleMessages = useMemo(
    () => state.snapshot?.messages.filter((message) => message.role !== "system") ?? [],
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

  const submit = () => {
    const text = draft.trim();
    if (!text || !canSend) return;
    setDraft("");
    void state.send(text).catch(() => setDraft(text));
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
            {state.settings?.active_profile || state.status.model || t("模型未配置")}
          </span>
          <span className={styles.runBadge} data-status={currentStatus}>
            <span className={shared.statusDot} data-status={currentStatus} />
            {statusLabel(currentStatus, t)}
            {running && duration && <b>{duration}</b>}
          </span>
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
                  <p>
                    {t("描述你希望检查、实现或解释的内容。工具调用和子 Agent 会显示在运行轨迹中。")}
                  </p>
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
        <div className={styles.composerFrame}>
          <label className={styles.composerLabel} htmlFor="agent-composer">
            {t("发送给")} <strong style={{ color }}>{name}</strong>
            {selectedTask && <span>· {t("子 Agent 后续指令")}</span>}
          </label>
          <textarea
            id="agent-composer"
            ref={composer}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                submit();
              }
            }}
            placeholder={t(
              !state.threadId
                ? "请先选择会话"
                : state.snapshot?.pending_approval
                  ? "请先处理待批准操作"
                  : running && !selectedTask
                    ? "当前运行结束后可继续发送"
                    : "描述任务，Enter 发送 · Shift+Enter 换行",
            )}
            disabled={!state.threadId || Boolean(state.snapshot?.pending_approval)}
            rows={2}
          />
          <div className={styles.composerBottom}>
            <span>{t(selectedTask ? "后续指令进入子 Agent 队列" : "发送后由本机运行时执行")}</span>
            <button
              className={shared.button}
              onClick={submit}
              disabled={!canSend}
              aria-label={t("发送消息")}
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
