import { useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { Check, ChevronDown, ChevronRight, Clock3, Command, TriangleAlert } from "lucide-react";
import type { Activity, StreamEvent } from "../api/types";
import { readableJson } from "../lib/format";
import shared from "../styles/shared.module.css";
import styles from "./Trajectory.module.css";
import { useI18n, type Translate } from "../i18n";

function title(type: string, t: Translate): string {
  const known: Record<string, string> = {
    "run.started": "开始运行",
    "run.completed": "运行完成",
    "run.failed": "运行失败",
    "run.paused": "运行暂停",
    "assistant.delta": "回复增量",
    "tool.started": "调用工具",
    "tool.completed": "工具完成",
    "tool.failed": "工具失败",
    "model.started": "模型请求开始",
    "model.completed": "模型请求完成",
    "model.failed": "模型请求失败",
    "task.updated": "子 Agent 状态",
    "task.completed": "子 Agent 完成",
    "approval.requested": "等待批准",
  };
  return t(known[type] ?? type.replaceAll(".", " · "));
}

function fromEvent(event: StreamEvent): Activity {
  const data = event.data;
  return {
    id: `live-${event.seq}`,
    type: event.type,
    at: event.at ?? null,
    summary: typeof data.summary === "string" ? data.summary : null,
    tool_name: typeof data.tool_name === "string" ? data.tool_name : null,
    status: typeof data.status === "string" ? data.status : null,
    duration_ms: typeof data.duration_ms === "number" ? data.duration_ms : null,
    data,
    thread_id: event.thread_id,
  };
}

interface TrajectoryProps {
  activity: Activity[];
  liveEvents: StreamEvent[];
  agentName: string;
  agentColor: string;
}

export function Trajectory({ activity, liveEvents, agentName, agentColor }: TrajectoryProps) {
  const { t, language } = useI18n();
  const parent = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const rows = [
    ...activity,
    ...liveEvents.filter((event) => event.type !== "assistant.delta").map(fromEvent),
  ];
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parent.current,
    estimateSize: () => 76,
    getItemKey: (index) => rows[index]?.id ?? index,
    overscan: 8,
    useFlushSync: false,
  });
  const toggle = (id: string) =>
    setExpanded((old) => {
      const next = new Set(old);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  if (!rows.length)
    return (
      <div className={shared.empty}>
        <Command size={25} strokeWidth={1.4} />
        <strong>{t("暂无运行轨迹")}</strong>
        <p>{t("模型、工具和审批事件会在运行时显示在这里。")}</p>
      </div>
    );

  return (
    <div
      className={styles.scroller}
      ref={parent}
      role="feed"
      aria-label={t("{name} 的运行轨迹", { name: agentName })}
    >
      <div className={styles.canvas} style={{ height: virtualizer.getTotalSize() }}>
        {virtualizer.getVirtualItems().map((item) => {
          const row = rows[item.index];
          if (!row) return null;
          const isExpanded = expanded.has(row.id);
          const data =
            row.data && typeof row.data === "object" && !Array.isArray(row.data)
              ? (row.data as Record<string, unknown>)
              : null;
          const durationMs =
            row.duration_ms ?? (typeof data?.duration_ms === "number" ? data.duration_ms : null);
          const toolName = row.tool_name ?? (typeof data?.name === "string" ? data.name : null);
          const failed = row.type.includes("failed") || row.status === "error";
          const completed = row.type.includes("completed") || row.status === "completed";
          const icon = failed ? (
            <TriangleAlert size={15} />
          ) : completed ? (
            <Check size={15} />
          ) : (
            <Command size={15} />
          );
          return (
            <article
              key={row.id}
              ref={virtualizer.measureElement}
              data-index={item.index}
              className={styles.row}
              style={{ transform: `translateY(${item.start}px)` }}
              aria-label={`${agentName}：${title(row.type, t)}`}
            >
              <span className={styles.rail} aria-hidden="true">
                <span className={styles.railIcon} data-failed={failed}>
                  {icon}
                </span>
              </span>
              <div className={styles.card}>
                <button
                  className={styles.rowHead}
                  onClick={() => toggle(row.id)}
                  aria-expanded={isExpanded}
                >
                  <span className={styles.agentBadge} style={{ color: agentColor }}>
                    {agentName}
                  </span>
                  <span className={styles.rowTitle}>
                    {title(row.type, t)}
                    {toolName && <b>{toolName}</b>}
                  </span>
                  {durationMs != null && (
                    <span className={styles.duration}>{(durationMs / 1000).toFixed(1)}s</span>
                  )}
                  {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                </button>
                {row.summary && <div className={styles.summary}>{row.summary}</div>}
                {isExpanded && row.data != null && (
                  <pre className={styles.payload}>{readableJson(row.data, 6000, t)}</pre>
                )}
                <div className={styles.meta}>
                  <Clock3 size={11} aria-hidden="true" />
                  {row.at
                    ? new Date(row.at).toLocaleTimeString(language === "en" ? "en-US" : "zh-CN", {
                        hour12: false,
                      })
                    : t("实时")}
                  <span>{row.type}</span>
                </div>
              </div>
            </article>
          );
        })}
      </div>
    </div>
  );
}
