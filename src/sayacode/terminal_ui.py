"""Rich presentation for the interactive terminal only.

Headless text, JSON, and JSONL are intentionally rendered by the CLI itself.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

_TOOL_LABELS = {
    "read_file": ("读取文件", "Read file"),
    "write_file": ("写入文件", "Write file"),
    "search_replace": ("修改文件", "Edit file"),
    "batch_edit": ("批量修改", "Batch edit"),
    "execute_command_tool": ("运行命令", "Run command"),
    "git": ("Git 操作", "Git operation"),
    "glob_search": ("查找文件", "Find files"),
    "grep_search": ("搜索内容", "Search content"),
    "delegate_to_subagent": ("派发任务", "Delegate task"),
    "task_wait": ("等待任务", "Wait for task"),
}

_TITLES = {
    "status": ("当前状态", "Current status"),
    "stats": ("当前状态", "Current status"),
    "context": ("当前上下文", "Current context"),
    "session": ("会话", "Session"),
    "sessions": ("会话", "Sessions"),
    "team": ("后台任务", "Background tasks"),
    "plan": ("计划", "Plan"),
    "doctor": ("诊断", "Diagnostics"),
    "permissions": ("权限", "Permissions"),
    "mcp": ("MCP", "MCP"),
    "trace": ("追踪", "Trace"),
}


class TerminalPresenter:
    """One presentation boundary for human-readable interactive output."""

    def __init__(
        self, console: Console, *, language: str = "en", redact: Callable[[Any], Any]
    ) -> None:
        self.console = console
        self.zh = language == "zh"
        self.redact = redact
        self._answer_open = False
        self._answer_buffer = ""
        self._answer_live: Live | None = None
        self._status: Any = None

    def _label(self, zh: str, en: str) -> str:
        return zh if self.zh else en

    def header(
        self, *, version: str, workspace: Path, model: str | None,
        mode: str, session_id: str,
    ) -> None:
        details = Table.grid(padding=(0, 2), expand=False)
        details.add_column(style="dim", no_wrap=True)
        details.add_column(overflow="fold")
        details.add_row(self._label("模型", "MODEL"), model or self._label("未配置", "Not configured"))
        mode_color = {"build": "green", "plan": "magenta", "review": "cyan"}.get(mode, "white")
        details.add_row(self._label("模式", "MODE"), Text(mode, style=f"bold {mode_color}"))
        details.add_row(self._label("会话", "SESSION"), session_id)
        title = Text.assemble(("SAYACODE", "bold cyan"), (f"  {version}", "dim"))
        self.console.print(
            Panel(details, title=title, title_align="left", border_style="bright_black",
                  padding=(0, 1), expand=True)
        )
        self.console.print(
            Text.assemble(
                (self._label("工作区", "WORKSPACE"), "dim"),
                "  ",
                (str(workspace), "white"),
            ),
            overflow="fold",
        )
        self.console.print(
            Text(
                self._label("输入任务 · /help 查看命令 · /quit 退出", "Enter a task · /help commands · /quit exit"),
                style="dim",
            )
        )
        self.console.print()

    def notice(self, message: str, *, level: str = "info") -> None:
        marker, color = {
            "info": ("•", "cyan"),
            "success": ("✓", "green"),
            "warning": ("!", "yellow"),
            "error": ("×", "red"),
        }.get(level, ("•", "cyan"))
        self._finish_answer()
        self.console.print(Text.assemble((f"{marker}  ", color), (message, "white")))

    def start_wait(self, message: str | None = None) -> None:
        if self._status is not None or not self.console.is_terminal:
            return
        self._status = self.console.status(
            message or self._label("正在思考…", "Thinking…"),
            spinner="dots", spinner_style="cyan",
        )
        self._status.start()

    def stop_wait(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def write_answer(self, delta: str) -> None:
        if not delta:
            return
        self.stop_wait()
        if not self._answer_open:
            self.console.print(Text("SAYA", style="bold cyan"))
            self._answer_open = True
            if self.console.is_terminal:
                self._answer_live = Live(
                    Markdown(""), console=self.console, refresh_per_second=8,
                    transient=False, vertical_overflow="visible",
                )
                self._answer_live.start()
        if self._answer_live is not None:
            self._answer_buffer += delta
            self._answer_live.update(Markdown(self._answer_buffer))
        else:
            self.console.print(delta, end="", markup=False, highlight=False, soft_wrap=True)

    def _finish_answer(self) -> None:
        if self._answer_open:
            if self._answer_live is not None:
                self._answer_live.update(Markdown(self._answer_buffer), refresh=True)
                self._answer_live.stop()
                self._answer_live = None
            else:
                self.console.print()
            self._answer_buffer = ""
            self._answer_open = False

    def end_turn(self) -> None:
        self.stop_wait()
        self._finish_answer()
        self.console.print()

    def tool_event(self, name: str, status: str, *, duration: float | None = None) -> None:
        self.stop_wait()
        self._finish_answer()
        description = _TOOL_LABELS.get(name, ("调用工具", "Use tool"))
        title = description[0] if self.zh else description[1]
        marker, color = {
            "started": ("›", "cyan"),
            "completed": ("✓", "green"),
            "failed": ("×", "red"),
        }.get(status, ("•", "white"))
        state = {
            "started": self._label("运行中", "running"),
            "completed": self._label("完成", "done"),
            "failed": self._label("失败", "failed"),
        }.get(status, status)
        elapsed = f"  {duration:.1f}s" if duration is not None else ""
        self.console.print(
            Text.assemble(
                (f"  {marker}  ", color), (f"{title} · {name}", "white"),
                (f"  {state}{elapsed}", "dim"),
            )
        )

    def task_event(self, event: dict[str, Any]) -> None:
        self.stop_wait()
        self._finish_answer()
        status = str(event.get("status") or event.get("type", "").removeprefix("task."))
        task_id = str(event.get("task_id") or "?")
        color = "green" if status == "completed" else "red" if status == "failed" else "yellow" if status == "paused" else "cyan"
        state = {
            "running": self._label("运行中", "running"),
            "pending": self._label("待运行", "pending"),
            "completed": self._label("已完成", "completed"),
            "failed": self._label("失败", "failed"),
            "paused": self._label("等待批准", "needs approval"),
            "stopped": self._label("已停止", "stopped"),
            "interrupted": self._label("已中断", "interrupted"),
        }.get(status, status)
        self.console.print(
            Text.assemble(
                ("  ◇  ", color),
                (self._label("任务", "Task") + f" {task_id}", "white"),
                (f"  {state}", color),
            )
        )
        if status == "paused":
            self.console.print(
                Text(
                    self._label(
                        f"     使用 /team approve {task_id} 处理审批",
                        f"     Use /team approve {task_id} to review the request",
                    ), style="dim",
                )
            )

    def approval_intro(self, count: int) -> None:
        self.stop_wait()
        self._finish_answer()
        self.notice(
            self._label(
                f"{count} 个操作等待批准，请逐项核对。",
                f"{count} action{'s' if count != 1 else ''} require approval. Review each one.",
            ), level="warning",
        )

    def approval_action(self, index: int, count: int, action: dict[str, Any]) -> None:
        name = str(action.get("name") or "tool")
        args = action.get("args", {})
        visible = self.redact(args)
        body = Syntax(
            json.dumps(visible, ensure_ascii=False, indent=2, default=str),
            "json", theme="ansi_dark", word_wrap=True, background_color="default",
        )
        title = Text.assemble(
            (f"{index + 1}/{count}  ", "bold yellow"), (name, "bold white")
        )
        self.console.print(
            Panel(body, title=title, title_align="left", border_style="yellow",
                  padding=(0, 1), expand=True)
        )

    def command_result(self, command: str, display: str) -> None:
        if not display:
            return
        self.stop_wait()
        self._finish_answer()
        name = command.split(maxsplit=1)[0].lstrip("/").lower()
        if name in {"help", "guide", "start"}:
            self.help()
            return
        try:
            data = json.loads(display)
        except (TypeError, ValueError):
            self.console.print(display, markup=False, highlight=False, overflow="fold")
            return
        if isinstance(data, list) and name == "history":
            self._history_result(data)
            return
        if isinstance(data, list) and name in {
            "team", "sessions", "session", "plan", "tools", "trace"
        }:
            self._list_result(name, data)
            return
        if isinstance(data, dict) and name in {"status", "stats", "context"}:
            self._status_result(data)
            return
        title = _TITLES.get(name, (name or "结果", name or "Result"))
        heading = title[0] if self.zh else title[1]
        self.console.print(
            Panel(
                Syntax(
                    json.dumps(self.redact(data), ensure_ascii=False, indent=2, default=str),
                    "json", theme="ansi_dark", word_wrap=True, background_color="default",
                ),
                title=heading, title_align="left", border_style="bright_black",
                padding=(0, 1), expand=True,
            )
        )

    def _status_result(self, data: dict[str, Any]) -> None:
        labels = {
            "workspace": self._label("工作区", "Workspace"),
            "session_id": self._label("会话", "Session"),
            "mode": self._label("模式", "Mode"),
            "profile": self._label("配置", "Profile"),
            "model": self._label("模型", "Model"),
            "message_count": self._label("消息", "Messages"),
            "active_tasks": self._label("后台任务", "Background tasks"),
            "mcp_tools": self._label("MCP 工具", "MCP tools"),
            "usage": self._label("用量", "Usage"),
        }
        table = Table(box=box.SIMPLE, show_header=False, show_edge=False, expand=True)
        table.add_column(style="dim", no_wrap=True)
        table.add_column(overflow="fold")
        for key in labels:
            if key not in data:
                continue
            value = data[key]
            if key in {"active_tasks", "mcp_tools"} and isinstance(value, list):
                shown = str(len(value))
            elif key == "usage" and isinstance(value, dict):
                input_tokens = value.get("input_tokens")
                output_tokens = value.get("output_tokens")
                total_tokens = value.get("total_tokens")
                if all(isinstance(item, int) for item in (input_tokens, output_tokens, total_tokens)):
                    shown = (
                        f"{self._label('输入', 'in')} {input_tokens:,}  ·  "
                        f"{self._label('输出', 'out')} {output_tokens:,}  ·  "
                        f"{self._label('合计', 'total')} {total_tokens:,}"
                    )
                else:
                    shown = str(value)
            elif value is None:
                shown = "—"
            else:
                shown = str(value)
            table.add_row(labels[key], shown)
        thread = data.get("thread")
        if isinstance(thread, dict):
            if thread.get("status"):
                table.add_row(
                    self._label("运行", "Run"), str(thread["status"])
                )
            if thread.get("title"):
                table.add_row(
                    self._label("标题", "Title"), str(thread["title"])
                )
        if data.get("mcp_error"):
            table.add_row("MCP", Text(str(data["mcp_error"]), style="red"))
        self.console.print(
            Panel(table, title=self._label("当前状态", "Current status"),
                  title_align="left", border_style="bright_black", padding=(0, 1))
        )

    def _list_result(self, name: str, rows: list[Any]) -> None:
        if not rows:
            self.notice(self._label("暂无内容", "Nothing to show"))
            return
        if name == "team":
            fields = [("task_id", "ID"), ("role", self._label("角色", "Role")),
                      ("status", self._label("状态", "Status"))]
        elif name in {"sessions", "session"}:
            fields = [("thread_id", "ID"), ("title", self._label("标题", "Title")),
                      ("mode", self._label("模式", "Mode"))]
        elif name == "tools":
            fields = [("name", self._label("工具", "Tool")),
                      ("description", self._label("用途", "Description"))]
        elif name == "trace":
            fields = [("at", self._label("时间", "Time")),
                      ("event", self._label("事件", "Event")), ("run_id", "Run ID")]
        else:
            fields = [("content", self._label("任务", "Task")),
                      ("status", self._label("状态", "Status"))]
        table = Table(
            box=box.SIMPLE_HEAVY, show_edge=False, expand=True,
            title=_TITLES.get(name, (name, name))[0 if self.zh else 1],
            title_style="bold cyan", header_style="bold dim",
        )
        for _, label in fields:
            table.add_column(label, overflow="fold")
        for item in rows:
            if not isinstance(item, dict):
                table.add_row(str(item), *("" for _ in fields[1:]))
                continue
            table.add_row(*(str(item.get(key) or "") for key, _ in fields))
        self.console.print(table)
        if name == "tools":
            self.console.print(
                Text(
                    self._label("使用 /tools <名称> 查看参数", "Use /tools <name> for parameters"),
                    style="dim",
                )
            )
        elif name == "team":
            self.console.print(
                Text(
                    self._label("使用 /team status <ID> 查看详情", "Use /team status <ID> for details"),
                    style="dim",
                )
            )

    def _history_result(self, rows: list[Any]) -> None:
        if not rows:
            self.notice(self._label("暂无会话记录", "No conversation history"))
            return
        self.console.print(
            Text(self._label("会话记录", "Conversation history"), style="bold cyan")
        )
        for item in rows:
            if not isinstance(item, dict):
                self.console.print(str(item), markup=False)
                continue
            role = str(item.get("role") or "message")
            body = str(item.get("content") or "")
            self.console.print(
                Panel(
                    Markdown(body) if role in {"ai", "assistant"} else Text(body),
                    title=role.upper(), title_align="left",
                    border_style="cyan" if role in {"ai", "assistant"} else "bright_black",
                    padding=(0, 1),
                )
            )

    def help(self) -> None:
        groups = (
            ("/status  /doctor", self._label("状态与诊断", "Status and diagnostics")),
            ("/session  /history", self._label("会话与历史", "Sessions and history")),
            ("/mode  /plan", self._label("模式与计划", "Modes and plans")),
            ("/team  /mcp", self._label("后台任务与扩展", "Tasks and extensions")),
            ("/git  /symbols  /analyze", self._label("仓库与代码", "Repository and code")),
            ("/permissions  /tools", self._label("权限与工具", "Permissions and tools")),
            ("/compact  /rewind", self._label("摘要与回退", "Compact and rewind")),
            ("/memory  /commands", self._label("项目约定与自定义命令", "Memory and custom commands")),
            ("/lang  /style  /settings", self._label("界面设置", "Display settings")),
            ("/quit", self._label("退出", "Exit")),
        )
        if self.console.width < 58:
            lines = [Text.assemble((commands, "bold cyan"), (f"\n  {meaning}", "dim"))
                     for commands, meaning in groups]
            self.console.print(Group(*lines))
            return
        table = Table(box=None, show_header=False, padding=(0, 2), expand=False)
        table.add_column(style="bold cyan", no_wrap=True)
        table.add_column(style="dim")
        for commands, meaning in groups:
            table.add_row(commands, meaning)
        self.console.print(
            Panel(table, title=self._label("命令速览", "Commands"), title_align="left",
                  border_style="bright_black", padding=(0, 1))
        )
