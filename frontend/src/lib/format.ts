import type { Language, Translate } from "../i18n";

export function shortId(value: string, length = 8): string {
  return value.length > length ? `${value.slice(0, length)}…` : value;
}

export function displaySessionTitle(value: string | null | undefined, t: Translate): string {
  if (value === "New session" || value === "新会话") return t("新会话");
  return value || t("未命名会话");
}

export function relativeTime(
  value: string | null | undefined,
  t: Translate = (key) => key,
  language: Language = "zh",
): string {
  if (!value) return "";
  const difference = Date.now() - new Date(value).getTime();
  if (!Number.isFinite(difference)) return "";
  if (difference < 60_000) return t("刚刚");
  if (difference < 3_600_000)
    return t("{count} 分钟前", { count: Math.floor(difference / 60_000) });
  if (difference < 86_400_000)
    return t("{count} 小时前", { count: Math.floor(difference / 3_600_000) });
  return new Date(value).toLocaleDateString(language === "en" ? "en-US" : "zh-CN", {
    month: "numeric",
    day: "numeric",
  });
}

export function elapsed(value: string | null | undefined, now = Date.now()): string {
  if (!value) return "";
  const seconds = Math.max(0, Math.floor((now - new Date(value).getTime()) / 1000));
  if (!Number.isFinite(seconds)) return "";
  return seconds >= 60
    ? `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`
    : `${seconds}s`;
}

export function statusLabel(status: string, t: Translate = (key) => key): string {
  const labels: Record<string, string> = {
    idle: "空闲",
    pending: "待开始",
    running: "运行中",
    stopping: "正在停止",
    paused: "等待批准",
    completed: "已完成",
    rewound: "已回退",
    stopped: "已停止",
    interrupted: "已中断",
    error: "失败",
    failed: "失败",
  };
  return t(labels[status] ?? status);
}

export function roleLabel(role: string, t: Translate = (key) => key): string {
  const labels: Record<string, string> = {
    builder: "构建",
    planner: "规划",
    reviewer: "审查",
    main: "SAYA",
  };
  return t(labels[role] ?? role);
}

export function agentColor(threadId: string): string {
  const colors = [
    "var(--agent-amber)",
    "var(--agent-blue)",
    "var(--agent-violet)",
    "var(--agent-rose)",
  ];
  let hash = 2166136261;
  for (let index = 0; index < threadId.length; index += 1) {
    hash = Math.imul(hash ^ threadId.charCodeAt(index), 16777619) >>> 0;
  }
  return colors[hash % colors.length] ?? "var(--agent-amber)";
}

export function readableJson(value: unknown, max = 6000, t: Translate = (key) => key): string {
  let text: string;
  try {
    text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  } catch {
    text = String(value);
  }
  return text.length > max ? `${text.slice(0, max)}\n… ${t("内容过长，已截断显示")}` : text;
}
