import type { TrustLevel } from "../api/types";

export const trustLevels: readonly { value: TrustLevel; label: string; detail: string }[] = [
  {
    value: "read_only",
    label: "只读",
    detail: "可读取与搜索；不提供文件写入或 Shell。待办和任务消息仍会更新。",
  },
  { value: "ask", label: "询问", detail: "文件改动、Shell 和外部工具调用需批准" },
  {
    value: "workspace_auto",
    label: "工作区内自动改动",
    detail:
      "文件可读取工作区外；工作区内的写入、编辑和删除自动执行。Shell 每次询问，外部工具也需批准；批准后可访问网络和工作区外路径。",
  },
  { value: "full", label: "完全信任", detail: "在本机直接执行操作" },
];
