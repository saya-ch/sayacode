import { api } from "../api/client";
import { useProduct } from "./useProduct";
import shared from "../styles/shared.module.css";
import styles from "./panel.module.css";
import { useI18n } from "../i18n";

export function SkillsPanel({
  workspaceId,
  threadId,
}: {
  workspaceId: string | null;
  threadId: string | null;
}) {
  const { t } = useI18n();
  const resource = useProduct(`skills:${workspaceId}`, () =>
    workspaceId ? api.skills(workspaceId) : Promise.resolve([]),
  );
  return (
    <section>
      <div className={styles.head}>
        <h2>Skills</h2>
        <p>{t("查看当前工作区的可用 Skill。激活操作作用于当前选中的 Agent 线程。")}</p>
      </div>
      {resource.error && (
        <div className={styles.notice} role="alert">
          {resource.error}
        </div>
      )}
      {resource.loading ? (
        <div className={styles.loading}>{t("正在读取 Skills…")}</div>
      ) : (
        <div className={styles.list}>
          {resource.data?.map((skill) => (
            <article key={`${skill.source}:${skill.name}`} className={styles.card}>
              <div className={styles.cardTitle}>
                <strong>{skill.name}</strong>
                <span className={styles.badge}>{skill.source}</span>
              </div>
              <p>{skill.description || t("未提供描述")}</p>
              <div className={styles.meta}>
                <span>{skill.path}</span>
              </div>
              <div className={styles.row} style={{ marginTop: 11 }}>
                <button
                  className={`${shared.secondaryButton} ${shared.small}`}
                  disabled={!threadId || resource.busy}
                  onClick={() => {
                    if (threadId)
                      void resource
                        .run(() => api.activateSkill(threadId, skill.name))
                        .catch(() => {});
                  }}
                >
                  {t("在当前线程激活")}
                </button>
                {skill.active === true && (
                  <span className={`${styles.badge} ${styles.activeBadge}`}>{t("已激活")}</span>
                )}
              </div>
            </article>
          ))}
          {!resource.data?.length && (
            <div className={shared.empty}>{t("当前工作区没有可用 Skill。")}</div>
          )}
        </div>
      )}
    </section>
  );
}
