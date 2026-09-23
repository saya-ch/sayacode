"""交互终端的颜色、状态和可见名称。

所有展示模块从这里读取同一套视觉语义，避免各自维护颜色和符号。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


class Palette:
    """Rich 与 prompt_toolkit 共用的低饱和终端色板。"""

    brand = "bright_cyan"
    accent = "cyan"
    text = "white"
    muted = "bright_black"
    success = "green"
    warning = "yellow"
    danger = "red"
    review = "magenta"
    panel = "bright_black"
    toolbar_bg = "#1f2430"
    toolbar_fg = "#c8d0dc"
    prompt = "#7dd3fc"
    agent_colors = (
        "bright_cyan",
        "bright_magenta",
        "bright_blue",
        "bright_yellow",
        "bright_green",
        "bright_white",
    )


@dataclass(frozen=True, slots=True)
class StateStyle:
    """一个状态在双语终端中的文字、标记和颜色。"""

    marker: str
    color: str
    zh: str
    en: str

    def label(self, chinese: bool) -> str:
        """按当前界面语言返回状态名称。"""
        return self.zh if chinese else self.en


@dataclass(frozen=True, slots=True)
class AgentStyle:
    """一个 Agent 线程在终端中的稳定身份样式。"""

    color: str
    zh: str
    en: str

    def label(self, chinese: bool) -> str:
        """按界面语言返回线程身份。"""
        return self.zh if chinese else self.en


def agent_style(
    thread_id: str | None, role: str | None = None, title: str | None = None
) -> AgentStyle:
    """按线程 ID 稳定分配颜色，主 Agent 始终使用品牌色。"""
    if not thread_id or role in {None, "main"}:
        return AgentStyle(Palette.brand, "SAYA", "SAYA")
    digest = hashlib.sha256(thread_id.encode("utf-8")).digest()
    color = Palette.agent_colors[digest[0] % len(Palette.agent_colors)]
    short_id = thread_id.removeprefix("task-")[:8]
    name = role or "agent"
    subject = " · ".join(item for item in (name, title, short_id) if item)
    return AgentStyle(color, subject, subject)


NOTICE_STYLES = {
    "info": StateStyle("·", Palette.accent, "提示", "info"),
    "success": StateStyle("✓", Palette.success, "完成", "done"),
    "warning": StateStyle("!", Palette.warning, "注意", "attention"),
    "error": StateStyle("×", Palette.danger, "错误", "error"),
}

TOOL_STYLES = {
    "started": StateStyle("›", Palette.accent, "运行中", "running"),
    "completed": StateStyle("✓", Palette.success, "完成", "done"),
    "failed": StateStyle("×", Palette.danger, "失败", "failed"),
}

TASK_STYLES = {
    "running": StateStyle("◇", Palette.accent, "运行中", "running"),
    "pending": StateStyle("◇", Palette.muted, "待运行", "pending"),
    "idle": StateStyle("◆", Palette.success, "空闲，可继续", "idle, continuable"),
    "failed": StateStyle("◆", Palette.danger, "失败", "failed"),
    "paused": StateStyle("◆", Palette.warning, "等待批准", "needs approval"),
    "stopped": StateStyle("◆", Palette.muted, "已停止", "stopped"),
    "interrupted": StateStyle("◆", Palette.warning, "已中断", "interrupted"),
}

REVIEW_STYLES = {
    "allow": StateStyle("✓", Palette.success, "自动批准", "auto-approved"),
    "deny": StateStyle("×", Palette.danger, "自动拒绝", "auto-rejected"),
    "ask": StateStyle("?", Palette.warning, "转人工确认", "human review"),
}

TRUST_STYLES = {
    "read_only": StateStyle("R", Palette.accent, "只读", "Read only"),
    "ask": StateStyle("A", Palette.warning, "询问", "Ask"),
    "jev": StateStyle("J", Palette.review, "Jev 自动审理", "Jev review"),
    "full": StateStyle("F", Palette.danger, "完全信任", "Full trust"),
}

TOOL_LABELS = {
    "read_file": ("读取文件", "Read file"),
    "write_file": ("写入文件", "Write file"),
    "search_replace": ("修改文件", "Edit file"),
    "delete_file": ("删除文件", "Delete file"),
    "list_directory": ("列出目录", "List directory"),
    "read_output_file": ("读取完整输出", "Read full output"),
    "execute_command_tool": ("运行命令", "Run command"),
    "git": ("查询 Git", "Query Git"),
    "glob_search": ("查找文件", "Find files"),
    "grep_search": ("搜索内容", "Search content"),
    "analyze_project": ("分析项目", "Analyze project"),
    "list_symbols": ("查询符号", "Query symbols"),
    "web_search": ("搜索网页", "Search web"),
    "write_todos": ("更新计划", "Update plan"),
    "delegate_to_subagent": ("派发任务", "Delegate task"),
    "send_message_to_subagent": ("追问任务", "Message task"),
    "task_status": ("查询任务", "Check task"),
    "task_delivery": ("检查交付", "Inspect delivery"),
    "task_wait": ("等待任务", "Wait for task"),
    "report_to_parent": ("报告父任务", "Report to parent"),
}

COMMAND_TITLES = {
    "status": ("当前状态", "Current status"),
    "stats": ("当前状态", "Current status"),
    "context": ("当前上下文", "Current context"),
    "session": ("会话", "Session"),
    "sessions": ("会话", "Sessions"),
    "team": ("后台任务", "Background tasks"),
    "todos": ("待办", "Todos"),
    "doctor": ("诊断", "Diagnostics"),
    "trust": ("信任", "Trust"),
    "mcp": ("MCP", "MCP"),
    "trace": ("追踪", "Trace"),
    "skills": ("Skill", "Skills"),
    "skill": ("Skill", "Skill"),
}

MODEL_PROTOCOL_LABELS = {
    "openai_chat_completions": "OpenAI Chat Completions",
    "openai_responses": "OpenAI Responses API",
    "anthropic_messages": "Anthropic Messages",
    "gemini_generate_content": "Gemini Native generateContent",
    "ollama_native_chat": "Ollama native chat",
}


def trust_style(level: str) -> StateStyle:
    """返回信任档位样式，未知值仍用可读的中性样式。"""
    return TRUST_STYLES.get(level, StateStyle("?", Palette.text, level, level))


def tool_detail(name: str, arguments: Any, result: Any = None) -> str:
    """从工具输入挑出一段安全短摘要，供完成行定位实际动作。"""
    if name == "delegate_to_subagent" and isinstance(result, str):
        try:
            payload = json.loads(result)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            task_id = payload.get("task_id")
            role = payload.get("role")
            title = payload.get("title")
            status = payload.get("status")
            values = [str(value) for value in (title, role, task_id, status) if value]
            if values:
                return " · ".join(values)
    if not isinstance(arguments, dict):
        return ""
    todos = arguments.get("todos")
    if name == "write_todos" and isinstance(todos, list):
        return f"{len(todos)} 项" if todos else "清空"
    keys = {
        "read_file": ("path",),
        "write_file": ("path",),
        "search_replace": ("path",),
        "delete_file": ("path",),
        "list_directory": ("path",),
        "read_output_file": ("path",),
        "execute_command_tool": ("command",),
        "git": ("action", "ref"),
        "glob_search": ("pattern",),
        "grep_search": ("query", "pattern"),
        "web_search": ("query",),
        "analyze_project": ("root_dir",),
        "list_symbols": ("root_dir", "query"),
        "delegate_to_subagent": ("role", "task"),
        "send_message_to_subagent": ("task_id", "message"),
        "task_status": ("task_id",),
        "task_delivery": ("task_id",),
        "task_wait": ("task_id",),
    }.get(name, ())
    parts: list[str] = []
    for key in keys:
        value = arguments.get(key)
        if value is None or value == "":
            continue
        text = " ".join(str(value).split())
        parts.append(text[:120] + ("…" if len(text) > 120 else ""))
        if len(parts) == 2:
            break
    return " · ".join(parts)


__all__ = [
    "COMMAND_TITLES",
    "AgentStyle",
    "MODEL_PROTOCOL_LABELS",
    "NOTICE_STYLES",
    "Palette",
    "REVIEW_STYLES",
    "StateStyle",
    "TASK_STYLES",
    "TOOL_LABELS",
    "TOOL_STYLES",
    "TRUST_STYLES",
    "trust_style",
    "tool_detail",
    "agent_style",
]
