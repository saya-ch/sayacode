import { useState } from "react";
import { CirclePlus, RotateCw, Trash2 } from "lucide-react";
import { api } from "../api/client";
import { useProduct } from "./useProduct";
import shared from "../styles/shared.module.css";
import styles from "./panel.module.css";
import { useI18n } from "../i18n";

export function McpPanel({ workspaceId }: { workspaceId: string | null }) {
  const { t } = useI18n();
  const resource = useProduct(`mcp:${workspaceId}`, () =>
    workspaceId
      ? api.mcp(workspaceId)
      : Promise.resolve({ trusted_project: false, servers: [], available_tools: [] }),
  );
  const [name, setName] = useState("");
  const [scope, setScope] = useState<"user" | "project">("project");
  const [config, setConfig] = useState('{\n  "transport": "stdio",\n  "command": ""\n}');
  const [adding, setAdding] = useState(false);
  const [removeName, setRemoveName] = useState<string | null>(null);
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!workspaceId) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(config);
    } catch {
      resource.setError(t("服务器配置必须是有效的 JSON 对象。"));
      return;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      resource.setError(t("服务器配置必须是 JSON 对象。"));
      return;
    }
    void resource
      .run(() =>
        api.addMcp(workspaceId, {
          name: name.trim(),
          scope,
          config: parsed as Record<string, unknown>,
        }),
      )
      .then(() => {
        setAdding(false);
        setName("");
      })
      .catch(() => {});
  };
  return (
    <section>
      <div className={styles.head}>
        <h2>{t("MCP 服务器")}</h2>
        <p>{t("服务器配置与当前工作区绑定。项目级服务器在信任该项目后才会对 Agent 可用。")}</p>
      </div>
      {!workspaceId && <div className={styles.notice}>{t("请先选择工作区。")}</div>}
      {resource.error && (
        <div className={styles.notice} role="alert">
          {resource.error}
        </div>
      )}
      <div className={styles.row}>
        <button
          className={shared.secondaryButton}
          disabled={!workspaceId}
          onClick={() => setAdding((value) => !value)}
        >
          <CirclePlus size={15} />
          {t("添加服务器")}
        </button>
        <button
          className={shared.ghostButton}
          disabled={!workspaceId || resource.busy}
          onClick={() => {
            if (workspaceId) void resource.run(() => api.reloadMcp(workspaceId)).catch(() => {});
          }}
        >
          <RotateCw size={14} />
          {t("重新加载")}
        </button>
      </div>
      {adding && (
        <form className={styles.form} onSubmit={submit}>
          <h3>{t("添加 MCP 服务器")}</h3>
          <label className={shared.label} htmlFor="mcp-name">
            {t("名称")}
          </label>
          <input
            id="mcp-name"
            className={shared.field}
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
          />
          <label className={shared.label} htmlFor="mcp-scope">
            {t("作用域")}
          </label>
          <select
            id="mcp-scope"
            className={shared.select}
            value={scope}
            onChange={(event) => setScope(event.target.value as "user" | "project")}
          >
            <option value="project">{t("当前项目")}</option>
            <option value="user">{t("所有工作区")}</option>
          </select>
          <label className={shared.label} htmlFor="mcp-config">
            {t("服务器配置（JSON）")}
          </label>
          <textarea
            id="mcp-config"
            className={shared.textarea}
            rows={8}
            spellCheck={false}
            value={config}
            onChange={(event) => setConfig(event.target.value)}
          />
          <div className={styles.row}>
            <button className={shared.button} disabled={resource.busy}>
              {t("保存服务器")}
            </button>
            <button type="button" className={shared.ghostButton} onClick={() => setAdding(false)}>
              {t("取消")}
            </button>
          </div>
        </form>
      )}
      {resource.loading ? (
        <div className={styles.loading}>{t("正在读取服务器…")}</div>
      ) : (
        <>
          <div className={styles.list}>
            {resource.data?.servers.map((server) => (
              <article key={`${server.scope}:${server.name}`} className={styles.card}>
                <div className={styles.cardTitle}>
                  <strong>{server.name}</strong>
                  <span className={styles.badge}>
                    {t(server.scope === "project" ? "项目" : "用户")}
                  </span>
                </div>
                <div className={styles.meta}>
                  <span>{t("状态 {status}", { status: server.status })}</span>
                  {server.tools && (
                    <span>{t("{count} 件工具", { count: server.tools.length })}</span>
                  )}
                </div>
                {server.error && (
                  <p style={{ color: "var(--danger)", marginTop: 9 }}>{server.error}</p>
                )}
                <div className={styles.row} style={{ marginTop: 9 }}>
                  <button
                    className={`${shared.ghostButton} ${shared.small}`}
                    onClick={() => setRemoveName(`${server.scope}:${server.name}`)}
                  >
                    <Trash2 size={13} />
                    {t("移除")}
                  </button>
                </div>
                {removeName === `${server.scope}:${server.name}` && (
                  <div className={styles.confirm}>
                    <p>{t("确认移除 {name}？", { name: server.name })}</p>
                    <button
                      className={`${shared.dangerButton} ${shared.small}`}
                      disabled={resource.busy}
                      onClick={() => {
                        if (workspaceId)
                          void resource
                            .run(() =>
                              api.removeMcp(
                                workspaceId,
                                server.name,
                                server.scope as "user" | "project",
                              ),
                            )
                            .then(() => setRemoveName(null))
                            .catch(() => {});
                      }}
                    >
                      {t("确认移除")}
                    </button>
                    <button
                      className={`${shared.ghostButton} ${shared.small}`}
                      onClick={() => setRemoveName(null)}
                    >
                      {t("取消")}
                    </button>
                  </div>
                )}
              </article>
            ))}
            {!resource.data?.servers.length && (
              <div className={shared.empty}>{t("尚未配置 MCP 服务器。")}</div>
            )}
          </div>
          <hr className={styles.divider} />
          <div className={styles.rowBetween}>
            <div>
              <strong>{t("信任当前项目")}</strong>
              <p style={{ color: "var(--text-faint)", margin: "3px 0 0", fontSize: 11 }}>
                {t("允许项目级 MCP 服务器向 Agent 提供工具。")}
              </p>
            </div>
            <button
              className={resource.data?.trusted_project ? shared.secondaryButton : shared.button}
              disabled={!workspaceId || resource.busy}
              onClick={() => {
                if (workspaceId)
                  void resource
                    .run(() => api.trustMcp(workspaceId, !resource.data?.trusted_project))
                    .catch(() => {});
              }}
            >
              {t(resource.data?.trusted_project ? "撤销信任" : "信任项目")}
            </button>
          </div>
          {resource.data?.available_tools?.length ? (
            <>
              <h3 className={styles.sectionTitle}>{t("当前可用工具")}</h3>
              <div className={styles.row}>
                {resource.data.available_tools.map((tool) => (
                  <span className={styles.badge} key={tool}>
                    {tool}
                  </span>
                ))}
              </div>
            </>
          ) : null}
        </>
      )}
    </section>
  );
}
