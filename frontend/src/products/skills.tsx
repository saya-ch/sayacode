import { api } from "../api/client";
import { useProduct } from "./useProduct";
import shared from "../styles/shared.module.css";
import styles from "./panel.module.css";
import { useI18n } from "../i18n";
import type { WorkspaceState } from "../state/useWorkspace";
import { canEditThreadContext } from "../lib/threadContext";
import { ThreadContextStatus } from "../components/ThreadContextStatus";

export function SkillsPanel({ state }: { state: WorkspaceState }) {
  const { t } = useI18n();
  const { workspaceId, threadId } = state;
  const resource = useProduct(`skills:${workspaceId}:${threadId}`, () =>
    workspaceId ? api.skills(workspaceId, threadId) : Promise.resolve([]),
  );
  const activeSkills =
    state.snapshot?.thread_id === threadId ? (state.snapshot.active_skills ?? []) : [];
  const canChange = canEditThreadContext(state) && !resource.busy;
  return (
    <section>
      <div className={styles.head}>
        <h2>Skills</h2>
        <p>{t("查看当前工作区的可用 Skill。激活操作作用于当前选中的 Agent 线程。")}</p>
      </div>
      <ThreadContextStatus feedback={state.contextFeedback} className={styles.feedback} />
      {threadId && !canEditThreadContext(state) && (
        <p className={styles.feedback}>{t("请在线程空闲时修改 Skill")}</p>
      )}
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
                  disabled={!canChange}
                  aria-pressed={activeSkills.includes(skill.name)}
                  onClick={() => {
                    if (threadId)
                      void resource
                        .run(() =>
                          activeSkills.includes(skill.name)
                            ? state.deactivateSkill(skill.name)
                            : state.activateSkill(skill.name),
                        )
                        .catch(() => {});
                  }}
                >
                  {t(activeSkills.includes(skill.name) ? "解除激活" : "在当前线程激活")}
                </button>
                {activeSkills.includes(skill.name) && (
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
