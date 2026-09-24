import { useState } from "react";
import { Search, ScanSearch } from "lucide-react";
import { api } from "../api/client";
import type { SymbolItem } from "../api/types";
import { useI18n } from "../i18n";
import { useProduct } from "./useProduct";
import shared from "../styles/shared.module.css";
import styles from "./panel.module.css";

const doctorCheckLabels: Record<string, string> = {
  workspace_exists: "工作区存在",
  git: "Git 可用",
  shell: "Shell 可用",
  profile_configured: "模型配置完成",
  mcp: "MCP 可用",
  checkpoints: "检查点可用",
  store: "数据存储可用",
  reviewer: "审理配置可用",
};

export function DiagnosticsPanel({ workspaceId }: { workspaceId: string | null }) {
  const { t } = useI18n();
  const git = useProduct(`git:${workspaceId}`, () =>
    workspaceId
      ? api.gitStatus(workspaceId)
      : Promise.resolve({ branch: null, clean: true, staged: [], unstaged: [], untracked: [] }),
  );
  const doctor = useProduct(`doctor:${workspaceId}`, () =>
    workspaceId ? api.doctor(workspaceId) : Promise.resolve({ ok: true, checks: [] }),
  );
  const [analysis, setAnalysis] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [symbols, setSymbols] = useState<SymbolItem[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await work();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : t("查询失败"));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section>
      <div className={styles.head}>
        <h2>{t("项目诊断")}</h2>
        <p>{t("从真实工作区读取 Git 状态、符号位置、项目分析及运行检查。")}</p>
      </div>
      {(git.error || doctor.error || error) && (
        <div className={styles.notice} role="alert">
          {git.error || doctor.error || error}
        </div>
      )}
      <h3 className={styles.sectionTitle}>{t("Git 状态")}</h3>
      {git.loading ? (
        <div className={styles.loading}>{t("正在检查 Git…")}</div>
      ) : (
        <div className={styles.card}>
          <div className={styles.cardTitle}>
            <strong>{git.data?.branch ?? t("非 Git 工作区")}</strong>
            <span className={`${styles.badge} ${git.data?.clean ? styles.activeBadge : ""}`}>
              {git.data?.clean ? t("干净") : t("有改动")}
            </span>
          </div>
          <div className={styles.meta}>
            <span>{t("已暂存 {count}", { count: git.data?.staged.length ?? 0 })}</span>
            <span>{t("未暂存 {count}", { count: git.data?.unstaged.length ?? 0 })}</span>
            <span>{t("未跟踪 {count}", { count: git.data?.untracked.length ?? 0 })}</span>
          </div>
          {git.data?.text && <pre className={styles.monoblock}>{git.data.text}</pre>}
        </div>
      )}
      <h3 className={styles.sectionTitle}>{t("运行检查")}</h3>
      {doctor.loading ? (
        <div className={styles.loading}>{t("正在运行诊断…")}</div>
      ) : (
        <div className={styles.card}>
          <div className={styles.cardTitle}>
            <strong>{doctor.data?.summary || t("检查结果")}</strong>
            <span className={`${styles.badge} ${doctor.data?.ok ? styles.activeBadge : ""}`}>
              {doctor.data?.ok ? t("正常") : t("需检查")}
            </span>
          </div>
          <table className={styles.table}>
            <tbody>
              {doctor.data?.checks.map((check) => (
                <tr key={check.name}>
                  <td>{t(doctorCheckLabels[check.name] ?? check.name)}</td>
                  <td>
                    {t(
                      check.status === "ok"
                        ? "通过"
                        : check.status === "failed"
                          ? "失败"
                          : check.status,
                    )}
                  </td>
                  <td>
                    {check.detail === "True"
                      ? t("是")
                      : check.detail === "False"
                        ? t("否")
                        : check.detail || "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <hr className={styles.divider} />
      <div className={styles.row}>
        <button
          className={shared.secondaryButton}
          disabled={!workspaceId || busy}
          onClick={() => {
            if (workspaceId) void run(async () => setAnalysis(await api.analysis(workspaceId)));
          }}
        >
          <ScanSearch size={14} />
          {t("分析项目结构")}
        </button>
        <button
          className={shared.ghostButton}
          disabled={git.loading || doctor.loading}
          onClick={() => {
            void Promise.all([git.refresh(), doctor.refresh()]);
          }}
        >
          {t("刷新状态")}
        </button>
      </div>
      {analysis && <pre className={styles.monoblock}>{analysis}</pre>}
      <h3 className={styles.sectionTitle}>{t("符号定位")}</h3>
      <form
        className={styles.row}
        onSubmit={(event) => {
          event.preventDefault();
          if (workspaceId)
            void run(async () => setSymbols(await api.symbols(workspaceId, query.trim())));
        }}
      >
        <input
          className={shared.field}
          style={{ flex: 1, minWidth: 140 }}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t("输入名称或路径")}
          aria-label={t("搜索符号")}
        />
        <button className={shared.secondaryButton} disabled={!workspaceId || busy}>
          <Search size={14} />
          {t("查询")}
        </button>
      </form>
      {symbols && (
        <div className={styles.list}>
          {symbols.map((symbol, index) => (
            <div className={styles.card} key={`${symbol.path}:${symbol.line}:${index}`}>
              <div className={styles.cardTitle}>
                <strong>{symbol.name}</strong>
                <span className={styles.badge}>{t(symbol.kind)}</span>
              </div>
              <div className={styles.meta}>
                {symbol.path}
                {symbol.line ? `:${symbol.line}` : ""}
              </div>
            </div>
          ))}
          {!symbols.length && <div className={shared.empty}>{t("没有找到匹配的符号。")}</div>}
        </div>
      )}
    </section>
  );
}
