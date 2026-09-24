import { useEffect, useState } from "react";
import { ArrowRight, CircleCheck, RotateCw } from "lucide-react";
import { api } from "../api/client";
import type { WorkspaceState } from "../state/useWorkspace";
import { displaySessionTitle, shortId } from "../lib/format";
import { useI18n } from "../i18n";
import shared from "../styles/shared.module.css";
import { useProduct } from "./useProduct";
import styles from "./SettingsPreferences.module.css";

type TrustLevel = "read_only" | "ask" | "jev" | "full";
type Advanced = {
  output_limit_bytes: number;
  task_notice_limit_bytes: number;
  max_consecutive_wakes: number;
  shutdown_grace_seconds: number;
};

const trustLevels: { value: TrustLevel; label: string; detail: string }[] = [
  { value: "read_only", label: "只读", detail: "只提供只读工具" },
  { value: "ask", label: "询问", detail: "有副作用的操作需批准" },
  { value: "jev", label: "Jev 自动审理", detail: "按风险等级自动处理" },
  { value: "full", label: "完全信任", detail: "在本机直接执行操作" },
];
const advancedFields: {
  key: keyof Advanced;
  label: string;
  min: string;
  step: string;
}[] = [
  { key: "output_limit_bytes", label: "工具输出预览上限（字节）", min: "1", step: "1" },
  { key: "task_notice_limit_bytes", label: "子任务通知预览上限（字节）", min: "1", step: "1" },
  { key: "max_consecutive_wakes", label: "连续自动唤醒保护次数", min: "1", step: "1" },
  { key: "shutdown_grace_seconds", label: "退出宽限期（秒）", min: "0.1", step: "0.1" },
];

function ScopeTitle({
  scope,
  title,
  description,
}: {
  scope: string;
  title: string;
  description: string;
}) {
  const { t } = useI18n();
  return (
    <div className={styles.scopeHead}>
      <span className={styles.scopeTag}>{t(scope)}</span>
      <h2>{t(title)}</h2>
      <p>{t(description)}</p>
    </div>
  );
}

export function GlobalPreferences({
  state,
  onOpenModels,
}: {
  state: WorkspaceState;
  onOpenModels: () => void;
}) {
  const { t } = useI18n();
  const [advanced, setAdvanced] = useState<Advanced>({
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
  }, [
    state.settings?.output_limit_bytes,
    state.settings?.task_notice_limit_bytes,
    state.settings?.max_consecutive_wakes,
    state.settings?.shutdown_grace_seconds,
  ]);

  return (
    <section>
      <ScopeTitle
        scope="全局"
        title="默认与界面"
        description="这些偏好保存在本机。新线程使用默认值；已指定覆盖的线程保持自己的设置。"
      />
      <div className={styles.settingList}>
        <div className={styles.settingRow}>
          <div>
            <strong>{t("默认模型")}</strong>
            <p>{t("新线程开始时沿用此模型；当前线程可以单独选择。")}</p>
          </div>
          <button
            type="button"
            className={styles.valueButton}
            onClick={onOpenModels}
            aria-label={t("默认模型")}
          >
            <span>
              {state.settings
                ? state.settings.active_profile || t("未配置")
                : state.status.model || t("未配置")}
            </span>
            <ArrowRight size={15} />
          </button>
        </div>
        <div className={styles.settingRow}>
          <div>
            <label htmlFor="settings-default-trust">{t("新线程默认信任档")}</label>
            <p>{t("只影响之后创建的线程。当前线程的信任档在“当前线程”中调整。")}</p>
          </div>
          <select
            id="settings-default-trust"
            className={shared.select}
            value={state.settings?.default_trust ?? "ask"}
            disabled={!state.settings || state.busy}
            onChange={(event) => {
              void state.setDefaultTrust(event.target.value as TrustLevel).catch(() => {});
            }}
          >
            {trustLevels.map((item) => (
              <option value={item.value} key={item.value}>
                {t(item.label)}
              </option>
            ))}
          </select>
        </div>
        <div className={styles.settingRow}>
          <div>
            <label htmlFor="settings-language">{t("界面语言")}</label>
            <p>{t("只改变界面文字，不修改 Agent 的任务指令。")}</p>
          </div>
          <select
            id="settings-language"
            className={shared.select}
            value={state.settings?.language ?? "auto"}
            disabled={!state.settings || state.busy}
            onChange={(event) => {
              void state.updateSettings({ language: event.target.value }).catch(() => {});
            }}
          >
            <option value="auto">{t("跟随浏览器")}</option>
            <option value="zh">{t("中文")}</option>
            <option value="en">{t("英文")}</option>
          </select>
        </div>
      </div>
      <details className={styles.advanced}>
        <summary>{t("高级运行设置")}</summary>
        <p>{t("这些参数作用于本机所有工作区，保存后用于后续运行。")}</p>
        <form
          className={styles.advancedForm}
          onSubmit={(event) => {
            event.preventDefault();
            if (Object.values(advanced).some((value) => !Number.isFinite(value) || value <= 0))
              return;
            void state.updateSettings(advanced).catch(() => {});
          }}
        >
          {advancedFields.map((field) => (
            <div className={styles.advancedField} key={field.key}>
              <label className={shared.label} htmlFor={"settings-" + field.key}>
                {t(field.label)}
              </label>
              <input
                id={"settings-" + field.key}
                className={shared.field}
                type="number"
                min={field.min}
                step={field.step}
                value={advanced[field.key]}
                onChange={(event) =>
                  setAdvanced((old) => ({ ...old, [field.key]: Number(event.target.value) }))
                }
              />
            </div>
          ))}
          <p>{t("仅控制子任务通知反复唤醒主 Agent；不是模型调用轮次上限。")}</p>
          <button className={shared.secondaryButton} disabled={state.busy} type="submit">
            {t("保存高级设置")}
          </button>
        </form>
      </details>
    </section>
  );
}

export function ThreadPreferences({
  state,
  onOpenModels,
}: {
  state: WorkspaceState;
  onOpenModels: () => void;
}) {
  const { t } = useI18n();
  const models = useProduct("thread-preferences-models", api.models);
  const task = state.tasks.find((item) => item.thread_id === state.threadId);
  const snapshot = state.snapshot?.thread_id === state.threadId ? state.snapshot : null;
  const modelSource =
    snapshot?.model_source === "thread"
      ? t("此线程指定")
      : snapshot?.model_source === "task"
        ? t("子任务派发时继承")
        : t("全局默认");
  const effectiveModel = snapshot?.effective_model ?? null;
  const override = snapshot?.profile_override_name ?? "";
  const profileExists = models.data?.profiles.some((item) => item.name === override);
  const trust = trustLevels.find((item) => item.value === snapshot?.trust_level);

  return (
    <section>
      <ScopeTitle
        scope="当前线程"
        title="模型与信任"
        description="仅影响当前选中的 Agent 线程。模型更改在下一次运行时生效。"
      />
      {!state.threadId ? (
        <div className={shared.empty}>{t("请先选择会话。")}</div>
      ) : !snapshot ? (
        <div className={shared.empty}>{t("正在读取会话信息…")}</div>
      ) : (
        <>
          <div className={styles.threadContext}>
            <strong>{task?.title || displaySessionTitle(snapshot?.title, t)}</strong>
            <span title={state.threadId}>{shortId(state.threadId, 18)}</span>
          </div>
          <div className={styles.settingList}>
            <div className={styles.modelSetting}>
              <label htmlFor="settings-thread-model">{t("线程模型")}</label>
              <p>{t("选择具体模型会覆盖继承值；清除覆盖后继续使用该线程的继承模型。")}</p>
              <div className={styles.modelSelectRow}>
                <select
                  id="settings-thread-model"
                  className={shared.select}
                  value={override}
                  disabled={!snapshot || models.loading || models.busy || state.busy}
                  onChange={(event) => {
                    void state.setThreadModel(event.target.value || null).catch(() => {});
                  }}
                >
                  <option value="">{t("跟随继承模型")}</option>
                  {override && !profileExists && (
                    <option value={override}>
                      {override} · {t("配置已移除")}
                    </option>
                  )}
                  {models.data?.profiles.map((item) => (
                    <option value={item.name} key={item.name}>
                      {item.name}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  className={shared.iconButton}
                  onClick={() => void models.refresh()}
                  aria-label={t("刷新模型列表")}
                  title={t("刷新模型列表")}
                >
                  <RotateCw size={15} />
                </button>
              </div>
              {models.error && (
                <p className={styles.error} role="alert">
                  {models.error}
                </p>
              )}
              <p className={styles.effectiveValue}>
                {t("当前生效")}：<strong>{effectiveModel || t("未配置")}</strong>
                <span>{modelSource}</span>
              </p>
              {!models.data?.profiles.length && !models.loading && (
                <button type="button" className={shared.secondaryButton} onClick={onOpenModels}>
                  {t("添加模型")} <ArrowRight size={14} />
                </button>
              )}
            </div>
            <div className={styles.trustSetting}>
              <h3>{t("信任档位")}</h3>
              <p>
                {t("当前生效")}：<strong>{t(trust?.label ?? "未配置")}</strong>
                <span className={styles.sourceTag}>{t("当前线程")}</span>
              </p>
              <div className={styles.trustOptions}>
                {trustLevels.map((item) => (
                  <button
                    type="button"
                    key={item.value}
                    className={[
                      styles.trustOption,
                      snapshot?.trust_level === item.value ? styles.selectedTrust : "",
                    ].join(" ")}
                    disabled={!snapshot || state.busy}
                    aria-pressed={snapshot?.trust_level === item.value}
                    onClick={() => {
                      void state.setTrust(item.value).catch(() => {});
                    }}
                  >
                    <span>
                      <strong>{t(item.label)}</strong>
                      <small>{t(item.detail)}</small>
                    </span>
                    {snapshot?.trust_level === item.value && <CircleCheck size={17} />}
                  </button>
                ))}
              </div>
            </div>
          </div>
        </>
      )}
    </section>
  );
}
