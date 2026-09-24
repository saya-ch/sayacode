import { useMemo, useRef, useState } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  Check,
  ChevronDown,
  ChevronRight,
  CirclePause,
  Clock3,
  Command,
  LoaderCircle,
  TriangleAlert,
} from "lucide-react";
import type { Activity, StreamEvent } from "../api/types";
import { readableJson } from "../lib/format";
import {
  activityTarget,
  projectActivity,
  type ActivityGroup,
  type ActivityKind,
  type ActivityStep,
} from "../state/activity";
import { useI18n, type Translate } from "../i18n";
import shared from "../styles/shared.module.css";
import styles from "./Trajectory.module.css";

type Focus = "all" | ActivityKind;
type VisibleRow =
  | { kind: "heading"; id: string; group: ActivityGroup }
  | { kind: "step"; id: string; step: ActivityStep };

const filters: { key: Focus; label: string }[] = [
  { key: "all", label: "全部" },
  { key: "tool", label: "工具" },
  { key: "model", label: "模型" },
  { key: "approval", label: "审批" },
  { key: "task", label: "子 Agent" },
];

function label(step: ActivityStep, t: Translate): string {
  if (step.kind === "tool") return step.name ?? t("工具调用");
  if (step.kind === "model") return t("模型请求");
  const names: Record<string, string> = {
    "approval.requested": "等待批准",
    "review.decision": "自动审理",
    "task.updated": "子 Agent 状态",
    "task.completed": "子 Agent 完成",
    hook: "Hook",
  };
  return t(names[step.type] ?? step.type.replaceAll(".", " · "));
}

function statusLabel(status: ActivityStep["status"], t: Translate): string {
  const names = {
    pending: "待开始",
    running: "运行中",
    completed: "已完成",
    failed: "失败",
    paused: "等待批准",
    stopped: "已停止",
    unknown: "状态待确认",
  };
  return t(names[status]);
}

function icon(step: ActivityStep) {
  if (step.status === "failed") return <TriangleAlert size={15} />;
  if (step.status === "completed") return <Check size={15} />;
  if (step.status === "paused") return <CirclePause size={15} />;
  if (step.status === "running") return <LoaderCircle size={15} />;
  return <Command size={15} />;
}

function time(value: string | null, language: string): string {
  if (!value) return "";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? ""
    : parsed.toLocaleTimeString(language === "en" ? "en-US" : "zh-CN", { hour12: false });
}

function duration(value: number | null): string | null {
  if (value == null) return null;
  return value < 1000 ? `${Math.round(value)}ms` : `${(value / 1000).toFixed(1)}s`;
}

function details(step: ActivityStep, t: Translate): { title: string; value: unknown }[] {
  const { tool_input, tool_output, error, detail_unavailable, ...other } = step.data;
  const sections: { title: string; value: unknown }[] = [];
  if (tool_input !== undefined) sections.push({ title: t("调用参数"), value: tool_input });
  if (tool_output !== undefined) sections.push({ title: t("工具结果"), value: tool_output });
  if (error !== undefined) sections.push({ title: t("错误"), value: error });
  if (detail_unavailable === true)
    sections.push({ title: t("详细内容不可用"), value: t("工具原文已不在当前检查点中") });
  if (Object.keys(other).length) sections.push({ title: t("事件数据"), value: other });
  return sections;
}

interface TrajectoryProps {
  activity: Activity[];
  liveEvents: StreamEvent[];
  agentName: string;
  agentColor: string;
}

export function Trajectory({ activity, liveEvents, agentName, agentColor }: TrajectoryProps) {
  const { t, language } = useI18n();
  const scroller = useRef<HTMLDivElement>(null);
  const [focus, setFocus] = useState<Focus>("all");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const groups = useMemo(() => projectActivity(activity, liveEvents), [activity, liveEvents]);
  const counts = useMemo(() => {
    const result: Record<Focus, number> = {
      all: 0,
      tool: 0,
      model: 0,
      approval: 0,
      task: 0,
      other: 0,
    };
    for (const group of groups)
      for (const step of group.steps) {
        result.all += 1;
        result[step.kind] += 1;
      }
    return result;
  }, [groups]);
  const rows = useMemo<VisibleRow[]>(
    () =>
      groups.flatMap((group) => {
        const steps =
          focus === "all" ? group.steps : group.steps.filter((step) => step.kind === focus);
        if (!steps.length && focus !== "all") return [];
        return [
          { kind: "heading" as const, id: group.id, group },
          ...steps.map((step) => ({ kind: "step" as const, id: step.id, step })),
        ];
      }),
    [focus, groups],
  );
  const virtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => scroller.current,
    estimateSize: (index) => (rows[index]?.kind === "heading" ? 43 : 50),
    getItemKey: (index) => rows[index]?.id ?? index,
    overscan: 10,
    useFlushSync: false,
  });
  const toggle = (id: string) =>
    setExpanded((old) => {
      const next = new Set(old);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  if (!groups.length)
    return (
      <div className={shared.empty}>
        <Command size={25} strokeWidth={1.4} />
        <strong>{t("暂无运行轨迹")}</strong>
        <p>{t("模型、工具和审批事件会在运行时显示在这里。")}</p>
      </div>
    );

  return (
    <section className={styles.layout} aria-label={t("{name} 的运行轨迹", { name: agentName })}>
      <div className={styles.filters} role="group" aria-label={t("筛选运行事件")}>
        {filters.map((filter) => (
          <button
            key={filter.key}
            type="button"
            aria-pressed={focus === filter.key}
            className={focus === filter.key ? styles.activeFilter : ""}
            onClick={() => {
              setFocus(filter.key);
              scroller.current?.scrollTo({ top: 0 });
            }}
          >
            {t(filter.label)} <span>{counts[filter.key]}</span>
          </button>
        ))}
      </div>
      <div className={styles.scroller} ref={scroller} role="region" aria-label={t("事件列表")}>
        {rows.length === 0 ? (
          <div className={styles.noMatch}>{t("这一类暂无事件")}</div>
        ) : (
          <div className={styles.canvas} style={{ height: virtualizer.getTotalSize() }}>
            {virtualizer.getVirtualItems().map((item) => {
              const row = rows[item.index];
              if (!row) return null;
              if (row.kind === "heading")
                return (
                  <div
                    key={row.id}
                    ref={virtualizer.measureElement}
                    data-index={item.index}
                    className={styles.runHeading}
                    style={{ transform: `translateY(${item.start}px)` }}
                  >
                    <strong>{t("第 {number} 轮", { number: row.group.number })}</strong>
                    <span>{time(row.group.at, language)}</span>
                    <span className={styles.runStatus} data-status={row.group.status}>
                      {statusLabel(row.group.status, t)}
                    </span>
                  </div>
                );
              const { step } = row;
              const open = expanded.has(step.id);
              const target = activityTarget(step);
              const sections = details(step, t);
              const error = typeof step.data.error === "string" ? step.data.error : null;
              return (
                <article
                  key={row.id}
                  ref={virtualizer.measureElement}
                  data-index={item.index}
                  className={styles.row}
                  data-status={step.status}
                  style={{ transform: `translateY(${item.start}px)` }}
                >
                  <button
                    type="button"
                    className={styles.rowHead}
                    onClick={() => toggle(step.id)}
                    aria-expanded={open}
                  >
                    <span className={styles.stepIcon} aria-hidden="true">
                      {icon(step)}
                    </span>
                    <span className={styles.agentBadge} style={{ color: agentColor }}>
                      {agentName}
                    </span>
                    <strong className={styles.rowTitle}>{label(step, t)}</strong>
                    {target && (
                      <span className={styles.target} title={target}>
                        {target}
                      </span>
                    )}
                    <span className={styles.stepStatus}>{statusLabel(step.status, t)}</span>
                    <span className={styles.duration}>
                      {duration(step.durationMs) ?? time(step.at, language)}
                    </span>
                    {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  </button>
                  {open && (
                    <div className={styles.details}>
                      {error && <p className={styles.error}>{error}</p>}
                      <div className={styles.meta}>
                        <Clock3 size={12} aria-hidden="true" />
                        {time(step.at, language)}
                        {step.endedAt && <span>→ {time(step.endedAt, language)}</span>}
                        <span>{step.type}</span>
                      </div>
                      {sections.map((section) => (
                        <div key={section.title} className={styles.detailSection}>
                          <strong>{section.title}</strong>
                          <pre>{readableJson(section.value, 6000, t)}</pre>
                        </div>
                      ))}
                    </div>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
}
