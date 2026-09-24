import { lazy, Suspense, useEffect, useState } from "react";
import {
  ArrowRight,
  Braces,
  ChevronRight,
  CircleCheck,
  CirclePlus,
  GitCompare,
  List,
  ListTodo,
  PanelRightClose,
  Play,
  Settings2,
  ShieldAlert,
  Square,
  Waypoints,
} from "lucide-react";
import type { Task } from "../api/types";
import type { WorkspaceState } from "../state/useWorkspace";
import {
  agentColor,
  readableJson,
  relativeTime,
  roleLabel,
  shortId,
  statusLabel,
} from "../lib/format";
import shared from "../styles/shared.module.css";
import styles from "./Inspector.module.css";
import { useI18n } from "../i18n";
import { descendantTasks } from "../lib/tasks";

const AgentGraph = lazy(() =>
  import("./AgentGraph").then((module) => ({ default: module.AgentGraph })),
);

type InspectorTab = "agents" | "approvals" | "changes" | "settings";

interface InspectorProps {
  state: WorkspaceState;
  onClose: () => void;
  onOpenApproval: () => void;
  onOpenProducts: () => void;
}

function TaskRow({ task, active, onClick }: { task: Task; active: boolean; onClick: () => void }) {
  const { t, language } = useI18n();
  return (
    <button
      className={`${styles.taskRow} ${active ? styles.activeTask : ""}`}
      onClick={onClick}
      aria-current={active ? "true" : undefined}
    >
      <span className={styles.taskColor} style={{ background: agentColor(task.thread_id) }} />
      <span className={styles.taskText}>
        <strong>{task.title || roleLabel(task.role, t)}</strong>
        <small>
          {roleLabel(task.role, t)} · {statusLabel(task.status, t)}
          {task.updated_at ? ` · ${relativeTime(task.updated_at, t, language)}` : ""}
        </small>
      </span>
      <span
        className={shared.statusDot}
        data-status={task.status}
        aria-label={statusLabel(task.status, t)}
      />
    </button>
  );
}

function AgentPanel({ state, onOpenApproval }: Pick<InspectorProps, "state" | "onOpenApproval">) {
  const { t } = useI18n();
  const [view, setView] = useState<"graph" | "list">("graph");
  const [spawning, setSpawning] = useState(false);
  const [role, setRole] = useState("builder");
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [worktree, setWorktree] = useState(true);
  const selected = state.tasks.find((task) => task.thread_id === state.threadId);
  const currentTasks = descendantTasks(state.tasks, state.sessionId);
  const activeCount = currentTasks.filter((task) => task.status === "running").length;
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!prompt.trim()) return;
    void state
      .spawnTask({
        role,
        prompt: prompt.trim(),
        title: title.trim() || undefined,
        worktree_enabled: role === "builder" ? worktree : undefined,
      })
      .then(() => {
        setSpawning(false);
        setPrompt("");
        setTitle("");
      })
      .catch(() => {});
  };
  return (
    <div className={styles.panelScroll}>
      <div className={styles.sectionTitle}>
        <span>
          <Waypoints size={16} /> {t("Agent 协作")}
        </span>
        <span className={styles.softCount}>
          {t("{active} 运行中 / {total} 子任务", {
            active: activeCount,
            total: currentTasks.length,
          })}
        </span>
      </div>
      <div className={styles.segmented} role="tablist" aria-label={t("Agent 展示形式")}>
        <button
          role="tab"
          aria-selected={view === "graph"}
          className={view === "graph" ? styles.activeSegment : ""}
          onClick={() => setView("graph")}
        >
          <Waypoints size={13} />
          {t("关系图")}
        </button>
        <button
          role="tab"
          aria-selected={view === "list"}
          className={view === "list" ? styles.activeSegment : ""}
          onClick={() => setView("list")}
        >
          <List size={13} />
          {t("列表")}
        </button>
      </div>
      {view === "graph" && state.sessionId && (
        <Suspense fallback={<div className={styles.inlineEmpty}>{t("正在绘制 Agent 关系…")}</div>}>
          <AgentGraph
            rootThreadId={state.sessionId}
            rootStatus={
              state.parentSnapshot?.thread_id === state.sessionId
                ? state.parentSnapshot.status
                : "idle"
            }
            tasks={currentTasks}
            selectedThreadId={state.threadId}
            onSelect={state.selectThread}
          />
        </Suspense>
      )}
      <div className={styles.taskList}>
        <button
          className={`${styles.taskRow} ${state.threadId === state.sessionId ? styles.activeTask : ""}`}
          onClick={() => {
            if (state.sessionId) state.selectThread(state.sessionId);
          }}
        >
          <span className={styles.taskColor} style={{ background: "var(--agent-saya)" }} />
          <span className={styles.taskText}>
            <strong>SAYA</strong>
            <small>
              {t("主 Agent")} ·{" "}
              {statusLabel(
                state.parentSnapshot?.thread_id === state.sessionId
                  ? state.parentSnapshot.status
                  : "idle",
                t,
              )}
            </small>
          </span>
          <ChevronRight size={14} />
        </button>
        {currentTasks.map((task) => (
          <TaskRow
            key={task.id}
            task={task}
            active={task.thread_id === state.threadId}
            onClick={() => state.selectThread(task.thread_id)}
          />
        ))}
        {!currentTasks.length && (
          <div className={styles.inlineEmpty}>
            {t("暂无子 Agent。主 Agent 可以自主派发，你也可以手动创建。")}
          </div>
        )}
      </div>
      <button
        className={`${shared.secondaryButton} ${styles.wideButton}`}
        onClick={() => setSpawning((value) => !value)}
        disabled={!state.threadId}
      >
        <CirclePlus size={15} />
        {t("创建子 Agent")}
      </button>
      {spawning && (
        <form className={styles.spawnForm} onSubmit={submit}>
          <label className={shared.label} htmlFor="spawn-role">
            {t("角色")}
          </label>
          <select
            className={shared.select}
            id="spawn-role"
            value={role}
            onChange={(event) => setRole(event.target.value)}
          >
            <option value="builder">{t("构建")}</option>
            <option value="planner">{t("规划")}</option>
            <option value="reviewer">{t("审查")}</option>
          </select>
          <label className={shared.label} htmlFor="spawn-title">
            {t("标题（可选）")}
          </label>
          <input
            className={shared.field}
            id="spawn-title"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder={t("例如：检查后端 API")}
          />
          <label className={shared.label} htmlFor="spawn-prompt">
            {t("任务")}
          </label>
          <textarea
            className={shared.textarea}
            id="spawn-prompt"
            rows={4}
            required
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            placeholder={t("说明子 Agent 要完成的工作")}
          />
          {role === "builder" && (
            <label className={styles.checkbox}>
              <input
                type="checkbox"
                checked={worktree}
                onChange={(event) => setWorktree(event.target.checked)}
              />
              {t("使用独立 Git worktree")}
            </label>
          )}
          <button className={shared.button} disabled={state.busy || !prompt.trim()} type="submit">
            {t("派发任务")}
          </button>
        </form>
      )}
      {selected && (
        <section className={styles.detailCard}>
          <div className={styles.detailHead}>
            <span
              className={styles.taskColor}
              style={{ background: agentColor(selected.thread_id) }}
            />
            <strong>{selected.title}</strong>
          </div>
          <div className={styles.detailGrid}>
            <span>{t("角色")}</span>
            <b>{roleLabel(selected.role, t)}</b>
            <span>{t("状态")}</span>
            <b>{statusLabel(selected.status, t)}</b>
            <span>{t("线程")}</span>
            <b className={shared.mono}>{shortId(selected.thread_id, 16)}</b>
            <span>{t("工作树")}</span>
            <b>{t(selected.worktree_enabled ? "独立" : "共享")}</b>
            {selected.last_outcome && (
              <>
                <span>{t("本轮结果")}</span>
                <b>{statusLabel(selected.last_outcome, t)}</b>
              </>
            )}
          </div>
          {selected.stopped_reason && (
            <p className={styles.taskResult}>
              {t("停止原因")}：{selected.stopped_reason}
            </p>
          )}
          {selected.unconfirmed_effects && (
            <p className={styles.taskError} role="alert">
              {t("在途操作可能未确认，恢复前请先检查工作树。")}
              {selected.recovery_note && ` ${selected.recovery_note}`}
            </p>
          )}
          {selected.result && <p className={styles.taskResult}>{selected.result}</p>}
          {selected.error && <p className={styles.taskError}>{selected.error}</p>}
          <div className={styles.taskActions}>
            {selected.status === "running" && (
              <button
                className={`${shared.secondaryButton} ${shared.small}`}
                onClick={() => {
                  void state.taskAction(selected.id, "stop").catch(() => {});
                }}
                disabled={state.busy}
              >
                <Square size={13} />
                {t("停止")}
              </button>
            )}
            {(["stopped", "interrupted"].includes(selected.status) ||
              (selected.status === "idle" && selected.last_outcome === "stopped")) && (
              <button
                className={`${shared.secondaryButton} ${shared.small}`}
                onClick={() => {
                  void state.taskAction(selected.id, "resume").catch(() => {});
                }}
                disabled={state.busy}
              >
                <Play size={13} />
                {t("恢复")}
              </button>
            )}
            {selected.status === "paused" && (
              <button
                className={`${shared.secondaryButton} ${shared.small}`}
                onClick={onOpenApproval}
              >
                <ShieldAlert size={13} />
                {t("处理审批")}
              </button>
            )}
          </div>
        </section>
      )}
    </div>
  );
}

function ApprovalsPanel({
  state,
  onOpenApproval,
}: Pick<InspectorProps, "state" | "onOpenApproval">) {
  const { t } = useI18n();
  const paused = descendantTasks(state.tasks, state.sessionId).filter(
    (task) => task.status === "paused",
  );
  return (
    <div className={styles.panelScroll}>
      <div className={styles.sectionTitle}>
        <span>
          <ShieldAlert size={16} /> {t("待批准")}
        </span>
        <span className={styles.softCount}>
          {t("{count} 项线程", {
            count:
              paused.length +
              (state.snapshot?.pending_approval && state.threadId === state.sessionId ? 1 : 0),
          })}
        </span>
      </div>
      {state.snapshot?.pending_approval && (
        <section className={styles.approvalCard}>
          <strong>
            {state.threadId === state.sessionId
              ? "SAYA"
              : (state.tasks.find((task) => task.thread_id === state.threadId)?.title ??
                t("子 Agent"))}
          </strong>
          <span>
            {t("{count} 个待执行操作", { count: state.snapshot.pending_approval.actions.length })}
          </span>
          {state.snapshot.pending_approval.actions.map((action, index) => (
            <code key={index}>{action.name}</code>
          ))}
          <button className={shared.button} onClick={onOpenApproval}>
            {t("逐项审查")} <ArrowRight size={14} />
          </button>
        </section>
      )}
      {paused
        .filter((task) => task.thread_id !== state.threadId)
        .map((task) => (
          <button
            key={task.id}
            className={styles.pendingTask}
            onClick={() => state.selectThread(task.thread_id)}
          >
            <span>{task.title}</span>
            <small>{t("选择后查看待批准操作")}</small>
            <ChevronRight size={14} />
          </button>
        ))}
      {!paused.length && !state.snapshot?.pending_approval && (
        <div className={shared.empty}>
          <ShieldAlert size={24} strokeWidth={1.4} />
          <strong>{t("没有待批准操作")}</strong>
          <p>{t("Agent 遇到需要你决定的调用时，会在这里停下。")}</p>
        </div>
      )}
    </div>
  );
}

function ChangesPanel({ state }: Pick<InspectorProps, "state">) {
  const { t } = useI18n();
  const [taskId, setTaskId] = useState<string | null>(null);
  const [diff, setDiff] = useState<unknown>(null);
  const [confirming, setConfirming] = useState(false);
  const [cleaning, setCleaning] = useState(false);
  const deliverables = descendantTasks(state.tasks, state.sessionId).filter(
    (task) => task.worktree_enabled && task.delivery_state !== "cleaned",
  );
  const chosen = deliverables.find((task) => task.id === taskId);
  useEffect(() => {
    setDiff(null);
    setConfirming(false);
    setCleaning(false);
  }, [taskId]);
  return (
    <div className={styles.panelScroll}>
      <div className={styles.sectionTitle}>
        <span>
          <GitCompare size={16} /> {t("变更交付")}
        </span>
        <span className={styles.softCount}>{t("{count} 项", { count: deliverables.length })}</span>
      </div>
      {deliverables.map((task) => (
        <button
          className={`${styles.changeRow} ${task.id === taskId ? styles.activeTask : ""}`}
          key={task.id}
          onClick={() => setTaskId(task.id)}
        >
          <strong>{task.title}</strong>
          <small>
            {t(task.delivery_state === "applied" ? "已应用" : "待应用")} ·{" "}
            {statusLabel(task.status, t)}
          </small>
        </button>
      ))}
      {!deliverables.length && (
        <div className={shared.empty}>
          <GitCompare size={24} strokeWidth={1.4} />
          <strong>{t("暂无可交付变更")}</strong>
          <p>{t("使用独立 worktree 的子 Agent 完成后，可在此检查和应用差异。")}</p>
        </div>
      )}
      {chosen && (
        <section className={styles.deliveryCard}>
          <div className={styles.detailHead}>
            <strong>{chosen.title}</strong>
          </div>
          <button
            className={shared.secondaryButton}
            disabled={state.busy}
            onClick={() => {
              void state
                .taskAction(chosen.id, "diff")
                .then((value) => setDiff(value.diff ?? value))
                .catch(() => {});
            }}
          >
            <Braces size={14} />
            {t("查看差异")}
          </button>
          {diff != null && <pre className={styles.diff}>{readableJson(diff, 30000, t)}</pre>}
          {chosen.delivery_state !== "applied" &&
            !["pending", "running", "stopping"].includes(chosen.status) &&
            !chosen.unconfirmed_effects &&
            (confirming ? (
              <div className={styles.confirmRow}>
                <p>{t("确定将这份差异应用到主工作区？发生冲突时不会部分应用。")}</p>
                <button
                  className={`${shared.ghostButton} ${shared.small}`}
                  onClick={() => setConfirming(false)}
                >
                  {t("取消")}
                </button>
                <button
                  className={`${shared.button} ${shared.small}`}
                  disabled={state.busy}
                  onClick={() => {
                    void state
                      .taskAction(chosen.id, "apply")
                      .then(() => setConfirming(false))
                      .catch(() => {});
                  }}
                >
                  {t("确认应用")}
                </button>
              </div>
            ) : (
              <button
                className={shared.button}
                disabled={state.busy || diff == null}
                onClick={() => setConfirming(true)}
              >
                {t("应用到主工作区")}
              </button>
            ))}
          {chosen.delivery_state === "applied" &&
            ["completed", "stopped", "failed", "interrupted"].includes(chosen.status) &&
            (cleaning ? (
              <div className={styles.confirmRow}>
                <p>{t("确认清理已应用交付的独立工作树？不会删除主工作区中的已应用变更。")}</p>
                <button
                  className={`${shared.ghostButton} ${shared.small}`}
                  onClick={() => setCleaning(false)}
                >
                  {t("取消")}
                </button>
                <button
                  className={`${shared.dangerButton} ${shared.small}`}
                  disabled={state.busy}
                  onClick={() => {
                    void state
                      .taskAction(chosen.id, "cleanup")
                      .then(() => setCleaning(false))
                      .catch(() => {});
                  }}
                >
                  {t("确认清理")}
                </button>
              </div>
            ) : (
              <button className={shared.secondaryButton} onClick={() => setCleaning(true)}>
                {t("清理独立工作树")}
              </button>
            ))}
        </section>
      )}
    </div>
  );
}

function SettingsPanel({
  state,
  onOpenProducts,
}: Pick<InspectorProps, "state" | "onOpenProducts">) {
  const { t } = useI18n();
  const [advanced, setAdvanced] = useState({
    output_limit_bytes: 65536,
    task_notice_limit_bytes: 65536,
    max_consecutive_wakes: 8,
    shutdown_grace_seconds: 10,
  });
  useEffect(() => {
    if (!state.settings) return;
    setAdvanced({
      output_limit_bytes: state.settings.output_limit_bytes ?? 65536,
      task_notice_limit_bytes: state.settings.task_notice_limit_bytes ?? 65536,
      max_consecutive_wakes: state.settings.max_consecutive_wakes ?? 8,
      shutdown_grace_seconds: state.settings.shutdown_grace_seconds ?? 10,
    });
  }, [state.settings]);
  const levels = [
    { value: "read_only", label: "只读", detail: "文件写入不可用" },
    { value: "ask", label: "询问", detail: "有副作用的操作需批准" },
    { value: "jev", label: "Jev 自动审理", detail: "按风险等级自动处理" },
    { value: "full", label: "完全信任", detail: "在本机直接执行操作" },
  ] as const;
  return (
    <div className={styles.panelScroll}>
      <div className={styles.sectionTitle}>
        <span>
          <Settings2 size={16} /> {t("当前会话设置")}
        </span>
      </div>
      <section className={styles.settingsSection}>
        <h3>{t("信任档位")}</h3>
        <p>{t("更改只影响当前会话。新会话使用用户默认档位。")}</p>
        <div className={styles.trustOptions}>
          {levels.map((level) => (
            <button
              key={level.value}
              className={`${styles.trustOption} ${state.snapshot?.trust_level === level.value ? styles.selectedTrust : ""}`}
              onClick={() => {
                void state.setTrust(level.value).catch(() => {});
              }}
              disabled={!state.threadId || state.busy}
              aria-pressed={state.snapshot?.trust_level === level.value}
            >
              <span>
                <strong>{t(level.label)}</strong>
                <small>{t(level.detail)}</small>
              </span>
              {state.snapshot?.trust_level === level.value && <CircleCheck size={15} />}
            </button>
          ))}
        </div>
      </section>
      <section className={styles.settingsSection}>
        <h3>{t("运行配置")}</h3>
        <div className={styles.detailGrid}>
          <span>{t("模型")}</span>
          <b>{state.settings?.active_profile || state.status.model || t("未配置")}</b>
          <span>{t("协议")}</span>
          <b>{state.status.protocol || "—"}</b>
          <span>{t("自动记忆")}</span>
          <b>{t(state.settings?.memory_enabled ? "已开启" : "已关闭")}</b>
        </div>
        <label className={shared.label} style={{ marginTop: 13 }} htmlFor="default-trust">
          {t("新会话默认信任档")}
        </label>
        <select
          className={shared.select}
          id="default-trust"
          value={state.settings?.default_trust || "ask"}
          onChange={(event) => {
            void state
              .setDefaultTrust(event.target.value as "read_only" | "ask" | "jev" | "full")
              .catch(() => {});
          }}
          disabled={state.busy}
        >
          {levels.map((level) => (
            <option value={level.value} key={level.value}>
              {t(level.label)}
            </option>
          ))}
        </select>
        <label className={shared.label} style={{ marginTop: 13 }} htmlFor="interface-language">
          {t("界面语言")}
        </label>
        <select
          className={shared.select}
          id="interface-language"
          value={state.settings?.language ?? "auto"}
          onChange={(event) => {
            void state.updateSettings({ language: event.target.value }).catch(() => {});
          }}
          disabled={state.busy}
        >
          <option value="auto">{t("跟随浏览器")}</option>
          <option value="zh">{t("中文")}</option>
          <option value="en">{t("英文")}</option>
        </select>
      </section>
      <details className={styles.settingsSection}>
        <summary className={styles.advancedSummary}>{t("高级运行设置")}</summary>
        <form
          className={styles.advancedForm}
          onSubmit={(event) => {
            event.preventDefault();
            if (Object.values(advanced).some((value) => !Number.isFinite(value) || value <= 0))
              return;
            void state.updateSettings(advanced).catch(() => {});
          }}
        >
          <label className={shared.label} htmlFor="output-limit">
            {t("工具输出预览上限（字节）")}
          </label>
          <input
            id="output-limit"
            className={shared.field}
            type="number"
            min="1"
            step="1"
            value={advanced.output_limit_bytes}
            onChange={(event) =>
              setAdvanced((old) => ({ ...old, output_limit_bytes: Number(event.target.value) }))
            }
          />
          <label className={shared.label} htmlFor="notice-limit">
            {t("子任务通知预览上限（字节）")}
          </label>
          <input
            id="notice-limit"
            className={shared.field}
            type="number"
            min="1"
            step="1"
            value={advanced.task_notice_limit_bytes}
            onChange={(event) =>
              setAdvanced((old) => ({
                ...old,
                task_notice_limit_bytes: Number(event.target.value),
              }))
            }
          />
          <label className={shared.label} htmlFor="wake-limit">
            {t("连续自动唤醒保护次数")}
          </label>
          <input
            id="wake-limit"
            className={shared.field}
            type="number"
            min="1"
            step="1"
            value={advanced.max_consecutive_wakes}
            onChange={(event) =>
              setAdvanced((old) => ({ ...old, max_consecutive_wakes: Number(event.target.value) }))
            }
          />
          <p>{t("仅控制子任务通知反复唤醒主 Agent；不是模型调用轮次上限。")}</p>
          <label className={shared.label} htmlFor="shutdown-grace">
            {t("退出宽限期（秒）")}
          </label>
          <input
            id="shutdown-grace"
            className={shared.field}
            type="number"
            min="0.1"
            step="0.1"
            value={advanced.shutdown_grace_seconds}
            onChange={(event) =>
              setAdvanced((old) => ({ ...old, shutdown_grace_seconds: Number(event.target.value) }))
            }
          />
          <button className={shared.secondaryButton} disabled={state.busy}>
            {t("保存高级设置")}
          </button>
        </form>
      </details>
      <button className={`${shared.secondaryButton} ${styles.wideButton}`} onClick={onOpenProducts}>
        <Settings2 size={15} />
        {t("模型、MCP、Skill、记忆与诊断")}
      </button>
    </div>
  );
}

export function Inspector({ state, onClose, onOpenApproval, onOpenProducts }: InspectorProps) {
  const { t } = useI18n();
  const [tab, setTab] = useState<InspectorTab>("agents");
  const tabs: { key: InspectorTab; label: string; icon: React.ReactNode; count?: number }[] = [
    { key: "agents", label: "协作", icon: <Waypoints size={15} />, count: state.tasks.length },
    {
      key: "approvals",
      label: "审批",
      icon: <ShieldAlert size={15} />,
      count:
        state.tasks.filter((task) => task.status === "paused").length +
        (state.snapshot?.pending_approval && state.threadId === state.sessionId ? 1 : 0),
    },
    { key: "changes", label: "变更", icon: <GitCompare size={15} /> },
    { key: "settings", label: "设置", icon: <Settings2 size={15} /> },
  ];
  return (
    <aside className={styles.inspector} aria-label={t("任务检查器")}>
      <div className={styles.header}>
        <div>
          <span className={shared.eyebrow}>RUN INSPECTOR</span>
          <strong>{t("任务检查器")}</strong>
        </div>
        <button
          className={`${shared.iconButton} ${styles.mobileClose}`}
          onClick={onClose}
          aria-label={t("关闭检查器")}
        >
          <PanelRightClose size={18} />
        </button>
      </div>
      <nav className={styles.tabs} aria-label={t("检查器视图")}>
        {tabs.map((item) => (
          <button
            key={item.key}
            onClick={() => setTab(item.key)}
            className={tab === item.key ? styles.selectedTab : ""}
            aria-current={tab === item.key ? "page" : undefined}
            title={t(item.label)}
          >
            {item.icon}
            <span>{t(item.label)}</span>
            {item.count ? <b>{item.count}</b> : null}
          </button>
        ))}
      </nav>
      {tab === "agents" && <AgentPanel state={state} onOpenApproval={onOpenApproval} />}
      {tab === "approvals" && <ApprovalsPanel state={state} onOpenApproval={onOpenApproval} />}
      {tab === "changes" && <ChangesPanel state={state} />}
      {tab === "settings" && <SettingsPanel state={state} onOpenProducts={onOpenProducts} />}
      <div className={styles.footer}>
        <ListTodo size={14} /> {t("当前线程")} {shortId(state.threadId ?? "—", 15)}
      </div>
    </aside>
  );
}
