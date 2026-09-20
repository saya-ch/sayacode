"""仅负责交互终端展示。

无头输出由命令行入口自行渲染。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from rich import box
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .help import GROUPS, TOPICS, find_topic, format_help

_TOOL_LABELS = {
    "read_file": ("读取文件", "Read file"),
    "write_file": ("写入文件", "Write file"),
    "search_replace": ("修改文件", "Edit file"),
    "execute_command_tool": ("运行命令", "Run command"),
    "git": ("查询 Git", "Query Git"),
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
    "todos": ("待办", "Todos"),
    "doctor": ("诊断", "Diagnostics"),
    "trust": ("信任", "Trust"),
    "mcp": ("MCP", "MCP"),
    "trace": ("追踪", "Trace"),
}

MODEL_PROTOCOL_LABELS = {
    "openai_chat_completions": "OpenAI Chat Completions",
    "openai_responses": "OpenAI Responses API",
    "anthropic_messages": "Anthropic Messages",
    "gemini_generate_content": "Gemini Native generateContent",
    "ollama_native_chat": "Ollama native chat",
}


class TerminalPresenter:
    """交互输出展示边界。"""

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
        self,
        *,
        version: str,
        workspace: Path,
        model: str | None,
        trust_level: str,
        session_id: str,
        protocol: str | None = None,
    ) -> None:
        details = Table.grid(padding=(0, 2), expand=False)
        details.add_column(style="dim", no_wrap=True)
        details.add_column(overflow="fold")
        if protocol:
            details.add_row(self._label("协议", "API"), protocol)
        details.add_row(
            self._label("模型", "MODEL"), model or self._label("未配置", "Not configured")
        )
        trust_color = {"read_only": "cyan", "ask": "yellow", "full": "red"}.get(
            trust_level, "white"
        )
        trust_name = {
            "read_only": self._label("只读", "Read only"),
            "ask": self._label("询问", "Ask"),
            "full": self._label("完全信任", "Full trust"),
        }.get(trust_level, trust_level)
        details.add_row(self._label("信任", "TRUST"), Text(trust_name, style=f"bold {trust_color}"))
        details.add_row(self._label("会话", "SESSION"), session_id)
        title = Text.assemble(("SAYACODE", "bold cyan"), (f"  {version}", "dim"))
        self.console.print(
            Panel(
                details,
                title=title,
                title_align="left",
                border_style="bright_black",
                padding=(0, 1),
                expand=True,
            )
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
                self._label(
                    "输入任务 · /help 查看命令 · /quit 退出",
                    "Enter a task · /help commands · /quit exit",
                ),
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
            spinner="dots",
            spinner_style="cyan",
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
                    Markdown(""),
                    console=self.console,
                    refresh_per_second=8,
                    transient=False,
                    vertical_overflow="visible",
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
                (f"  {marker}  ", color),
                (f"{title} · {name}", "white"),
                (f"  {state}{elapsed}", "dim"),
            )
        )

    def task_event(self, event: dict[str, Any]) -> None:
        self.stop_wait()
        self._finish_answer()
        status = str(event.get("status") or event.get("type", "").removeprefix("task."))
        task_id = str(event.get("task_id") or "?")
        color = (
            "green"
            if status == "completed"
            else "red"
            if status == "failed"
            else "yellow"
            if status == "paused"
            else "cyan"
        )
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
                    ),
                    style="dim",
                )
            )

    def agent_event(self, event: dict[str, Any]) -> None:
        """展示子任务触发的父轮次。"""
        kind = str(event.get("type") or "")
        task_id = str(event.get("task_id") or "?")
        thread_id = str(event.get("thread_id") or "?")
        if kind == "agent.wake.started":
            self.notice(
                self._label(
                    f"主 Agent 收到任务 {task_id} 的通知，正在继续执行…",
                    f"Main agent received task {task_id} and is continuing…",
                )
            )
        elif kind == "agent.wake.completed":
            self.notice(
                self._label(
                    f"主 Agent 已根据任务 {task_id} 继续",
                    f"Main agent continued from task {task_id}",
                ),
                level="success",
            )
            response = str(event.get("response") or "")
            if response:
                self.write_answer(response)
                self.end_turn()
        elif kind == "agent.wake.paused":
            self.notice(
                self._label(
                    f"主 Agent 等待批准；输入 /approve {thread_id} 查看操作",
                    f"Main agent needs approval; use /approve {thread_id}",
                ),
                level="warning",
            )
        elif kind == "agent.wake.failed":
            self.notice(
                self._label("主 Agent 自动继续失败", "Main agent continuation failed")
                + f"：{event.get('error') or ''}",
                level="error",
            )
        elif kind == "agent.wake.stopped":
            self.notice(
                self._label(
                    "主 Agent 已在检查点停止，可下次启动继续",
                    "Main agent stopped at a checkpoint and can continue later",
                ),
                level="warning",
            )
        elif kind == "agent.wake.uncertain":
            self.notice(
                self._label(
                    f"任务 {task_id} 的主 Agent 自动继续未确认；请检查会话历史与任务结果",
                    f"Main-agent continuation for task {task_id} is unconfirmed; inspect history and task status",
                ),
                level="warning",
            )

    def approval_intro(self, count: int) -> None:
        self.stop_wait()
        self._finish_answer()
        self.notice(
            self._label(
                f"{count} 个操作等待批准，请逐项核对。",
                f"{count} action{'s' if count != 1 else ''} require approval. Review each one.",
            ),
            level="warning",
        )

    def approval_action(self, index: int, count: int, action: dict[str, Any]) -> None:
        name = str(action.get("name") or "tool")
        args = action.get("args", {})
        visible = self.redact(args)
        body = Syntax(
            json.dumps(visible, ensure_ascii=False, indent=2, default=str),
            "json",
            theme="ansi_dark",
            word_wrap=True,
            background_color="default",
        )
        title = Text.assemble((f"{index + 1}/{count}  ", "bold yellow"), (name, "bold white"))
        self.console.print(
            Panel(
                body,
                title=title,
                title_align="left",
                border_style="yellow",
                padding=(0, 1),
                expand=True,
            )
        )

    def command_result(self, command: str, display: str) -> None:
        if not display:
            return
        self.stop_wait()
        self._finish_answer()
        name = command.split(maxsplit=1)[0].lstrip("/").lower()
        if name in {"help", "guide", "start"}:
            parts = command.split(maxsplit=1)
            self.help(parts[1] if len(parts) > 1 else "")
            return
        try:
            data = json.loads(display)
        except (TypeError, ValueError):
            self.console.print(display, markup=False, highlight=False, overflow="fold")
            return
        if isinstance(data, dict) and name in {"new", "reset"} and data.get("session_id"):
            self.notice(
                self._label(
                    f"已切换到新会话 {data['session_id']}",
                    f"New session active: {data['session_id']}",
                ),
                level="success",
            )
            return
        if (
            isinstance(data, dict)
            and name in {"models", "model", "config"}
            and isinstance(data.get("profiles"), dict)
        ):
            self._models_result(data)
            return
        if isinstance(data, list) and name == "history":
            self._history_result(data)
            return
        if isinstance(data, list) and name in {
            "team",
            "sessions",
            "session",
            "todos",
            "tools",
            "trace",
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
                    "json",
                    theme="ansi_dark",
                    word_wrap=True,
                    background_color="default",
                ),
                title=heading,
                title_align="left",
                border_style="bright_black",
                padding=(0, 1),
                expand=True,
            )
        )

    def _status_result(self, data: dict[str, Any]) -> None:
        labels = {
            "workspace": self._label("工作区", "Workspace"),
            "session_id": self._label("会话", "Session"),
            "trust_level": self._label("信任", "Trust"),
            "profile": self._label("配置", "Profile"),
            "model": self._label("模型", "Model"),
            "protocol": self._label("协议", "API protocol"),
            "base_url": self._label("接口地址", "Endpoint"),
            "context_length": self._label("上下文", "Context"),
            "max_output_tokens": self._label("最大输出", "Max output"),
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
                if all(
                    isinstance(item, int) for item in (input_tokens, output_tokens, total_tokens)
                ):
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
                table.add_row(self._label("运行", "Run"), str(thread["status"]))
            if thread.get("title"):
                table.add_row(self._label("标题", "Title"), str(thread["title"]))
        if data.get("mcp_error"):
            table.add_row("MCP", Text(str(data["mcp_error"]), style="red"))
        self.console.print(
            Panel(
                table,
                title=self._label("当前状态", "Current status"),
                title_align="left",
                border_style="bright_black",
                padding=(0, 1),
            )
        )

    def _models_result(self, data: dict[str, Any]) -> None:
        profiles = data["profiles"]
        if not profiles:
            self.notice(
                self._label(
                    "还没有模型配置，输入 /model add 开始添加。",
                    "No models configured. Use /model add to add one.",
                ),
                level="warning",
            )
            return
        table = Table(
            box=box.SIMPLE_HEAVY,
            show_edge=False,
            expand=True,
            title=self._label("模型列表", "Models"),
            title_style="bold cyan",
            header_style="bold dim",
        )
        for heading in (
            self._label("配置", "Profile"),
            self._label("接口协议", "API protocol"),
            self._label("模型", "Model"),
            self._label("上下文 / 输出", "Context / output"),
        ):
            table.add_column(heading, overflow="fold")
        default = data.get("default_profile")
        for name, profile in profiles.items():
            item = profile if isinstance(profile, dict) else {}
            context_length = item.get("context_length")
            max_output = item.get("max_output_tokens")
            table.add_row(
                ("● " if name == default else "  ") + str(name),
                MODEL_PROTOCOL_LABELS.get(
                    str(item.get("protocol")), str(item.get("protocol") or "—")
                ),
                str(item.get("model_id") or "—"),
                f"{context_length:,} / {max_output:,}"
                if isinstance(context_length, int) and isinstance(max_output, int)
                else "—",
            )
        self.console.print(table)
        self.console.print(
            Text(
                self._label(
                    "● 为默认配置 · /model add 添加 · /model key <名称> 更新密钥 · /model use <名称> 切换",
                    "● default · /model add to add · /model key <name> updates key · /model use <name> switches",
                ),
                style="dim",
            )
        )

    def _list_result(self, name: str, rows: list[Any]) -> None:
        if not rows:
            self.notice(self._label("暂无内容", "Nothing to show"))
            return
        if name == "team":
            fields = [
                ("task_id", "ID"),
                ("role", self._label("角色", "Role")),
                ("status", self._label("状态", "Status")),
            ]
        elif name in {"sessions", "session"}:
            fields = [
                ("thread_id", "ID"),
                ("title", self._label("标题", "Title")),
                ("trust_level", self._label("信任", "Trust")),
            ]
        elif name == "tools":
            fields = [
                ("name", self._label("工具", "Tool")),
                ("description", self._label("用途", "Description")),
            ]
        elif name == "trace":
            fields = [
                ("at", self._label("时间", "Time")),
                ("event", self._label("事件", "Event")),
                ("run_id", "Run ID"),
            ]
        else:
            fields = [
                ("content", self._label("任务", "Task")),
                ("status", self._label("状态", "Status")),
            ]
        table = Table(
            box=box.SIMPLE_HEAVY,
            show_edge=False,
            expand=True,
            title=_TITLES.get(name, (name, name))[0 if self.zh else 1],
            title_style="bold cyan",
            header_style="bold dim",
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
                    self._label(
                        "使用 /team status <ID> 查看详情", "Use /team status <ID> for details"
                    ),
                    style="dim",
                )
            )

    def _history_result(self, rows: list[Any]) -> None:
        if not rows:
            self.notice(self._label("暂无会话记录", "No conversation history"))
            return
        self.console.print(Text(self._label("会话记录", "Conversation history"), style="bold cyan"))
        for item in rows:
            if not isinstance(item, dict):
                self.console.print(str(item), markup=False)
                continue
            role = str(item.get("role") or "message")
            body = str(item.get("content") or "")
            self.console.print(
                Panel(
                    Markdown(body) if role in {"ai", "assistant"} else Text(body),
                    title=role.upper(),
                    title_align="left",
                    border_style="cyan" if role in {"ai", "assistant"} else "bright_black",
                    padding=(0, 1),
                )
            )

    def help(self, query: str = "") -> None:
        language = "zh" if self.zh else "en"
        if query.strip():
            asked = query.strip().lstrip("/").split(maxsplit=1)[0].lower()
            topic = find_topic(asked)
            if topic is None:
                self.notice(format_help(query, language=language), level="warning")
                return
            body = Text()
            body.append(topic.summary(language), style="white")
            aliases = ["/" + name for name in topic.names if name != asked]
            if aliases:
                body.append("\n\n" + self._label("别名：", "Aliases: "), style="dim")
                body.append("  ".join(aliases), style="cyan")
            body.append("\n\n" + self._label("用法：", "Usage: "), style="dim")
            body.append(topic.shown_usage(language), style="bold cyan")
            body.append("\n" + self._label("示例：", "Example: "), style="dim")
            body.append(topic.shown_example(language), style="green")
            if detail := topic.detail(language):
                body.append("\n\n" + detail, style="white")
            self.console.print(
                Panel(
                    body,
                    title=f"/{asked}",
                    title_align="left",
                    border_style="cyan",
                    padding=(0, 1),
                    expand=True,
                )
            )
            return

        self.console.print(
            Text(
                self._label(
                    "直接输入文字与 Agent 对话；用 /help <命令> 查看详细用法。",
                    "Type a task to talk to the agent; use /help <command> for details.",
                ),
                style="dim",
            )
        )
        if self.console.width < 58:
            for group, zh_name, en_name in GROUPS:
                names = "  ".join(
                    item
                    for topic in TOPICS
                    if topic.group == group
                    for item in (*("/" + name for name in topic.names), *topic.quick_actions)
                )
                self.console.print(Text(zh_name if self.zh else en_name, style="bold white"))
                self.console.print(Text(names, style="cyan"), overflow="fold")
            return
        table = Table(box=box.SIMPLE, show_edge=False, show_header=False, expand=True)
        table.add_column(style="bold white", no_wrap=True)
        table.add_column(style="cyan", overflow="fold")
        for group, zh_name, en_name in GROUPS:
            names = "  ".join(
                item
                for topic in TOPICS
                if topic.group == group
                for item in (*("/" + name for name in topic.names), *topic.quick_actions)
            )
            table.add_row(zh_name if self.zh else en_name, names)
        self.console.print(table)
