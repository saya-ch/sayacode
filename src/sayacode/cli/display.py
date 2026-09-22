"""仅负责交互终端展示。

无头输出由命令行入口自行渲染。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .result_views import CommandResultRenderer
from .theme import (
    NOTICE_STYLES,
    REVIEW_STYLES,
    TASK_STYLES,
    TOOL_LABELS,
    TOOL_STYLES,
    AgentStyle,
    Palette,
    StateStyle,
    agent_style,
    tool_detail,
    trust_style,
)


class TerminalPresenter:
    """交互输出展示边界，只管交互终端的排版与脱敏展示。
    参数是终端对象与语言，展示前会先收尾未完成的回答。
    约束是无头输出不走这里，密钥类字段展示前先脱敏。"""

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
        self._tasks: dict[str, dict[str, Any]] = {}
        self._agent_thread_id: str | None = None
        self._agent_role = "main"
        self._todos: list[dict[str, Any]] = []
        self._results = CommandResultRenderer(
            console,
            is_chinese=lambda: self.zh,
            redact=redact,
            notice=self.notice,
        )

    def _label(self, zh: str, en: str) -> str:
        return zh if self.zh else en

    def set_agent(self, thread_id: str | None, role: str | None = None) -> None:
        """设置当前展示线程，后续正文和工具行沿用同一身份颜色。"""
        self._agent_thread_id = thread_id
        self._agent_role = role or "main"

    def update_todos(self, todos: Any) -> None:
        """缓存主线程 Todo，用于底部状态栏的进度显示。"""
        if isinstance(todos, list) and all(isinstance(item, dict) for item in todos):
            self._todos = [dict(item) for item in todos]

    def todo_progress(self) -> tuple[int, int]:
        """返回已完成和总 Todo 数。"""
        total = len(self._todos)
        completed = sum(item.get("status") == "completed" for item in self._todos)
        return completed, total

    def _identity(
        self,
        thread_id: str | None = None,
        role: str | None = None,
        title: str | None = None,
    ) -> AgentStyle:
        """取得稳定的线程身份样式。"""
        return agent_style(
            thread_id if thread_id is not None else self._agent_thread_id,
            role if role is not None else self._agent_role,
            title,
        )

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
        """打印启动头图，展示版本模型信任与会话。
        参数是版本、工作区、模型、信任档与会话号，另可带接口协议。
        只在交互启动时调用一次，清屏后重绘不影响会话状态。"""
        details = Table.grid(padding=(0, 2), expand=True)
        details.add_column(style=Palette.muted, no_wrap=True)
        details.add_column(overflow="fold", ratio=1)
        details.add_row(
            self._label("模型", "MODEL"), model or self._label("未配置", "Not configured")
        )
        if protocol:
            details.add_row(self._label("协议", "API"), protocol)
        trust = trust_style(trust_level)
        details.add_row(
            self._label("信任", "TRUST"),
            Text(trust.label(self.zh), style=f"bold {trust.color}"),
        )
        details.add_row(self._label("会话", "SESSION"), session_id)
        shown_workspace = self._compact_path(workspace)
        details.add_row(self._label("工作区", "WORKSPACE"), shown_workspace)
        title = Text.assemble(("SAYACODE", f"bold {Palette.brand}"), (f"  {version}", Palette.muted))
        self.console.print(
            Panel(
                details,
                title=title,
                subtitle="LANGCHAIN  ·  LANGGRAPH",
                title_align="left",
                subtitle_align="right",
                border_style=Palette.panel,
                padding=(0, 1),
                expand=True,
            )
        )
        if self.console.width < 58:
            hint = self._label("输入任务  ·  /help 命令  ·  /quit 退出", "Type a task  ·  /help  ·  /quit")
        else:
            hint = self._label(
                "直接输入任务  ·  /help 命令  ·  /new 新会话  ·  /quit 退出",
                "Type a task  ·  /help commands  ·  /new session  ·  /quit exit",
            )
        self.console.print(Text(hint, style=Palette.muted))
        self.console.print()

    def wizard(
        self,
        title: str,
        description: str,
        options: list[str] | tuple[str, ...] = (),
    ) -> None:
        """展示设置向导的标题、说明和编号选项。"""
        self.stop_wait()
        self._finish_answer()
        body = Table.grid(padding=(0, 1), expand=True)
        body.add_column(no_wrap=True, style=Palette.accent)
        body.add_column(overflow="fold")
        body.add_row("", Text(description, style=Palette.muted))
        for index, option in enumerate(options, 1):
            body.add_row(f"{index}.", option)
        self.console.print(
            Panel(
                body,
                title=title,
                title_align="left",
                border_style=Palette.accent,
                padding=(0, 1),
            )
        )

    def _compact_path(self, path: Path) -> str:
        """按终端宽度保留工作区路径的末尾，避免面板内部折断路径。"""
        value = str(path)
        if self.console.width < 58:
            return path.name
        available = max(24, self.console.width - 14)
        if len(value) <= available:
            return value
        tail_parts = Path(value).parts[-2:]
        tail = "\\".join(tail_parts)
        compact = "…\\" + tail
        if len(compact) <= available:
            return compact
        return "…" + value[-(available - 1) :]

    def task_count(self) -> int:
        """返回当前仍需关注的后台任务数，供输入状态栏显示。"""
        return sum(
            item.get("status") in {"pending", "running", "stopping", "paused"}
            for item in self._tasks.values()
        )

    def notice(self, message: str, *, level: str = "info") -> None:
        """打印一行带颜色标记的提示，提示前先收尾回答。
        参数是提示文本与等级，等级决定标记颜色。
        约束是回答流中途调用会先结算，避免输出交错。"""
        style = NOTICE_STYLES.get(level, NOTICE_STYLES["info"])
        self._finish_answer()
        self.console.print(
            Text.assemble((f"{style.marker}  ", style.color), (message, Palette.text))
        )

    def start_wait(self, message: str | None = None) -> None:
        """打开思考中转圈，提示模型正在工作。
        参数是可选等待文案，缺省按语言给默认文案。
        非终端或已有等待态时直接返回，避免重复启动。"""
        if not self.console.is_terminal:
            return
        if self._status is not None:
            if message:
                self._status.update(message)
            return
        self._status = self.console.status(
            message or self._label("正在思考…", "Thinking…"),
            spinner="dots",
            spinner_style=Palette.accent,
        )
        self._status.start()

    def stop_wait(self) -> None:
        """停掉等待转圈，输出新内容前先恢复光标。
        无参数无返回，多次调用安全。
        约束是必须与开始配对，事件回调里先停再画。"""
        if self._status is not None:
            self._status.stop()
            self._status = None

    def write_answer(
        self, delta: str, *, thread_id: str | None = None, role: str | None = None
    ) -> None:
        """追加一段模型增量文本，保持同一回答块内连续渲染。
        参数是增量文本，空串直接忽略。
        终端下用实时块渲染，非终端逐段打印，调用前会先停等待态。"""
        if not delta:
            return
        self.stop_wait()
        if not self._answer_open:
            identity = self._identity(thread_id, role)
            self.console.print(Text(identity.label(self.zh), style=f"bold {identity.color}"))
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
        """结束本轮回答，收尾等待态与回答块并空一行。
        无参数无返回，每轮流结束调用一次。
        约束是即使中途出错也要调用，保证下一轮从干净状态开始。"""
        self.stop_wait()
        self._finish_answer()
        self.console.print()

    def turn_summary(
        self,
        *,
        duration: float,
        tool_calls: int,
        failed_tools: int = 0,
        paused: bool = False,
        failed: bool = False,
    ) -> None:
        """在回答后显示一行紧凑的本轮执行摘要。"""
        self.stop_wait()
        self._finish_answer()
        if failed:
            state = self._label("失败", "failed")
            color = Palette.danger
        elif paused:
            state = self._label("已暂停", "paused")
            color = Palette.warning
        elif failed_tools:
            state = self._label("已完成，存在工具失败", "completed with tool failures")
            color = Palette.warning
        else:
            state = self._label("完成", "done")
            color = Palette.success
        details = [f"{duration:.1f}s"]
        if tool_calls:
            details.insert(
                0,
                self._label(f"{tool_calls} 次工具调用", f"{tool_calls} tool calls"),
            )
        self.console.print(
            Text.assemble(
                (state, color),
                ("  ·  " + "  ·  ".join(details), Palette.muted),
            )
        )

    def tool_event(
        self,
        name: str,
        status: str,
        *,
        duration: float | None = None,
        arguments: Any = None,
        result: Any = None,
        thread_id: str | None = None,
        role: str | None = None,
    ) -> None:
        """打印一行工具起止状态，方便跟随执行进度。
        参数是工具名与起止状态，另可带耗时秒数。
        调用前先收尾回答与等待态，避免与正文混排。"""
        self.stop_wait()
        self._finish_answer()
        if status == "started":
            return
        description = TOOL_LABELS.get(name, ("调用工具", "Use tool"))
        title = description[0] if self.zh else description[1]
        visual = TOOL_STYLES.get(status, StateStyle("·", Palette.text, status, status))
        elapsed = f"  {duration:.1f}s" if duration is not None else ""
        detail = tool_detail(name, arguments, result)
        suffix = f"  ·  {detail}" if detail else ""
        shown_name = name if name not in TOOL_LABELS else ""
        raw_name = f"  {shown_name}" if shown_name else ""
        self.console.print(
            Text.assemble(
                (f"{self._identity(thread_id, role).label(self.zh)}  ", self._identity(thread_id, role).color),
                (f"  {visual.marker}  ", visual.color),
                (title, Palette.text),
                (f"{raw_name}  ·  {visual.label(self.zh)}{suffix}{elapsed}", Palette.muted),
            )
        )

    def task_event(self, event: dict[str, Any]) -> None:
        """打印后台任务状态变化，暂停时给出审批入口。
        参数是任务事件字典，含任务号与状态。
        暂停态会多打印一行审批提示，其余状态只打印一行。"""
        self.stop_wait()
        self._finish_answer()
        status = str(event.get("status") or event.get("type", "").removeprefix("task."))
        task_id = str(event.get("task_id") or "?")
        thread_id = str(event.get("thread_id") or task_id)
        self._tasks[task_id] = {
            "task_id": task_id,
            "role": str(event.get("role") or ""),
            "title": str(event.get("title") or ""),
            "status": status,
        }
        identity = self._identity(
            thread_id,
            str(event.get("role") or "builder"),
            str(event.get("title") or ""),
        )
        visual = TASK_STYLES.get(status, StateStyle("◇", Palette.text, status, status))
        role = str(event.get("role") or "")
        role_suffix = f"  ·  {role}" if role else ""
        self.console.print(
            Text.assemble(
                (f"{identity.label(self.zh)}  ", identity.color),
                (f"  {visual.marker}  ", visual.color),
                (self._label("任务", "Task") + f" {task_id}", Palette.text),
                (f"  {visual.label(self.zh)}{role_suffix}", visual.color),
            )
        )
        if status == "paused":
            self.console.print(
                Text(
                    self._label(
                        f"     使用 /team approve {task_id} 处理审批",
                        f"     Use /team approve {task_id} to review the request",
                    ),
                    style=Palette.muted,
                )
            )
        result = event.get("result") or event.get("error")
        if status in {"idle", "failed", "paused"} and result:
            preview = str(result)
            if len(preview) > 800:
                preview = preview[:797] + "…"
            self.console.print(
                Panel(
                    Text(preview, style=Palette.text),
                    title=self._label("子 Agent 结果", "Child agent result"),
                    title_align="left",
                    border_style=identity.color,
                    padding=(0, 1),
                )
            )

    def review_event(self, event: dict[str, Any]) -> None:
        """展示 Jev 对一次工具调用的审理结论。"""
        self.stop_wait()
        self._finish_answer()
        action = str(event.get("action") or "ask")
        identity = self._identity(str(event.get("thread_id") or ""), "main")
        visual = REVIEW_STYLES.get(action, StateStyle("·", Palette.text, action, action))
        confidence = event.get("confidence")
        score = f"  {float(confidence):.0%}" if isinstance(confidence, (int, float)) else ""
        self.console.print(
            Text.assemble(
                (f"{identity.label(self.zh)}  ", identity.color),
                (f"  {visual.marker}  ", visual.color),
                ("Jev  ", Palette.review),
                (str(event.get("tool_name") or "tool"), Palette.text),
                (f"  ·  {visual.label(self.zh)}{score}", visual.color),
            )
        )

    def agent_event(self, event: dict[str, Any]) -> None:
        """展示子任务触发的父轮次。参数是父轮次唤醒事件，返回无。
        完成后若带回文本会直接写入回答块，暂停与失败只做提示不自动继续。"""
        kind = str(event.get("type") or "")
        task_id = str(event.get("task_id") or "?")
        thread_id = str(event.get("thread_id") or "?")
        if kind == "agent.wake.started":
            self.notice(
                self._label(
                    f"SAYA 收到任务 {task_id} 的通知，正在继续执行…",
                    f"SAYA received task {task_id} and is continuing…",
                )
            )
        elif kind == "agent.wake.completed":
            self.notice(
                self._label(
                    f"SAYA 已根据任务 {task_id} 继续",
                    f"SAYA continued from task {task_id}",
                ),
                level="success",
            )
            response = str(event.get("response") or "")
            if response:
                self.write_answer(response, thread_id=thread_id, role="main")
                self.end_turn()
        elif kind == "agent.wake.paused":
            self.notice(
                self._label(
                    f"SAYA 等待批准；输入 /approve {thread_id} 查看操作",
                    f"SAYA needs approval; use /approve {thread_id}",
                ),
                level="warning",
            )
        elif kind == "agent.wake.failed":
            self.notice(
                self._label("SAYA 自动继续失败", "SAYA continuation failed")
                + f"：{event.get('error') or ''}",
                level="error",
            )
        elif kind == "agent.wake.stopped":
            self.notice(
                self._label(
                    "SAYA 已在检查点停止，可下次启动继续",
                    "SAYA stopped at a checkpoint and can continue later",
                ),
                level="warning",
            )
        elif kind == "agent.wake.deferred":
            self.notice(
                self._label(
                    "SAYA 自动继续次数已达上限；下次用户输入会携带待处理消息",
                    "SAYA wake limit reached; pending messages will arrive with the next user input",
                ),
                level="warning",
            )

    def approval_intro(self, count: int) -> None:
        """打印审批开场，提醒逐项核对操作数。
        参数是待批准操作总数，返回无。
        调用在逐项卡片之前，真正提问走审批循环。"""
        self.stop_wait()
        self._finish_answer()
        self.notice(
            self._label(
                f"{count} 个操作等待批准，请逐项核对。",
                f"{count} action{'s' if count != 1 else ''} require approval. Review each one.",
            ),
            level="warning",
        )

    def approval_target(self, action: dict[str, Any]) -> str:
        """返回审批提问中使用的动作名称和目标摘要。"""
        name = str(action.get("name") or "tool")
        title_pair = TOOL_LABELS.get(name, (name, name))
        title = title_pair[0] if self.zh else title_pair[1]
        detail = tool_detail(name, self.redact(action.get("args", {})))
        suffix = f"  ·  {detail}" if detail else ""
        return f"{title} [{name}]{suffix}"

    def approval_action(self, index: int, count: int, action: dict[str, Any]) -> None:
        """打印单张审批卡片，展示工具名与脱敏后的参数。
        参数是序号总数与操作请求字典，返回无。
        卡片只展示不提问，提问由审批循环统一处理。"""
        args = action.get("args", {})
        visible = self.redact(args)
        body = Syntax(
            json.dumps(visible, ensure_ascii=False, indent=2, default=str),
            "json",
            theme="ansi_dark",
            word_wrap=True,
            background_color="default",
        )
        title = Text.assemble(
            (f"{index + 1}/{count}  ", "bold yellow"),
            (self.approval_target(action), "bold white"),
        )
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
        """用结构化视图展示一条斜杠命令结果。"""
        if not display:
            return
        self.stop_wait()
        self._finish_answer()
        self._results.render(command, display)

    def help(self, query: str = "") -> None:
        """展示命令目录或单条命令详情。"""
        self.stop_wait()
        self._finish_answer()
        self._results.help(query)
