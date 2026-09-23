"""斜杠命令结果的 Rich 视图。

该模块只把已经脱敏的数据转换成表格或面板，不参与命令执行和会话控制。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from rich import box
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .help import GROUPS, TOPICS, find_topic, format_help
from .theme import COMMAND_TITLES, MODEL_PROTOCOL_LABELS, Palette


class CommandResultRenderer:
    """集中渲染命令返回值，让主展示类只处理实时事件。"""

    def __init__(
        self,
        console: Console,
        *,
        is_chinese: Callable[[], bool],
        redact: Callable[[Any], Any],
        notice: Callable[..., None],
    ) -> None:
        self.console = console
        self._is_chinese = is_chinese
        self.redact = redact
        self.notice = notice

    @property
    def zh(self) -> bool:
        """读取展示器的实时语言设置。"""
        return self._is_chinese()

    def _label(self, zh: str, en: str) -> str:
        return zh if self.zh else en

    def render(self, command: str, display: str) -> None:
        """按命令类别把文本结果路由到专用视图。"""
        if not display:
            return
        name = command.split(maxsplit=1)[0].lstrip("/").lower()
        if name in {"help", "guide", "start"}:
            parts = command.split(maxsplit=1)
            self.help(parts[1] if len(parts) > 1 else "")
            return
        if name == "skill" and command.split(maxsplit=2)[1:2] == ["show"]:
            self.console.print(Markdown(display))
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
        elif (
            isinstance(data, dict)
            and name in {"models", "model", "config"}
            and isinstance(data.get("profiles"), dict)
        ):
            self._models(data)
        elif isinstance(data, list) and name == "history":
            self._history(data)
        elif isinstance(data, dict) and name == "skill" and data.get("activated"):
            self.notice(
                self._label(
                    f"已在当前会话启用 Skill：{data['activated']}",
                    f"Skill activated in this session: {data['activated']}",
                ),
                level="success",
            )
        elif isinstance(data, list) and name in {
            "team",
            "sessions",
            "session",
            "todos",
            "tools",
            "trace",
            "skills",
            "skill",
        }:
            self._list(name, data)
        elif isinstance(data, dict) and name in {"status", "stats", "context"}:
            self._status(data)
        else:
            self._json(name, data)

    def _json(self, name: str, data: Any) -> None:
        title = COMMAND_TITLES.get(name, (name or "结果", name or "Result"))
        self.console.print(
            Panel(
                Syntax(
                    json.dumps(self.redact(data), ensure_ascii=False, indent=2, default=str),
                    "json",
                    theme="ansi_dark",
                    word_wrap=True,
                    background_color="default",
                ),
                title=title[0 if self.zh else 1],
                title_align="left",
                border_style=Palette.panel,
                padding=(0, 1),
            )
        )

    def _status(self, data: dict[str, Any]) -> None:
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
        table = Table.grid(padding=(0, 2), expand=True)
        table.add_column(style=Palette.muted, no_wrap=True)
        table.add_column(overflow="fold")
        for key, label in labels.items():
            if key in data:
                table.add_row(label, self._status_value(key, data[key]))
        thread = data.get("thread")
        if isinstance(thread, dict):
            if thread.get("status"):
                table.add_row(self._label("运行", "Run"), str(thread["status"]))
            if thread.get("title"):
                table.add_row(self._label("标题", "Title"), str(thread["title"]))
        if data.get("mcp_error"):
            table.add_row("MCP", Text(str(data["mcp_error"]), style=Palette.danger))
        self.console.print(
            Panel(
                table,
                title=self._label("当前状态", "Current status"),
                title_align="left",
                border_style=Palette.panel,
                padding=(0, 1),
            )
        )

    def _status_value(self, key: str, value: Any) -> str:
        """把状态页的集合和用量字段压缩成一行。"""
        if key in {"active_tasks", "mcp_tools"} and isinstance(value, list):
            return str(len(value))
        if key == "usage" and isinstance(value, dict):
            values = tuple(
                value.get(name) for name in ("input_tokens", "output_tokens", "total_tokens")
            )
            if all(isinstance(item, int) for item in values):
                input_tokens, output_tokens, total_tokens = values
                return (
                    f"{self._label('输入', 'in')} {input_tokens:,}  ·  "
                    f"{self._label('输出', 'out')} {output_tokens:,}  ·  "
                    f"{self._label('合计', 'total')} {total_tokens:,}"
                )
        return "—" if value is None else str(value)

    def _models(self, data: dict[str, Any]) -> None:
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
            box=box.MINIMAL_DOUBLE_HEAD,
            show_edge=False,
            expand=True,
            title=self._label("模型", "Models"),
            title_style=f"bold {Palette.brand}",
            header_style=f"bold {Palette.muted}",
        )
        columns = (
            self._label("默认", "Default"),
            self._label("配置", "Profile"),
            self._label("接口协议", "API protocol"),
            self._label("模型", "Model"),
            self._label("上下文 / 输出", "Context / output"),
        )
        for heading in columns:
            table.add_column(heading, overflow="fold", no_wrap=heading == columns[0])
        default = data.get("default_profile")
        for name, profile in profiles.items():
            item = profile if isinstance(profile, dict) else {}
            context_length = item.get("context_length")
            max_output = item.get("max_output_tokens")
            table.add_row(
                self._label("是", "yes") if name == default else "",
                str(name),
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
                    "/model add 添加  ·  /model key <名称> 更新密钥  ·  /model use <名称> 切换",
                    "/model add  ·  /model key <name>  ·  /model use <name>",
                ),
                style=Palette.muted,
            )
        )

    def _list(self, name: str, rows: list[Any]) -> None:
        if not rows:
            self.notice(
                self._label(
                    "暂无 Skill；将 SKILL.md 放入项目 .agents/skills/<名称>/ 或用户目录 SAYACODE_HOME/skills/<名称>/。",
                    "No Skills found. Add SKILL.md under project .agents/skills/<name>/ or SAYACODE_HOME/skills/<name>/.",
                )
                if name in {"skills", "skill"}
                else self._label("暂无内容", "Nothing to show")
            )
            return
        fields = self._list_fields(name)
        table = Table(
            box=box.MINIMAL_DOUBLE_HEAD,
            show_edge=False,
            expand=True,
            title=COMMAND_TITLES.get(name, (name, name))[0 if self.zh else 1],
            title_style=f"bold {Palette.brand}",
            header_style=f"bold {Palette.muted}",
        )
        for _, label in fields:
            table.add_column(label, overflow="fold")
        for item in rows:
            if isinstance(item, dict):
                table.add_row(*(str(item.get(key) or "") for key, _ in fields))
            else:
                table.add_row(str(item), *("" for _ in fields[1:]))
        self.console.print(table)
        hint = {
            "tools": self._label("/tools <名称> 查看参数", "/tools <name> shows parameters"),
            "team": self._label("/team status <ID> 查看详情", "/team status <ID> shows details"),
            "skills": self._label("/skill use <名称> 启用", "/skill use <name> activates"),
            "skill": self._label("/skill use <名称> 启用", "/skill use <name> activates"),
        }.get(name)
        if hint:
            self.console.print(Text(hint, style=Palette.muted))

    def _list_fields(self, name: str) -> list[tuple[str, str]]:
        """返回各类列表的稳定列定义。"""
        if name == "team":
            return [
                ("task_id", "ID"),
                ("title", self._label("标题", "Title")),
                ("role", self._label("角色", "Role")),
                ("status", self._label("状态", "Status")),
            ]
        if name in {"sessions", "session"}:
            return [
                ("thread_id", "ID"),
                ("title", self._label("标题", "Title")),
                ("trust_level", self._label("信任", "Trust")),
            ]
        if name == "tools":
            return [
                ("name", self._label("工具", "Tool")),
                ("description", self._label("用途", "Description")),
            ]
        if name in {"skills", "skill"}:
            return [
                ("name", self._label("Skill", "Skill")),
                ("description", self._label("用途", "Description")),
                ("source", self._label("来源", "Source")),
            ]
        if name == "trace":
            return [
                ("at", self._label("时间", "Time")),
                ("event", self._label("事件", "Event")),
                ("run_id", "Run ID"),
            ]
        return [
            ("content", self._label("任务", "Task")),
            ("status", self._label("状态", "Status")),
        ]

    def _history(self, rows: list[Any]) -> None:
        if not rows:
            self.notice(self._label("暂无会话记录", "No conversation history"))
            return
        self.console.print(
            Text(self._label("会话记录", "Conversation history"), style=f"bold {Palette.brand}")
        )
        for item in rows:
            if not isinstance(item, dict):
                self.console.print(str(item), markup=False)
                continue
            role = str(item.get("role") or "message")
            body = str(item.get("content") or "")
            assistant = role in {"ai", "assistant"}
            self.console.print(
                Panel(
                    Markdown(body) if assistant else Text(body),
                    title="SAYA" if assistant else self._label("你", "YOU"),
                    title_align="left",
                    border_style=Palette.accent if assistant else Palette.panel,
                    padding=(0, 1),
                )
            )

    def help(self, query: str = "") -> None:
        """展示按任务流分组的命令总览或单条命令说明。"""
        language = "zh" if self.zh else "en"
        if query.strip():
            self._help_topic(query, language)
            return
        intro = Text(
            self._label(
                "直接输入任务开始工作。斜杠命令只用于控制会话和运行环境。",
                "Type a task to begin. Slash commands control the session and runtime.",
            ),
            style=Palette.muted,
        )
        if self.console.width < 58:
            blocks: list[Any] = [intro]
            for group, zh_name, en_name in GROUPS:
                blocks.extend(
                    (
                        Text(zh_name if self.zh else en_name, style="bold white"),
                        Text(self._group_commands(group), style=Palette.accent),
                    )
                )
            body: Any = Group(*blocks)
        else:
            table = Table.grid(padding=(0, 3), expand=True)
            table.add_column(style="bold white", no_wrap=True)
            table.add_column(style=Palette.accent, overflow="fold")
            for group, zh_name, en_name in GROUPS:
                table.add_row(zh_name if self.zh else en_name, self._group_commands(group))
            body = Group(intro, Text(""), table)
        self.console.print(
            Panel(
                body,
                title=self._label("命令", "Commands"),
                subtitle=self._label("/help <命令> 查看详情", "/help <command> for details"),
                title_align="left",
                subtitle_align="right",
                border_style=Palette.panel,
                padding=(0, 1),
            )
        )

    def _help_topic(self, query: str, language: str) -> None:
        asked = query.strip().lstrip("/").split(maxsplit=1)[0].lower()
        topic = find_topic(asked)
        if topic is None:
            self.notice(format_help(query, language=language), level="warning")
            return
        body = Text()
        body.append(topic.summary(language), style="white")
        aliases = ["/" + name for name in topic.names if name != asked]
        if aliases:
            body.append("\n\n" + self._label("别名  ", "Aliases  "), style=Palette.muted)
            body.append("  ".join(aliases), style=Palette.accent)
        body.append("\n\n" + self._label("用法  ", "Usage  "), style=Palette.muted)
        body.append(topic.shown_usage(language), style=f"bold {Palette.accent}")
        body.append("\n" + self._label("示例  ", "Example  "), style=Palette.muted)
        body.append(topic.shown_example(language), style=Palette.success)
        if detail := topic.detail(language):
            body.append("\n\n" + detail, style="white")
        self.console.print(
            Panel(
                body,
                title=f"/{asked}",
                title_align="left",
                border_style=Palette.accent,
                padding=(0, 1),
            )
        )

    @staticmethod
    def _group_commands(group: str) -> str:
        return "  ".join(
            item for topic in TOPICS if topic.group == group for item in ("/" + topic.name,)
        )


__all__ = ["CommandResultRenderer"]
