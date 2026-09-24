import { useMemo } from "react";
import { Check, ChevronRight, Clock3, LoaderCircle, TriangleAlert } from "lucide-react";
import type { Activity, StreamEvent } from "../api/types";
import { activityTarget, projectActivity, type ActivityStep } from "../state/activity";
import { useI18n } from "../i18n";
import styles from "./InlineActivity.module.css";

interface InlineActivityProps {
  activity: Activity[];
  liveEvents: StreamEvent[];
  agentName: string;
  agentColor: string;
  onOpenTrajectory?: () => void;
}

function stepName(step: ActivityStep): string {
  if (step.kind === "tool") return step.name ?? "工具";
  if (step.kind === "model") return "模型请求";
  if (step.kind === "approval") return "审批";
  if (step.kind === "task") return "子 Agent";
  return step.type.replaceAll(".", " · ");
}

export function InlineActivity({
  activity,
  liveEvents,
  agentName,
  agentColor,
  onOpenTrajectory,
}: InlineActivityProps) {
  const { t } = useI18n();
  const groups = useMemo(() => projectActivity(activity, liveEvents), [activity, liveEvents]);
  const latest = groups.at(-1);
  const steps = latest?.steps.filter((step) => step.kind !== "other").slice(-5) ?? [];
  if (!steps.length && latest?.status !== "running") return null;

  return (
    <section className={styles.panel} aria-label={t("{name} 的最新活动", { name: agentName })}>
      <div className={styles.heading}>
        <span className={styles.agent} style={{ color: agentColor }}>
          {agentName}
        </span>
        <strong>{t("当前运行活动")}</strong>
        {onOpenTrajectory && (
          <button type="button" onClick={onOpenTrajectory}>
            {t("查看完整轨迹")} <ChevronRight size={13} aria-hidden="true" />
          </button>
        )}
      </div>
      <ol className={styles.list}>
        {!steps.length && (
          <li data-status="running">
            <span className={styles.icon} aria-hidden="true">
              <LoaderCircle size={14} />
            </span>
            <span className={styles.name}>{t("正在等待下一步事件")}</span>
          </li>
        )}
        {steps.map((step) => {
          const target = activityTarget(step);
          return (
            <li key={step.id} data-status={step.status}>
              <span className={styles.icon} aria-hidden="true">
                {step.status === "failed" ? (
                  <TriangleAlert size={14} />
                ) : step.status === "completed" ? (
                  <Check size={14} />
                ) : step.status === "running" ? (
                  <LoaderCircle size={14} />
                ) : (
                  <Clock3 size={14} />
                )}
              </span>
              <span className={styles.name}>{t(stepName(step))}</span>
              {target && (
                <span className={styles.target} title={target}>
                  {target}
                </span>
              )}
              <span className={styles.status}>
                {t(
                  step.status === "running"
                    ? "运行中"
                    : step.status === "completed"
                      ? "已完成"
                      : step.status === "failed"
                        ? "失败"
                        : step.status === "paused"
                          ? "等待批准"
                          : step.status === "stopped"
                            ? "已停止"
                            : step.status === "pending"
                              ? "待开始"
                              : "状态待确认",
                )}
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
