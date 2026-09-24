import { useState } from "react";
import { FlaskConical } from "lucide-react";
import { api } from "../api/client";
import { useI18n } from "../i18n";
import { useProduct } from "./useProduct";
import shared from "../styles/shared.module.css";
import styles from "./panel.module.css";

export function ReviewerPanel() {
  const { t } = useI18n();
  const resource = useProduct("reviewer", api.reviewer);
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [modelId, setModelId] = useState("");
  const [testResult, setTestResult] = useState<string | null>(null);
  const [removing, setRemoving] = useState(false);
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    void resource
      .run(() =>
        api.configureReviewer({
          base_url: baseUrl.trim(),
          api_key: apiKey,
          model_id: modelId.trim(),
        }),
      )
      .then(() => {
        setApiKey("");
        setBaseUrl("");
        setModelId("");
      })
      .catch(() => {});
  };
  return (
    <section>
      <div className={styles.head}>
        <h2>{t("Jev 自动审理")}</h2>
        <p>
          {t(
            "单独配置审理模型。高风险操作仍按照审理结果进入人工批准，不会改变当前会话的信任档位。",
          )}
        </p>
      </div>
      {resource.error && (
        <div className={styles.notice} role="alert">
          {resource.error}
        </div>
      )}
      {resource.loading ? (
        <div className={styles.loading}>{t("正在读取审理配置…")}</div>
      ) : (
        <div className={styles.card}>
          <div className={styles.cardTitle}>
            <strong>
              {resource.data?.configured ? t("审理模型已配置") : t("尚未配置审理模型")}
            </strong>
            <span
              className={
                resource.data?.configured ? `${styles.badge} ${styles.activeBadge}` : styles.badge
              }
            >
              {resource.data?.configured ? t("可用") : t("未配置")}
            </span>
          </div>
          <div className={styles.meta}>
            <span>{resource.data?.base_url || "—"}</span>
            <span>{resource.data?.model_id || "—"}</span>
            <span>{resource.data?.has_api_key ? t("凭据已保存") : t("无凭据")}</span>
          </div>
          {resource.data?.configured && (
            <div className={styles.row} style={{ marginTop: 12 }}>
              <button
                className={`${shared.ghostButton} ${shared.small}`}
                onClick={() => setRemoving(true)}
              >
                {t("移除审理配置")}
              </button>
            </div>
          )}
          {removing && (
            <div className={styles.confirm}>
              <p>{t("确认移除 Jev 审理配置？当前使用 Jev 信任档的会话需先切换。")}</p>
              <button
                className={`${shared.dangerButton} ${shared.small}`}
                disabled={resource.busy}
                onClick={() => {
                  void resource
                    .run(api.deleteReviewer)
                    .then(() => setRemoving(false))
                    .catch(() => {});
                }}
              >
                {t("确认移除")}
              </button>
              <button
                className={`${shared.ghostButton} ${shared.small}`}
                onClick={() => setRemoving(false)}
              >
                {t("取消")}
              </button>
            </div>
          )}
        </div>
      )}
      <form className={styles.form} onSubmit={submit}>
        <h3>{t("配置审理端点")}</h3>
        <label className={shared.label} htmlFor="reviewer-url">
          {t("接口地址")}
        </label>
        <input
          id="reviewer-url"
          className={shared.field}
          type="url"
          value={baseUrl}
          onChange={(event) => setBaseUrl(event.target.value)}
          required
          placeholder="https://reviewer.example.com"
        />
        <label className={shared.label} htmlFor="reviewer-model">
          {t("模型 ID")}
        </label>
        <input
          id="reviewer-model"
          className={shared.field}
          value={modelId}
          onChange={(event) => setModelId(event.target.value)}
          required
        />
        <label className={shared.label} htmlFor="reviewer-key">
          {t("API Key")}
        </label>
        <input
          id="reviewer-key"
          className={shared.field}
          type="password"
          autoComplete="new-password"
          value={apiKey}
          onChange={(event) => setApiKey(event.target.value)}
          required
        />
        <div className={styles.row}>
          <button className={shared.button} disabled={resource.busy}>
            {t("保存配置")}
          </button>
          <button
            type="button"
            className={shared.secondaryButton}
            disabled={!resource.data?.configured || resource.busy}
            onClick={() => {
              void api
                .testReviewer()
                .then((value) =>
                  setTestResult(
                    value.ok ? t("审理端点测试通过") : value.error || t("审理端点测试失败"),
                  ),
                )
                .catch((reason: unknown) =>
                  setTestResult(reason instanceof Error ? reason.message : t("测试失败")),
                );
            }}
          >
            <FlaskConical size={14} />
            {t("测试已保存配置")}
          </button>
        </div>
        {testResult && (
          <div className={styles.notice} role="status">
            {testResult}
          </div>
        )}
      </form>
    </section>
  );
}
