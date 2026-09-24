import { useState } from "react";
import { CirclePlus, FlaskConical, Pencil, Trash2 } from "lucide-react";
import { api } from "../api/client";
import type { ModelInput, ModelProfile } from "../api/types";
import { useProduct } from "./useProduct";
import shared from "../styles/shared.module.css";
import styles from "./panel.module.css";
import { useI18n } from "../i18n";
import { finishModelTest, noModelTest, startModelTest } from "./modelTest";

const protocols = [
  ["openai_chat_completions", "OpenAI Chat Completions"],
  ["openai_responses", "OpenAI Responses API"],
  ["anthropic_messages", "Anthropic Messages"],
  ["gemini_generate_content", "Gemini generateContent"],
  ["ollama_native_chat", "Ollama native chat"],
] as const;

const blank: ModelInput = {
  name: "",
  protocol: protocols[0][0],
  base_url: "",
  api_key: "",
  model_id: "",
  context_length: 128000,
  max_output_tokens: 8192,
};

export function ModelsPanel() {
  const { t, language } = useI18n();
  const resource = useProduct("models", api.models);
  const [noAuth, setNoAuth] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [form, setForm] = useState<ModelInput | null>(null);
  const [test, setTest] = useState(noModelTest);
  const [deleteName, setDeleteName] = useState<string | null>(null);
  const update = (key: keyof ModelInput, value: string | number) =>
    setForm((old) => (old ? { ...old, [key]: value } : old));
  const edit = (item: ModelProfile) => {
    setEditing(item.name);
    setNoAuth(false);
    setForm({
      name: item.name,
      protocol: item.protocol,
      base_url: item.base_url,
      api_key: "",
      model_id: item.model_id,
      context_length: item.context_length,
      max_output_tokens: item.max_output_tokens,
    });
  };
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!form) return;
    const work = editing
      ? () =>
          api.updateModel(editing, {
            protocol: form.protocol,
            base_url: form.base_url,
            model_id: form.model_id,
            context_length: form.context_length,
            max_output_tokens: form.max_output_tokens,
            ...(noAuth ? { api_key: null } : form.api_key?.trim() ? { api_key: form.api_key } : {}),
          })
      : () => api.addModel({ ...form, api_key: noAuth ? null : form.api_key });
    void resource
      .run(work)
      .then(() => {
        setEditing(null);
        setNoAuth(false);
        setForm(null);
      })
      .catch(() => {});
  };
  return (
    <section>
      <div className={styles.head}>
        <h2>{t("模型连接")}</h2>
        <p>{t("按照端点实际支持的协议配置。API Key 只写入本机配置，不会在此回显。")}</p>
      </div>
      <button
        className={shared.secondaryButton}
        onClick={() => {
          setEditing(null);
          setNoAuth(false);
          setForm({ ...blank });
        }}
      >
        <CirclePlus size={15} />
        {t("添加模型")}
      </button>
      {resource.error && (
        <div role="alert" className={styles.notice}>
          {resource.error}
        </div>
      )}
      {form && (
        <form className={styles.form} onSubmit={submit}>
          <h3>{editing ? t("编辑 {name}", { name: editing }) : t("添加模型")}</h3>
          <label className={shared.label} htmlFor="profile-name">
            {t("配置名")}
          </label>
          <input
            id="profile-name"
            className={shared.field}
            value={form.name ?? ""}
            disabled={Boolean(editing)}
            onChange={(event) => update("name", event.target.value)}
            required
          />
          <label className={shared.label} htmlFor="profile-protocol">
            {t("接口协议")}
          </label>
          <select
            id="profile-protocol"
            className={shared.select}
            value={form.protocol}
            onChange={(event) => update("protocol", event.target.value)}
          >
            {protocols.map(([value, label]) => (
              <option value={value} key={value}>
                {label}
              </option>
            ))}
          </select>
          <label className={shared.label} htmlFor="profile-url">
            {t("接口地址")}
          </label>
          <input
            id="profile-url"
            type="url"
            className={shared.field}
            value={form.base_url}
            onChange={(event) => update("base_url", event.target.value)}
            required
            placeholder="https://api.example.com"
          />
          <label className={shared.label} htmlFor="profile-key">
            {editing ? t("API Key（留空保持原值）") : "API Key"}
          </label>
          <input
            id="profile-key"
            type="password"
            autoComplete="new-password"
            className={shared.field}
            value={form.api_key ?? ""}
            onChange={(event) => update("api_key", event.target.value)}
            required={!editing && !noAuth}
            disabled={noAuth}
          />
          <label className={styles.checkRow}>
            <input
              type="checkbox"
              checked={noAuth}
              onChange={(event) => setNoAuth(event.target.checked)}
            />
            {t("端点不需要真实 API Key")}
          </label>
          <p>{t("不会读取环境变量；适配器可能发送非秘密占位凭据，端点需接受该请求。")}</p>
          <label className={shared.label} htmlFor="profile-id">
            {t("模型 ID")}
          </label>
          <input
            id="profile-id"
            className={shared.field}
            value={form.model_id}
            onChange={(event) => update("model_id", event.target.value)}
            required
          />
          <div className={styles.columns}>
            <div>
              <label className={shared.label} htmlFor="context-length">
                {t("上下文长度")}
              </label>
              <input
                id="context-length"
                type="number"
                min="1"
                className={shared.field}
                value={form.context_length}
                onChange={(event) => update("context_length", Number(event.target.value))}
                required
              />
            </div>
            <div>
              <label className={shared.label} htmlFor="max-output">
                {t("最大输出")}
              </label>
              <input
                id="max-output"
                type="number"
                min="1"
                className={shared.field}
                value={form.max_output_tokens}
                onChange={(event) => update("max_output_tokens", Number(event.target.value))}
                required
              />
            </div>
          </div>
          <div className={styles.row}>
            <button type="submit" className={shared.button} disabled={resource.busy}>
              {t(resource.busy ? "正在保存…" : "保存模型")}
            </button>
            <button
              type="button"
              className={shared.ghostButton}
              onClick={() => {
                setForm(null);
                setEditing(null);
              }}
            >
              {t("取消")}
            </button>
          </div>
        </form>
      )}
      {resource.loading ? (
        <div className={styles.loading}>{t("正在读取模型…")}</div>
      ) : (
        <div className={styles.list}>
          {resource.data?.profiles.map((item) => (
            <article key={item.name} className={styles.card}>
              <div className={styles.cardTitle}>
                <strong>{item.name}</strong>
                {item.name === resource.data?.active_profile && (
                  <span className={`${styles.badge} ${styles.activeBadge}`}>{t("当前使用")}</span>
                )}
              </div>
              <p>
                {item.model_id} ·{" "}
                {protocols.find(([value]) => value === item.protocol)?.[1] ?? item.protocol}
              </p>
              <div className={styles.meta}>
                <span>{item.base_url}</span>
                <span>
                  {t("上下文 {count}", {
                    count: item.context_length.toLocaleString(
                      language === "en" ? "en-US" : "zh-CN",
                    ),
                  })}
                </span>
                <span>
                  {t("输出 {count}", {
                    count: item.max_output_tokens.toLocaleString(
                      language === "en" ? "en-US" : "zh-CN",
                    ),
                  })}
                </span>
                <span>{t(item.has_api_key ? "凭据已保存" : "无凭据")}</span>
              </div>
              <div className={styles.row} style={{ marginTop: 12 }}>
                <button
                  className={`${shared.secondaryButton} ${shared.small}`}
                  disabled={resource.busy || item.name === resource.data?.active_profile}
                  onClick={() => {
                    void resource.run(() => api.useModel(item.name)).catch(() => {});
                  }}
                >
                  {t("设为当前模型")}
                </button>
                <button
                  className={`${shared.ghostButton} ${shared.small}`}
                  onClick={() => edit(item)}
                >
                  <Pencil size={13} />
                  {t("编辑")}
                </button>
                <button
                  className={`${shared.ghostButton} ${shared.small}`}
                  disabled={resource.busy || test.busy}
                  onClick={() => {
                    setTest(startModelTest(item.name));
                    void api
                      .testModel(item.name)
                      .then((result) =>
                        setTest((old) =>
                          finishModelTest(
                            old,
                            item.name,
                            t("{result} · 文本 {text} · 工具 {tools} · 流式 {stream}", {
                              result: t(result.ok ? "连接通过" : "测试失败"),
                              text: t(result.text ? "通过" : "未通过"),
                              tools: t(result.tool_calling ? "通过" : "未通过"),
                              stream: t(result.stream ? "通过" : "未通过"),
                            }) +
                              (Object.keys(result.errors).length
                                ? `\n${JSON.stringify(result.errors, null, 2)}`
                                : ""),
                          ),
                        ),
                      )
                      .catch((reason: unknown) =>
                        setTest((old) =>
                          finishModelTest(
                            old,
                            item.name,
                            reason instanceof Error ? reason.message : t("测试失败"),
                          ),
                        ),
                      );
                  }}
                >
                  <FlaskConical size={13} />
                  {test.busy && test.profile === item.name ? t("测试中…") : t("测试")}
                </button>
                <button
                  className={`${shared.ghostButton} ${shared.small}`}
                  onClick={() => setDeleteName(item.name)}
                >
                  <Trash2 size={13} />
                  {t("删除")}
                </button>
              </div>
              {test.profile === item.name && test.result && (
                <pre className={styles.monoblock}>{test.result}</pre>
              )}
              {deleteName === item.name && (
                <div className={styles.confirm}>
                  <p>{t("确定删除模型配置 {name}？", { name: item.name })}</p>
                  <div className={styles.row}>
                    <button
                      className={`${shared.dangerButton} ${shared.small}`}
                      onClick={() => {
                        void resource
                          .run(() => api.deleteModel(item.name))
                          .then(() => setDeleteName(null))
                          .catch(() => {});
                      }}
                    >
                      {t("确认删除")}
                    </button>
                    <button
                      className={`${shared.ghostButton} ${shared.small}`}
                      onClick={() => setDeleteName(null)}
                    >
                      {t("取消")}
                    </button>
                  </div>
                </div>
              )}
            </article>
          ))}
          {!resource.data?.profiles.length && (
            <div className={shared.empty}>{t("尚无模型配置。添加后即可启动 Agent。")}</div>
          )}
        </div>
      )}
    </section>
  );
}
