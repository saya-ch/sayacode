import { useEffect, useState } from "react";
import { RotateCw } from "lucide-react";
import { api } from "../api/client";
import { useI18n } from "../i18n";
import { shortId } from "../lib/format";
import { useProduct } from "./useProduct";
import shared from "../styles/shared.module.css";
import styles from "./panel.module.css";
import sessionStyles from "./session.module.css";

interface SessionPanelProps {
  workspaceId: string | null;
  threadId: string | null;
  onChanged?: () => void | Promise<void>;
}

function displayTime(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function isIdle(snapshot: Awaited<ReturnType<typeof api.snapshot>>): boolean {
  return (
    !snapshot.active_run &&
    !snapshot.pending_approval &&
    ["idle", "completed", "stopped", "error", "failed", "rewound"].includes(snapshot.status)
  );
}

export function SessionPanel({ workspaceId, threadId, onChanged }: SessionPanelProps) {
  const { t } = useI18n();
  const [focus, setFocus] = useState("");
  const [confirmRewind, setConfirmRewind] = useState<{
    threadId: string;
    checkpointId: string;
  } | null>(null);
  const [confirmGrantClear, setConfirmGrantClear] = useState<string | null>(null);
  const [confirmHookTrust, setConfirmHookTrust] = useState<{
    workspaceId: string;
    trusted: boolean;
  } | null>(null);
  const [success, setSuccess] = useState<{
    scope: "thread" | "workspace";
    id: string;
    text: string;
  } | null>(null);

  useEffect(() => {
    setFocus("");
    setConfirmRewind(null);
    setConfirmGrantClear(null);
    setSuccess(null);
  }, [threadId]);
  useEffect(() => {
    setConfirmHookTrust(null);
    setSuccess(null);
  }, [workspaceId]);

  // 会话信息与工作区 Hook 各自加载，缺少一个 ID 时仍能使用另一组功能。
  const thread = useProduct(`session-operations:${threadId}`, async () => {
    if (!threadId) return null;
    const [snapshot, checkpoints, trace, tools, memory] = await Promise.all([
      api.snapshot(threadId),
      api.checkpoints(threadId),
      api.traceThread(threadId),
      api.threadTools(threadId),
      api.threadMemorySettings(threadId),
    ]);
    return { threadId, snapshot, checkpoints, trace, tools, memory };
  });
  const hooks = useProduct(`session-hooks:${workspaceId}`, async () => {
    if (!workspaceId) return null;
    const [status, audit] = await Promise.all([api.hooks(workspaceId), api.hookAudit(workspaceId)]);
    return { workspaceId, status, audit };
  });
  const models = useProduct("session-model", api.models);

  const threadData = thread.data?.threadId === threadId ? thread.data : null;
  const hookData = hooks.data?.workspaceId === workspaceId ? hooks.data : null;
  const idle = threadData ? isIdle(threadData.snapshot) : false;
  const modelConfigured = Boolean(models.data?.active_profile);

  const freshIdle = async (id: string) => {
    if (!(await api.models()).active_profile)
      throw new Error(t("请先配置模型，才能压缩或回退会话上下文。"));
    // 点击后再查一次，避免面板打开期间开始运行造成过期状态。
    if (!isIdle(await api.snapshot(id)))
      throw new Error(t("会话正在运行或等待处理，当前不能回退或压缩。"));
  };

  const changeThread = (work: () => Promise<string>) => {
    const id = threadId;
    setSuccess(null);
    void thread
      .run(work)
      .then(async (message) => {
        await onChanged?.();
        if (id) setSuccess({ scope: "thread", id, text: message as string });
      })
      .catch((reason: unknown) => {
        thread.setError(reason instanceof Error ? reason.message : t("操作失败"));
      });
  };

  const changeHooks = (work: () => Promise<string>) => {
    const id = workspaceId;
    setSuccess(null);
    void hooks
      .run(work)
      .then(async (message) => {
        await onChanged?.();
        if (id) setSuccess({ scope: "workspace", id, text: message as string });
      })
      .catch((reason: unknown) => {
        hooks.setError(reason instanceof Error ? reason.message : t("操作失败"));
      });
  };

  return (
    <section className={sessionStyles.panel}>
      <div className={styles.head}>
        <h2>{t("会话与运行")}</h2>
        <p>{t("查看检查点、运行追踪、工具与 Hook，并管理当前会话的记忆设置。")}</p>
      </div>
      <div className={styles.row}>
        <button
          type="button"
          className={`${shared.secondaryButton} ${shared.small}`}
          disabled={thread.loading || hooks.loading || thread.busy || hooks.busy}
          onClick={() => {
            setSuccess(null);
            void Promise.all([thread.refresh(), hooks.refresh(), models.refresh()]);
          }}
        >
          <RotateCw size={14} />
          {t("刷新")}
        </button>
      </div>
      {success &&
        ((success.scope === "thread" && success.id === threadId) ||
          (success.scope === "workspace" && success.id === workspaceId)) && (
          <div className={`${styles.notice} ${styles.success}`} role="status">
            {success.text}
          </div>
        )}
      {(thread.error || hooks.error) && (
        <div className={styles.notice} role="alert">
          {thread.error || hooks.error}
        </div>
      )}

      <div className={sessionStyles.group}>
        <h3 className={styles.sectionTitle}>{t("检查点与上下文")}</h3>
        {!threadId ? (
          <div className={shared.empty}>{t("请先选择会话。")}</div>
        ) : thread.loading ? (
          <div className={styles.loading}>{t("正在读取会话信息…")}</div>
        ) : threadData ? (
          <>
            <div className={`${styles.card} ${sessionStyles.actionCard}`}>
              <div className={styles.rowBetween}>
                <strong>{t("手动压缩上下文")}</strong>
                <span className={styles.badge}>
                  {!modelConfigured ? t("模型未配置") : idle ? t("空闲") : t("当前不可操作")}
                </span>
              </div>
              <p>{t("将较早消息整理为摘要。消息不足时不会执行压缩。")}</p>
              <label className={shared.label} htmlFor="session-compact-focus">
                {t("希望保留的重点（可选）")}
              </label>
              <input
                id="session-compact-focus"
                className={shared.field}
                value={focus}
                onChange={(event) => setFocus(event.target.value)}
                placeholder={t("例如：当前任务目标与已确认的约束")}
              />
              <button
                type="button"
                className={`${shared.secondaryButton} ${shared.small}`}
                disabled={!idle || !modelConfigured || thread.busy}
                onClick={() => {
                  changeThread(async () => {
                    await freshIdle(threadId);
                    const result = await api.compactThread(threadId, focus.trim() || null);
                    return result.compacted ? t("上下文压缩完成。") : t("消息不足，未执行压缩。");
                  });
                }}
              >
                {thread.busy ? t("正在处理…") : t("压缩上下文")}
              </button>
              {!modelConfigured && (
                <p className={sessionStyles.hint}>
                  {t("请先配置模型，才能压缩或回退会话上下文。")}
                </p>
              )}
              {!idle && modelConfigured && (
                <p className={sessionStyles.hint}>
                  {t("请在会话空闲且无待批准操作时压缩或回退。")}
                </p>
              )}
            </div>
            <div className={styles.list}>
              {threadData.checkpoints.map((checkpoint) => (
                <article key={checkpoint.checkpoint_id} className={styles.card}>
                  <div className={styles.cardTitle}>
                    <strong className={sessionStyles.mono}>
                      {shortId(checkpoint.checkpoint_id, 16)}
                    </strong>
                    <span className={styles.badge}>
                      {t("消息 {count} 条", { count: checkpoint.message_count })}
                    </span>
                  </div>
                  <div className={styles.meta}>
                    <span>{displayTime(checkpoint.created_at)}</span>
                    {checkpoint.next.length > 0 && (
                      <span>{t("后续节点：{steps}", { steps: checkpoint.next.join(", ") })}</span>
                    )}
                  </div>
                  <div className={sessionStyles.actions}>
                    <button
                      type="button"
                      className={`${shared.ghostButton} ${shared.small}`}
                      disabled={
                        !idle || !modelConfigured || thread.busy || !checkpoint.checkpoint_id
                      }
                      onClick={() =>
                        setConfirmRewind({ threadId, checkpointId: checkpoint.checkpoint_id })
                      }
                    >
                      {t("回退到此处")}
                    </button>
                  </div>
                  {confirmRewind?.threadId === threadId &&
                    confirmRewind.checkpointId === checkpoint.checkpoint_id && (
                      <div className={styles.confirm}>
                        <p>
                          {t(
                            "确认回退到此检查点？系统会建立新的会话状态分支；不会撤销或修改工作区文件，也不会自动重做工具操作。",
                          )}
                        </p>
                        <div className={styles.row}>
                          <button
                            type="button"
                            className={`${shared.dangerButton} ${shared.small}`}
                            disabled={!idle || !modelConfigured || thread.busy}
                            onClick={() => {
                              changeThread(async () => {
                                await freshIdle(threadId);
                                const result = await api.rewindThread(
                                  threadId,
                                  checkpoint.checkpoint_id,
                                );
                                if (!result.rewound) throw new Error(t("回退未完成。"));
                                setConfirmRewind(null);
                                return t("已回退到检查点 {id}。", {
                                  id: shortId(result.checkpoint_id, 12),
                                });
                              });
                            }}
                          >
                            {t("确认回退")}
                          </button>
                          <button
                            type="button"
                            className={`${shared.ghostButton} ${shared.small}`}
                            onClick={() => setConfirmRewind(null)}
                          >
                            {t("取消")}
                          </button>
                        </div>
                      </div>
                    )}
                </article>
              ))}
              {threadData.checkpoints.length === 0 && (
                <div className={shared.empty}>{t("此会话还没有检查点。")}</div>
              )}
            </div>
          </>
        ) : null}
      </div>

      <div className={sessionStyles.group}>
        <h3 className={styles.sectionTitle}>{t("会话记忆")}</h3>
        {threadId && thread.loading && (
          <div className={styles.loading}>{t("正在读取记忆设置…")}</div>
        )}
        {threadData && (
          <div className={`${styles.card} ${sessionStyles.settings}`}>
            <p>{t("可覆盖当前会话的记忆读取与学习设置；选择继承时使用全局设置。")}</p>
            <div className={styles.columns}>
              <div>
                <label className={shared.label} htmlFor="session-memory-use">
                  {t("读取已保存记忆")}
                </label>
                <select
                  id="session-memory-use"
                  className={shared.select}
                  disabled={thread.busy}
                  value={
                    threadData.memory.use_override === null
                      ? "inherit"
                      : threadData.memory.use_override
                        ? "on"
                        : "off"
                  }
                  onChange={(event) => {
                    const value = event.target.value;
                    changeThread(async () => {
                      await api.updateThreadMemorySettings(threadData.threadId, {
                        use: value === "inherit" ? null : value === "on",
                      });
                      return t("会话记忆读取设置已保存。");
                    });
                  }}
                >
                  <option value="inherit">{t("继承全局设置")}</option>
                  <option value="on">{t("使用")}</option>
                  <option value="off">{t("停用")}</option>
                </select>
                <span className={sessionStyles.hint}>
                  {t("当前生效：{value}", { value: threadData.memory.use ? t("使用") : t("停用") })}
                </span>
              </div>
              <div>
                <label className={shared.label} htmlFor="session-memory-learn">
                  {t("会话学习")}
                </label>
                <select
                  id="session-memory-learn"
                  className={shared.select}
                  disabled={thread.busy}
                  value={threadData.memory.learn_override ?? "inherit"}
                  onChange={(event) => {
                    const value = event.target.value;
                    changeThread(async () => {
                      await api.updateThreadMemorySettings(threadData.threadId, {
                        learn: value === "inherit" ? null : (value as "off" | "explicit" | "auto"),
                      });
                      return t("会话学习设置已保存。");
                    });
                  }}
                >
                  <option value="inherit">{t("继承全局设置")}</option>
                  <option value="off">{t("关闭")}</option>
                  <option value="explicit">{t("仅显式提出")}</option>
                  <option value="auto">{t("自动整理")}</option>
                </select>
                <span className={sessionStyles.hint}>
                  {t("当前生效：{value}", {
                    value:
                      threadData.memory.learn === "off"
                        ? t("关闭")
                        : threadData.memory.learn === "explicit"
                          ? t("仅显式提出")
                          : t("自动整理"),
                  })}
                </span>
              </div>
            </div>
          </div>
        )}
      </div>

      <div className={sessionStyles.group}>
        <h3 className={styles.sectionTitle}>{t("批准授权")}</h3>
        <div className={styles.card}>
          <p>{t("清除当前会话已保存的工具批准授权。之后需要重新批准相应操作。")}</p>
          <button
            type="button"
            className={`${shared.secondaryButton} ${shared.small}`}
            disabled={!threadId || thread.busy}
            onClick={() => setConfirmGrantClear(threadId)}
          >
            {t("清除批准授权")}
          </button>
          {confirmGrantClear === threadId && threadId && (
            <div className={styles.confirm}>
              <p>{t("确认清除当前会话的全部已保存批准授权？")}</p>
              <div className={styles.row}>
                <button
                  type="button"
                  className={`${shared.dangerButton} ${shared.small}`}
                  disabled={!threadId || thread.busy}
                  onClick={() => {
                    if (!threadId) return;
                    changeThread(async () => {
                      const result = await api.clearApprovalGrants(threadId);
                      if (!result.cleared) throw new Error(t("批准授权未清除。"));
                      setConfirmGrantClear(null);
                      return t("批准授权已清除。");
                    });
                  }}
                >
                  {t("确认清除")}
                </button>
                <button
                  type="button"
                  className={`${shared.ghostButton} ${shared.small}`}
                  onClick={() => setConfirmGrantClear(null)}
                >
                  {t("取消")}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>

      <div className={sessionStyles.group}>
        <h3 className={styles.sectionTitle}>{t("可用工具")}</h3>
        {threadId && thread.loading && <div className={styles.loading}>{t("正在读取工具…")}</div>}
        {threadData && (
          <div className={styles.list}>
            {threadData.tools.map((tool) => (
              <div key={tool.name} className={styles.card}>
                <div className={styles.cardTitle}>
                  <strong>{tool.name}</strong>
                </div>
                {tool.description && <p>{tool.description}</p>}
              </div>
            ))}
            {threadData.tools.length === 0 && (
              <div className={shared.empty}>{t("此会话没有可用工具。")}</div>
            )}
          </div>
        )}
      </div>

      <div className={sessionStyles.group}>
        <h3 className={styles.sectionTitle}>{t("运行追踪")}</h3>
        {threadId && thread.loading && (
          <div className={styles.loading}>{t("正在读取运行追踪…")}</div>
        )}
        {threadData && (
          <div className={`${styles.list} ${sessionStyles.scrollList}`}>
            {threadData.trace.map((entry) => (
              <div key={entry.id} className={styles.card}>
                <div className={styles.cardTitle}>
                  <strong>{entry.event}</strong>
                  <span className={styles.badge}>{displayTime(entry.at)}</span>
                </div>
                <div className={styles.meta}>
                  {entry.run_id && <span>{t("运行 {id}", { id: shortId(entry.run_id, 12) })}</span>}
                  {entry.task_id && (
                    <span>{t("任务 {id}", { id: shortId(entry.task_id, 12) })}</span>
                  )}
                </div>
                {entry.details && Object.keys(entry.details).length > 0 && (
                  <details className={sessionStyles.details}>
                    <summary>{t("查看详情")}</summary>
                    <pre className={styles.monoblock}>{JSON.stringify(entry.details, null, 2)}</pre>
                  </details>
                )}
              </div>
            ))}
            {threadData.trace.length === 0 && (
              <div className={shared.empty}>{t("此会话还没有运行追踪。")}</div>
            )}
          </div>
        )}
      </div>

      <div className={sessionStyles.group}>
        <h3 className={styles.sectionTitle}>{t("工作区 Hook")}</h3>
        {!workspaceId ? (
          <div className={shared.empty}>{t("请先选择工作区。")}</div>
        ) : hooks.loading ? (
          <div className={styles.loading}>{t("正在读取 Hook…")}</div>
        ) : hookData ? (
          <>
            <div className={styles.card}>
              <div className={styles.rowBetween}>
                <strong>{t("项目 Hook 信任")}</strong>
                <span className={styles.badge}>
                  {hookData.status.project_trusted ? t("已信任") : t("未信任")}
                </span>
              </div>
              <p>
                {t(
                  "信任后，工作区中的项目 Hook 配置会被加载并可在事件发生时执行。用户级 Hook 独立于此设置。",
                )}
              </p>
              {threadData &&
                ["read_only", "workspace_auto"].includes(threadData.snapshot.trust_level ?? "") && (
                  <p>
                    {t("当前线程的信任档不会运行 Hook；工作区信任设置只决定项目 Hook 是否可加载。")}
                  </p>
                )}
              <div className={styles.meta}>
                <span>{t("用户级 {count} 个", { count: hookData.status.user_hooks })}</span>
                <span>{t("项目级 {count} 个", { count: hookData.status.project_hooks })}</span>
              </div>
              <div className={sessionStyles.actions}>
                <button
                  type="button"
                  className={`${shared.secondaryButton} ${shared.small}`}
                  disabled={hooks.busy}
                  onClick={() =>
                    setConfirmHookTrust({ workspaceId, trusted: !hookData.status.project_trusted })
                  }
                >
                  {hookData.status.project_trusted ? t("撤销项目 Hook 信任") : t("信任项目 Hook")}
                </button>
                <button
                  type="button"
                  className={`${shared.ghostButton} ${shared.small}`}
                  disabled={hooks.busy}
                  onClick={() => {
                    changeHooks(async () => {
                      await api.reloadHooks(workspaceId);
                      return t("Hook 已重新加载。");
                    });
                  }}
                >
                  <RotateCw size={13} />
                  {t("重新加载 Hook")}
                </button>
              </div>
              {confirmHookTrust?.workspaceId === workspaceId && (
                <div className={styles.confirm}>
                  <p>
                    {confirmHookTrust.trusted
                      ? t("确认信任此工作区的项目 Hook？项目 Hook 可在事件发生时运行本地命令。")
                      : t("确认撤销此工作区的项目 Hook 信任？项目 Hook 将不再加载。")}
                  </p>
                  <div className={styles.row}>
                    <button
                      type="button"
                      className={`${shared.dangerButton} ${shared.small}`}
                      disabled={hooks.busy}
                      onClick={() => {
                        const trusted = confirmHookTrust.trusted;
                        changeHooks(async () => {
                          await api.trustHooks(workspaceId, trusted);
                          setConfirmHookTrust(null);
                          return trusted ? t("已信任项目 Hook。") : t("已撤销项目 Hook 信任。");
                        });
                      }}
                    >
                      {confirmHookTrust.trusted ? t("确认信任") : t("确认撤销")}
                    </button>
                    <button
                      type="button"
                      className={`${shared.ghostButton} ${shared.small}`}
                      onClick={() => setConfirmHookTrust(null)}
                    >
                      {t("取消")}
                    </button>
                  </div>
                </div>
              )}
              {hookData.status.warnings.map((warning, index) => (
                <div key={`${warning}:${index}`} className={styles.notice} role="status">
                  {warning}
                </div>
              ))}
            </div>
            <h4 className={sessionStyles.subhead}>{t("已加载的 Hook")}</h4>
            <div className={styles.list}>
              {hookData.status.hooks.map((hook, index) => (
                <div
                  key={`${hook.source}:${hook.event}:${hook.name}:${index}`}
                  className={styles.card}
                >
                  <div className={styles.cardTitle}>
                    <strong>{hook.name}</strong>
                    <span className={styles.badge}>
                      {hook.source === "project"
                        ? t("项目")
                        : hook.source === "user"
                          ? t("用户")
                          : hook.source}
                    </span>
                  </div>
                  <div className={styles.meta}>
                    <span>{hook.event}</span>
                    <span>{hook.blocking ? t("阻断型") : t("非阻断型")}</span>
                    <span>{t("超时 {seconds} 秒", { seconds: hook.timeout })}</span>
                  </div>
                </div>
              ))}
              {hookData.status.hooks.length === 0 && (
                <div className={shared.empty}>{t("尚无已加载的 Hook。")}</div>
              )}
            </div>
            <h4 className={sessionStyles.subhead}>{t("Hook 执行记录")}</h4>
            <div className={`${styles.list} ${sessionStyles.scrollList}`}>
              {hookData.audit.map((entry) => (
                <div key={entry.id} className={styles.card}>
                  <div className={styles.cardTitle}>
                    <strong>{entry.name}</strong>
                    <span className={styles.badge}>
                      {entry.blocked ? t("已阻断") : t("未阻断")}
                    </span>
                  </div>
                  <div className={styles.meta}>
                    <span>{displayTime(entry.at)}</span>
                    <span>{entry.event}</span>
                    <span>
                      {entry.source === "project"
                        ? t("项目")
                        : entry.source === "user"
                          ? t("用户")
                          : entry.source}
                    </span>
                    <span>{t("退出码 {code}", { code: entry.returncode })}</span>
                  </div>
                </div>
              ))}
              {hookData.audit.length === 0 && (
                <div className={shared.empty}>{t("尚无 Hook 执行记录。")}</div>
              )}
            </div>
          </>
        ) : null}
      </div>
    </section>
  );
}
