import { useEffect, useState } from "react";
import { Check, ChevronRight, Clock3, LoaderCircle, TriangleAlert } from "lucide-react";
import type { ActiveRun } from "../api/types";
import { elapsed } from "../lib/format";
import type { TimelineProcessItem, TimelineProcessRow } from "../lib/conversationTimeline";
import { activityTarget } from "../state/activity";
import { useI18n } from "../i18n";
import styles from "./ConversationProcess.module.css";

function targetOf(item: TimelineProcessItem): string | null {
  const input = item.message?.tool_input;
  if (input) {
    for (const key of ["path", "file_path", "command", "pattern", "query", "url", "cwd"]) {
      const value = input[key];
      if (typeof value === "string" && value) return value.replace(/\s+/g, " ").trim();
    }
  }
  return item.activity ? activityTarget(item.activity) : null;
}

function nameOf(item: TimelineProcessItem): string {
  return item.kind === "model"
    ? "模型请求"
    : (item.message?.tool_name ?? item.activity?.name ?? "工具调用");
}

function durationOf(item: TimelineProcessItem, now: number): string | null {
  if (item.status === "running") return elapsed(item.activity?.at, now) || null;
  const value = item.activity?.durationMs;
  if (value == null) return null;
  return value < 1000 ? `${Math.round(value)}ms` : `${(value / 1000).toFixed(1)}s`;
}

function statusIcon(status: TimelineProcessItem["status"]) {
  if (status === "running") return <LoaderCircle className={styles.spinner} size={15} />;
  if (status === "failed") return <TriangleAlert size={15} />;
  if (status === "completed") return <Check size={15} />;
  return <Clock3 size={15} />;
}

export function ConversationProcess({
  row,
  activeRun,
  runStatus,
}: {
  row: TimelineProcessRow;
  activeRun?: ActiveRun | null;
  runStatus?: string;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!row.active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [row.active]);

  const runningItem = [...row.items].reverse().find((item) => item.status === "running");
  const toolCount = row.items.filter((item) => item.kind === "tool").length;
  const failed = row.items.some((item) => item.status === "failed");
  const stopping = row.active && (runStatus === "stopping" || activeRun?.status === "stopping");
  const target = runningItem?.kind === "tool" ? targetOf(runningItem) : null;
  const title = stopping
    ? t("正在停止本轮运行")
    : row.active && runningItem
      ? runningItem.kind === "model"
        ? t("正在思考")
        : `${t("正在执行工具")} · ${t(nameOf(runningItem))}`
      : row.active
        ? t("正在等待下一步事件")
        : failed
          ? t("有失败的工具调用")
          : t("执行了 {count} 项操作", { count: toolCount });
  const runTime = row.active && activeRun?.started_at ? elapsed(activeRun.started_at, now) : "";

  return (
    <section className={styles.process} data-active={row.active} aria-busy={row.active}>
      <button
        type="button"
        className={styles.summary}
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className={styles.summaryIcon} aria-hidden="true">
          {row.active ? (
            <LoaderCircle className={styles.spinner} size={15} />
          ) : failed ? (
            <TriangleAlert size={15} />
          ) : (
            <Check size={15} />
          )}
        </span>
        <span className={`${styles.summaryTitle} ${row.active ? styles.runningText : ""}`}>
          <span aria-live="polite">{title}</span>
          {target && <code title={target}>{target}</code>}
        </span>
        {row.active && runTime && (
          <span className={styles.duration} aria-hidden="true">
            {runTime}
          </span>
        )}
        <ChevronRight className={styles.chevron} size={15} aria-hidden="true" />
      </button>

      {open && (
        <div className={styles.items}>
          {!row.items.length && <p className={styles.waiting}>{t("正在等待下一步事件")}</p>}
          {row.items.map((item) => {
            const itemTarget = targetOf(item);
            const duration = durationOf(item, now);
            const input = item.message?.tool_input ?? item.activity?.data.tool_input;
            const output = item.message?.text ?? item.activity?.data.tool_output;
            const itemStatus = t(
              {
                running: "运行中",
                completed: "已完成",
                failed: "失败",
                paused: "等待批准",
                stopped: "已停止",
                pending: "待开始",
                unknown: "状态待确认",
              }[item.status],
            );
            if (input === undefined && output === undefined) {
              return (
                <div className={styles.item} data-status={item.status} key={item.key}>
                  <div className={styles.itemLine}>
                    <span className={styles.itemIcon} aria-hidden="true">
                      {statusIcon(item.status)}
                    </span>
                    <strong>{t(nameOf(item))}</strong>
                    {itemTarget && <code title={itemTarget}>{itemTarget}</code>}
                    <span className={styles.itemMeta}>
                      {itemStatus}
                      {duration && ` · ${duration}`}
                    </span>
                  </div>
                </div>
              );
            }
            return (
              <details className={styles.item} data-status={item.status} key={item.key}>
                <summary>
                  <span className={styles.itemIcon} aria-hidden="true">
                    {statusIcon(item.status)}
                  </span>
                  <strong>{t(nameOf(item))}</strong>
                  {itemTarget && <code title={itemTarget}>{itemTarget}</code>}
                  <span className={styles.itemMeta}>
                    {itemStatus}
                    {duration && ` · ${duration}`}
                  </span>
                  <ChevronRight size={14} aria-hidden="true" />
                </summary>
                {(input !== undefined || output !== undefined) && (
                  <div className={styles.itemDetails}>
                    {input !== undefined && (
                      <>
                        <small>{t("调用参数")}</small>
                        <pre>
                          {typeof input === "string" ? input : JSON.stringify(input, null, 2)}
                        </pre>
                      </>
                    )}
                    {output !== undefined && (
                      <>
                        <small>{t("工具结果")}</small>
                        <pre>
                          {typeof output === "string" ? output : JSON.stringify(output, null, 2)}
                        </pre>
                      </>
                    )}
                  </div>
                )}
              </details>
            );
          })}
        </div>
      )}
    </section>
  );
}
