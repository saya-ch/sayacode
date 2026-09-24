import { useState } from "react";
import { Check, Pencil, Pin, Search, Trash2 } from "lucide-react";
import { api } from "../api/client";
import type { MemoryDetail, MemorySettings } from "../api/types";
import { useI18n } from "../i18n";
import { relativeTime } from "../lib/format";
import { useProduct } from "./useProduct";
import shared from "../styles/shared.module.css";
import styles from "./panel.module.css";

const memoryStateLabels: Record<string, string> = {
  active: "已确认",
  candidate: "待确认",
  needs_verification: "待核实",
  replaced: "已替代",
};

const evidenceRoleLabels: Record<string, string> = {
  user: "用户",
  assistant: "助手",
  tool: "工具",
  system: "系统",
};

export function MemoryPanel({ workspaceId }: { workspaceId: string | null }) {
  const { language, t } = useI18n();
  const [scope, setScope] = useState<"all" | "user" | "project">("all");
  const [draftQuery, setDraftQuery] = useState("");
  const [query, setQuery] = useState("");
  const [newScope, setNewScope] = useState<"user" | "project">("user");
  const [newText, setNewText] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [forgetId, setForgetId] = useState<string | null>(null);
  const [detail, setDetail] = useState<MemoryDetail | null>(null);
  const [detailError, setDetailError] = useState<{ id: string; message: string } | null>(null);
  const settings = useProduct("memory-settings", api.memorySettings);
  const records = useProduct(`memory:${workspaceId}:${scope}:${query}`, () =>
    workspaceId
      ? api.memories(workspaceId, scope === "all" ? undefined : scope, query || undefined)
      : Promise.resolve([]),
  );
  const changeSettings = (patch: Partial<MemorySettings>) => {
    if (workspaceId)
      void settings.run(() => api.updateMemorySettings(workspaceId, patch)).catch(() => {});
  };
  const add = (event: React.FormEvent) => {
    event.preventDefault();
    if (!workspaceId || !newText.trim()) return;
    void records
      .run(() => api.remember(workspaceId, newScope, newText.trim()))
      .then(() => setNewText(""))
      .catch(() => {});
  };
  return (
    <section>
      <div className={styles.head}>
        <h2>{t("跨会话记忆")}</h2>
        <p>{t("用户记忆跨项目可用，项目记忆只在所属工作区生效。可查看、修正、确认或忘记。")}</p>
      </div>
      {(settings.error || records.error) && (
        <div role="alert" className={styles.notice}>
          {settings.error || records.error}
        </div>
      )}
      {settings.data && (
        <div className={styles.card}>
          <div className={styles.rowBetween}>
            <strong>{t("记忆系统")}</strong>
            <label className={styles.checkRow}>
              <input
                type="checkbox"
                checked={settings.data.enabled}
                onChange={(event) => changeSettings({ enabled: event.target.checked })}
                disabled={!workspaceId || settings.busy}
              />
              {settings.data.enabled ? t("已开启") : t("已关闭")}
            </label>
          </div>
          <div className={styles.rowBetween}>
            <span className={shared.muted}>{t("将已保存记忆用于新会话")}</span>
            <label className={styles.checkRow}>
              <input
                type="checkbox"
                checked={settings.data.use}
                onChange={(event) => changeSettings({ use: event.target.checked })}
                disabled={!workspaceId || settings.busy}
              />
              {t("使用")}
            </label>
          </div>
          <label className={shared.label} htmlFor="memory-learn">
            {t("自动学习")}
          </label>
          <select
            id="memory-learn"
            className={shared.select}
            value={settings.data.learn}
            onChange={(event) =>
              changeSettings({ learn: event.target.value as MemorySettings["learn"] })
            }
            disabled={!workspaceId || settings.busy}
          >
            <option value="off">{t("关闭")}</option>
            <option value="explicit">{t("仅显式提出")}</option>
            <option value="auto">{t("对话后自动整理")}</option>
          </select>
          <p style={{ color: "var(--text-faint)", fontSize: 12, margin: "9px 0 0" }}>
            {t("自动整理在后台进行，不会把失败计入主 Agent 任务。")}
          </p>
        </div>
      )}
      <form className={styles.form} onSubmit={add}>
        <h3>{t("添加记忆")}</h3>
        <div className={styles.columns}>
          <div>
            <label className={shared.label} htmlFor="memory-new-scope">
              {t("范围")}
            </label>
            <select
              id="memory-new-scope"
              className={shared.select}
              value={newScope}
              onChange={(event) => setNewScope(event.target.value as "user" | "project")}
            >
              <option value="user">{t("用户")}</option>
              <option value="project">{t("当前项目")}</option>
            </select>
          </div>
        </div>
        <label className={shared.label} htmlFor="memory-new-text">
          {t("内容")}
        </label>
        <textarea
          id="memory-new-text"
          className={shared.textarea}
          value={newText}
          onChange={(event) => setNewText(event.target.value)}
          rows={3}
          required
          placeholder={t("例如：我偏好先写测试再改实现")}
        />
        <button
          className={shared.button}
          disabled={!workspaceId || records.busy || !newText.trim()}
        >
          {t("保存记忆")}
        </button>
      </form>
      <hr className={styles.divider} />
      <div className={styles.row}>
        <select
          aria-label={t("记忆范围筛选")}
          className={shared.select}
          style={{ width: 120 }}
          value={scope}
          onChange={(event) => setScope(event.target.value as "all" | "user" | "project")}
        >
          <option value="all">{t("全部范围")}</option>
          <option value="user">{t("用户")}</option>
          <option value="project">{t("项目")}</option>
        </select>
        <input
          className={shared.field}
          style={{ flex: 1, minWidth: 140 }}
          aria-label={t("搜索记忆")}
          value={draftQuery}
          onChange={(event) => setDraftQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") setQuery(draftQuery.trim());
          }}
          placeholder={t("搜索已保存记忆")}
        />
        <button className={shared.secondaryButton} onClick={() => setQuery(draftQuery.trim())}>
          <Search size={14} />
          {t("搜索")}
        </button>
      </div>
      {records.loading ? (
        <div className={styles.loading}>{t("正在读取记忆…")}</div>
      ) : (
        <div className={styles.list}>
          {records.data?.map((record) => (
            <article key={record.id} className={styles.card}>
              <div className={styles.cardTitle}>
                <strong>{record.subject || t("记忆")}</strong>
                <span className={styles.badge}>
                  {t(record.pinned ? "{scope} · {state} · 已固定" : "{scope} · {state}", {
                    scope: record.scope === "user" ? t("用户") : t("项目"),
                    state: t(memoryStateLabels[record.state] ?? record.state),
                  })}
                </span>
              </div>
              {editingId === record.id ? (
                <>
                  <textarea
                    className={shared.textarea}
                    rows={3}
                    value={editText}
                    onChange={(event) => setEditText(event.target.value)}
                    aria-label={t("修改记忆")}
                  />
                  <div className={styles.row} style={{ marginTop: 7 }}>
                    <button
                      className={`${shared.button} ${shared.small}`}
                      disabled={!workspaceId || records.busy || !editText.trim()}
                      onClick={() => {
                        if (workspaceId)
                          void records
                            .run(() => api.correctMemory(workspaceId, record.id, editText.trim()))
                            .then(() => setEditingId(null))
                            .catch(() => {});
                      }}
                    >
                      {t("保存修正")}
                    </button>
                    <button
                      className={`${shared.ghostButton} ${shared.small}`}
                      onClick={() => setEditingId(null)}
                    >
                      {t("取消")}
                    </button>
                  </div>
                </>
              ) : (
                <p>{record.text}</p>
              )}
              <div className={styles.meta}>
                <span>{relativeTime(record.updated_at, t, language)}</span>
                <span>{record.id.slice(0, 12)}</span>
              </div>
              <div className={styles.row} style={{ marginTop: 10 }}>
                <button
                  className={`${shared.ghostButton} ${shared.small}`}
                  disabled={!workspaceId}
                  onClick={() => {
                    if (!workspaceId) return;
                    if (detail?.id === record.id) {
                      setDetail(null);
                      return;
                    }
                    setDetailError(null);
                    void api
                      .memoryDetail(workspaceId, record.id)
                      .then(setDetail)
                      .catch((reason: unknown) =>
                        setDetailError({
                          id: record.id,
                          message: reason instanceof Error ? reason.message : t("读取依据失败"),
                        }),
                      );
                  }}
                >
                  {t("查看依据")}
                </button>
                <button
                  className={`${shared.ghostButton} ${shared.small}`}
                  disabled={!workspaceId || records.busy}
                  onClick={() => {
                    if (workspaceId)
                      void records
                        .run(() => api.pinMemory(workspaceId, record.id, !record.pinned))
                        .catch(() => {});
                  }}
                >
                  <Pin size={13} />
                  {record.pinned ? t("取消固定") : t("固定")}
                </button>
                <button
                  className={`${shared.ghostButton} ${shared.small}`}
                  onClick={() => {
                    setEditingId(record.id);
                    setEditText(record.text);
                  }}
                >
                  <Pencil size={13} />
                  {t("修正")}
                </button>
                {record.state !== "active" && (
                  <button
                    className={`${shared.secondaryButton} ${shared.small}`}
                    disabled={!workspaceId || records.busy}
                    onClick={() => {
                      if (workspaceId)
                        void records
                          .run(() => api.confirmMemory(workspaceId, record.id))
                          .catch(() => {});
                    }}
                  >
                    <Check size={13} />
                    {t("确认")}
                  </button>
                )}
                <button
                  className={`${shared.ghostButton} ${shared.small}`}
                  onClick={() => setForgetId(record.id)}
                >
                  <Trash2 size={13} />
                  {t("忘记")}
                </button>
              </div>
              {detail?.id === record.id && (
                <div className={styles.monoblock}>
                  <strong>{t("记忆依据")}</strong>
                  {detail.validity_reason && <p>{detail.validity_reason}</p>}
                  {detail.evidence.length ? (
                    detail.evidence.map((evidence, index) => (
                      <div key={`${evidence.source_ref}:${evidence.message_id}:${index}`}>
                        <span>
                          {t("{role} · {source}", {
                            role: t(evidenceRoleLabels[evidence.role] ?? evidence.role),
                            source: evidence.source_ref,
                          })}
                        </span>
                        <p>{evidence.preview}</p>
                      </div>
                    ))
                  ) : (
                    <p>{t("尚无可展示的依据预览。")}</p>
                  )}
                </div>
              )}
              {detailError?.id === record.id && (
                <div className={styles.notice} role="alert">
                  {detailError.message}
                </div>
              )}
              {forgetId === record.id && (
                <div className={styles.confirm}>
                  <p>{t("确认忘记这条记忆？后续会话将不再引用它。")}</p>
                  <button
                    className={`${shared.dangerButton} ${shared.small}`}
                    disabled={!workspaceId || records.busy}
                    onClick={() => {
                      if (workspaceId)
                        void records
                          .run(() => api.forgetMemory(workspaceId, record.id))
                          .then(() => setForgetId(null))
                          .catch(() => {});
                    }}
                  >
                    {t("确认忘记")}
                  </button>
                  <button
                    className={`${shared.ghostButton} ${shared.small}`}
                    onClick={() => setForgetId(null)}
                  >
                    {t("取消")}
                  </button>
                </div>
              )}
            </article>
          ))}
          {!records.data?.length && (
            <div className={shared.empty}>{t("当前范围没有匹配的记忆。")}</div>
          )}
        </div>
      )}
    </section>
  );
}
