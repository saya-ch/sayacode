"""
SAYACODE CLI 主题系统

所有 Rich 渲染遵循一条铁律：外部内容（用户输入、模型输出、工具结果）绝不嵌入
f"[color]...[/]" 格式的 markup 字符串。必须通过 Text(markup=False) 或 Markdown() 等
Rich renderable 对象传递，从根源杜绝 [/] 被误解析为 closing tag 导致的崩溃。
"""

from __future__ import annotations

import re
import time
from typing import Dict, Iterable, Optional

from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.prompt import Confirm
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from .i18n import on_off, tr


# ═══════════════════════════════════════════════════════════════════════════════
# 色彩常量
# ═══════════════════════════════════════════════════════════════════════════════

class SayacodeColors:
    SAKURA_PINK  = "#FFB7C5"
    SAKURA_DEEP  = "#FF69B4"
    SAKURA_HOT   = "#FF1493"
    SAKURA_ROSE  = "#FF85A2"
    SAKURA_PASTEL = "#FFD1DC"
    SAKURA_BORDER = "#FFDDE8"

    PRIMARY    = SAKURA_PINK
    SECONDARY  = SAKURA_DEEP
    ACCENT     = SAKURA_HOT

    BACKGROUND  = "#0A0A0A"
    SURFACE     = "#161016"
    SURFACE_ALT = "#211521"
    TEXT        = "#E0E0E0"
    TEXT_DIM    = "#9A8693"
    TEXT_BRIGHT = "#FFFFFF"

    SUCCESS  = SAKURA_PINK
    WARNING  = "#FFB703"
    ERROR    = "#FF6B6B"
    INFO     = SAKURA_ROSE

    SESSION_BORDER = SAKURA_BORDER
    BORDER        = SAKURA_BORDER
    BORDER_BRIGHT = SAKURA_BORDER
    USER_INPUT_BORDER = "#FFFFFF"


# ═══════════════════════════════════════════════════════════════════════════════
# SpinnerMode 状态机
# ═══════════════════════════════════════════════════════════════════════════════


class SpinnerMode:
    """流式渲染状态 — 参考 Claude Code SpinnerMode.

    THINKING → 模型正在思考
    TEXT     → 正在生成文本回复
    TOOL_USE → 正在调用工具
    TOOL_RESULT → 工具返回结果
    IDLE     → 等待用户输入
    """
    THINKING = "thinking"
    TEXT = "text"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    IDLE = "idle"

    _ALL = frozenset({THINKING, TEXT, TOOL_USE, TOOL_RESULT, IDLE})

    @classmethod
    def is_valid(cls, value: str) -> bool:
        return value in cls._ALL

    @classmethod
    def all_modes(cls) -> frozenset[str]:
        return cls._ALL


# ═══════════════════════════════════════════════════════════════════════════════
# Rich 控制台
# ═══════════════════════════════════════════════════════════════════════════════

SAYACODE_THEME = Theme({
    "primary":     SayacodeColors.PRIMARY,
    "secondary":   SayacodeColors.SECONDARY,
    "accent":      SayacodeColors.ACCENT,
    "text":        SayacodeColors.TEXT,
    "text_dim":    SayacodeColors.TEXT_DIM,
    "text_bright": SayacodeColors.TEXT_BRIGHT,
    "success":     SayacodeColors.SUCCESS,
    "warning":     SayacodeColors.WARNING,
    "error":       SayacodeColors.ERROR,
    "info":        SayacodeColors.INFO,
})

console = Console(theme=SAYACODE_THEME)
plain_console = Console(force_terminal=True)

LIVE_RESPONSE_LINE_LIMIT = 18
# 思考链攒够这么多字符就先落一段盘（见 _take_reasoning_flush）。
# 太小 → 每个 token 打印一次，刷屏且把段落切碎；太大 → 又回到「盯着一个思考中等 6 分钟」。
REASONING_FLUSH_CHARS = 320


# ═══════════════════════════════════════════════════════════════════════════════
# 安全渲染原语 —— 所有外部内容进入 Rich 前必须经过这些函数
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_text(content: str, *, style: str = "") -> Text:
    """将任意外部文本包装为安全的 Rich Text。Text() 构造时不解析 markup。"""
    return Text(str(content), style=style)


def _safe_markdown(content: str) -> Markdown:
    """将任意外部文本渲染为 Markdown，不经过 Rich inline markup 解析。"""
    return Markdown(str(content), code_theme="monokai")


def _assemble(*parts: str | tuple[str, str]) -> Text:
    """
    安全拼接：每个 part 是纯文本字符串或 (text, style) 元组。
    与 Text.assemble() 相同的用法，但 text 部分强制 markup=False。
    """
    text = Text()
    for p in parts:
        if isinstance(p, tuple):
            text.append(p[0], style=p[1])
        else:
            text.append(str(p))
    return text


def _line(icon: str, icon_style: str, message: str) -> Text:
    """单行消息：[icon] message，message 为外部文本。"""
    return _assemble(
        (icon, icon_style),
        (" ", ""),
        (str(message), SayacodeColors.TEXT_DIM),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Logo 绘制
# ═══════════════════════════════════════════════════════════════════════════════

_logo_displayed = False

SAYACODE_LOGO = """\
╔════════════════════════════════════════════════════════════════════════════════╗
║                                                                                ║
║    ███████╗ █████╗ ██╗   ██╗ █████╗      ██████╗ ██████╗ ██████╗ ███████╗      ║
║    ██╔════╝██╔══██╗╚██╗ ██╔╝██╔══██╗    ██╔════╝██╔═══██╗██╔══██╗██╔════╝      ║
║    ███████╗███████║ ╚████╔╝ ███████║    ██║     ██║   ██║██║  ██║█████╗        ║
║    ╚════██║██╔══██║  ╚██╔╝  ██╔══██║    ██║     ██║   ██║██║  ██║██╔══╝        ║
║    ███████║██║  ██║   ██║   ██║  ██║    ╚██████╗╚██████╔╝██████╔╝███████╗      ║
║    ╚══════╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝     ╚═════╝ ╚═════╝ ╚═════╝ ╚══════╝      ║
║                                                                                ║
╚════════════════════════════════════════════════════════════════════════════════╝"""

_LOGO_COLORS = [
    "#FF269A", "#FF34A0", "#FF3FA6", "#FC4EAA", "#FC5AAB",
    "#FF69B4", "#FF73B9", "#FF7BBD", "#FA88C1", "#FC91C6",
]


def reset_logo_state() -> None:
    global _logo_displayed
    _logo_displayed = False


def print_logo(show_full: bool = True) -> None:
    global _logo_displayed
    if _logo_displayed and not show_full:
        console.print()
        console.print(Align.center(
            _assemble(("╭─ ", SayacodeColors.SAKURA_DEEP),
                      (" SAYACODE ", f"bold {SayacodeColors.SAKURA_HOT}"),
                      (" ─╮", SayacodeColors.SAKURA_DEEP))))
        console.print()
        return
    console.print()
    for idx, line in enumerate(SAYACODE_LOGO.splitlines()):
        console.print(Align.center(_safe_text(line, style=_LOGO_COLORS[idx])))
    console.print()
    _logo_displayed = True


# ═══════════════════════════════════════════════════════════════════════════════
# 提示符
# ═══════════════════════════════════════════════════════════════════════════════

def _shorten_value(value: str, max_width: int = 64) -> str:
    value = str(value)
    if len(value) <= max_width:
        return value
    head = max_width // 2 - 2
    return f"{value[:head]}...{value[-(max_width - head - 3):]}"


def _ctx_label(ratio: float) -> str:
    if ratio <= 0:
        return ""
    if ratio > 0.80:
        return f"ctx:{ratio:.0%}"
    if ratio > 0.60:
        return f"ctx:{ratio:.0%}"
    return f"ctx:{ratio:.0%}"


def short_prompt(workspace_name: str = "", context_usage: Optional[float] = None) -> Text:
    parts: list[str | tuple[str, str]] = []
    if workspace_name:
        parts.append((f"{_shorten_value(workspace_name, 20)} ", SayacodeColors.TEXT_DIM))
    if context_usage is not None and context_usage > 0:
        label = _ctx_label(context_usage)
        style = "bold red" if context_usage > 0.80 else ("yellow" if context_usage > 0.60 else SayacodeColors.TEXT_DIM)
        parts.append((f"{label} ", style))
    parts.append((">", f"bold {SayacodeColors.PRIMARY}"))
    return _assemble(*parts)


def format_token_hint(total_tokens: int) -> str:
    if total_tokens <= 0:
        return ""
    if total_tokens < 1000:
        return f"{total_tokens}t"
    return f"{total_tokens / 1000:.1f}kt"


# ═══════════════════════════════════════════════════════════════════════════════
# 摘要面板
# ═══════════════════════════════════════════════════════════════════════════════

def _build_summary_table(rows: Dict[str, str]) -> Table:
    table = Table.grid(padding=(0, 1))
    table.expand = True
    table.add_column(style=f"bold {SayacodeColors.TEXT_DIM}", no_wrap=True, ratio=1)
    table.add_column(style=SayacodeColors.TEXT, ratio=4, overflow="fold")
    for label, value in rows.items():
        table.add_row(str(label), "-" if value is None or value == "" else _safe_text(str(value)))
    return table


def _build_summary_panel(title: str, rows: Dict[str, str],
                         subtitle: Optional[str] = None,
                         footer: Optional[str] = None) -> Panel:
    renderables = [_build_summary_table(rows)]
    if footer:
        renderables.append(Text(""))
        renderables.append(_safe_text(footer, style=SayacodeColors.TEXT_DIM))
    return Panel(
        Group(*renderables),
        title=_assemble((title, f"bold {SayacodeColors.PRIMARY}")),
        subtitle=_safe_text(subtitle, style=SayacodeColors.TEXT_DIM) if subtitle else None,
        border_style=SayacodeColors.BORDER_BRIGHT,
        box=box.SQUARE, padding=(0, 1),
        style=f"on {SayacodeColors.BACKGROUND}",
    )


def print_summary_card(title: str, rows: Dict[str, str],
                       subtitle: Optional[str] = None,
                       footer: Optional[str] = None) -> None:
    console.print(_build_summary_panel(title, rows, subtitle=subtitle, footer=footer))


def print_split_summary_cards(
    left_title: str, left_rows: Dict[str, str],
    right_title: str, right_rows: Dict[str, str],
    left_subtitle: Optional[str] = None, right_subtitle: Optional[str] = None,
    left_footer: Optional[str] = None, right_footer: Optional[str] = None,
) -> None:
    layout = Table.grid(expand=True, padding=(0, 1))
    layout.add_column(ratio=1)
    layout.add_column(ratio=1)
    layout.add_row(
        _build_summary_panel(left_title, left_rows, subtitle=left_subtitle, footer=left_footer),
        _build_summary_panel(right_title, right_rows, subtitle=right_subtitle, footer=right_footer),
    )
    console.print(layout)


def print_message_header(label: str, color: str, meta: Optional[str] = None) -> None:
    h = _assemble(("● ", color), (label, f"bold {color}"))
    if meta:
        h.append(f"  {meta}", style=SayacodeColors.TEXT_DIM)
    console.print(h)


# ═══════════════════════════════════════════════════════════════════════════════
# 基础输出
# ═══════════════════════════════════════════════════════════════════════════════

def print_status(message: str) -> None:
    console.print(_line("·", SayacodeColors.INFO, message))


def print_success(message: str) -> None:
    console.print(_line("✓", SayacodeColors.SUCCESS, message))


def print_warning(message: str) -> None:
    console.print(_line("!", SayacodeColors.WARNING, message))


def print_error(message: str) -> None:
    console.print(_line("✗", SayacodeColors.ERROR, message))


def print_info(message: str) -> None:
    console.print(_line("i", SayacodeColors.TEXT_DIM, message))


def print_divider() -> None:
    console.print(Rule(style=SayacodeColors.BORDER))


def print_banner(title: str, subtitle: Optional[str] = None) -> None:
    banner = _assemble((title, f"bold {SayacodeColors.PRIMARY}"))
    if subtitle:
        banner.append(f"  {subtitle}", style=SayacodeColors.TEXT_DIM)
    console.print()
    console.print(Rule(banner, style=SayacodeColors.BORDER_BRIGHT))
    console.print()


def confirm_action(prompt: str, default: bool = False) -> bool:
    return Confirm.ask(f"{prompt}", default=default, console=console)


# ═══════════════════════════════════════════════════════════════════════════════
# 用户消息
# ═══════════════════════════════════════════════════════════════════════════════

def _render_plain_block(content: str, *, indent: int = 2, style: str = "") -> Text:
    text = Text()
    prefix = " " * indent
    lines = str(content or " ").splitlines() or [" "]
    for idx, line in enumerate(lines):
        if idx:
            text.append("\n")
        text.append(prefix, style=SayacodeColors.TEXT_DIM)
        text.append(line or " ", style=style)
    return text


def _user_header() -> Text:
    return _assemble(
        ("› ", f"bold {SayacodeColors.PRIMARY}"),
        ("user", f"bold {SayacodeColors.TEXT_DIM}"),
    )


def _build_user_message(content: str) -> Group:
    return Group(
        _user_header(),
        _render_plain_block(content, style=SayacodeColors.TEXT),
        Text(""),
    )


def print_user_message(content: str) -> None:
    console.print(_build_user_message(content))


# ═══════════════════════════════════════════════════════════════════════════════
# Agent 消息渲染
# ═══════════════════════════════════════════════════════════════════════════════

def _compact_markdown(content: str) -> str:
    """轻量规范化：统一换行、保护代码块、单换行转硬换行保持自然分段。"""
    if not content:
        return ""
    normalized = str(content).replace("\r\n", "\n").replace("\r", "\n")
    segments = normalized.split("```")
    for i in range(0, len(segments), 2):
        s = segments[i]
        s = re.sub(r"[ \t]+(\n|$)", r"\1", s, flags=re.MULTILINE)
        s = re.sub(r"\n{3,}", "\n\n", s)
        # 单换行 → 硬换行，自然分段不被合并
        s = re.sub(r"(?<!\n)\n(?!\n)", "  \n", s)
        segments[i] = s
    return "```".join(segments).strip() or normalized.strip()


def _saya_prefix(streaming: bool = False) -> str:
    if streaming:
        frames = ["·", "•", "●", "•"]
        return frames[int(time.monotonic() * 4) % len(frames)]
    return "●"


def _agent_header(*, streaming: bool = False, phase: Optional[str] = None) -> Text:
    header = _assemble(
        (_saya_prefix(streaming), SayacodeColors.SECONDARY),
        (" SAYA", f"bold {SayacodeColors.SECONDARY}"),
    )
    if phase:
        header.append(f"  {phase}", style=SayacodeColors.TEXT_DIM)
    return header


def agent_status_text(message: str = "") -> Text:
    return _agent_header(phase=message or tr("thinking"))


def _build_agent_message(
    content: str,
    *,
    show_header: bool = True,
    streaming: bool = False,
    loading_message: Optional[str] = None,
    tool_states: Optional[list[dict]] = None,
) -> Group:
    body: list = []

    if show_header:
        body.append(_agent_header(streaming=streaming, phase="responding" if streaming else None))

    if tool_states:
        body.append(_tool_indicator(tool_states[-1]))

    if content.strip():
        body.append(Padding(_safe_markdown(_compact_markdown(content)), (0, 0, 0, 2)))
        if streaming:
            body.append(_build_work_status_line("responding", loading_message or tr("thinking")))
    elif loading_message:
        body.append(_build_work_status_line("thinking", loading_message))
    else:
        body.append(_safe_text(" "))

    body.append(Text(""))
    return Group(*body)


def print_agent_message(content: str, *, show_header: bool = True) -> None:
    console.print(_build_agent_message(content or " ", show_header=show_header))


# ═══════════════════════════════════════════════════════════════════════════════
# 工具状态指示器
# ═══════════════════════════════════════════════════════════════════════════════

def _shorten_tool_preview(value: str, max_chars: int = 220) -> str:
    collapsed = _sanitize_tool_preview(value)
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[:max_chars - 3] + "..."


def _sanitize_tool_preview(value: str) -> str:
    text = " ".join(str(value).split())
    # 工具预览属于状态元信息，不是 assistant 正文。保持安静、适配终端即可。
    text = re.sub(r"[\U00010000-\U0010ffff]", "", text)
    text = re.sub(r"[\u2600-\u27BF\uFE0E\uFE0F]", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _parse_tool_stream_message(chunk: str) -> tuple[str, Optional[dict]]:
    """解析流式 chunk，把状态标记转成结构化事件。

    标记：``[调用工具:...]``、``[工具结果:...]``、``[工具执行出错:...]``、``[思考:...]``。

    ``[思考:...]`` 由 agent 层从厂商的推理字段产出（``reasoning`` /
    ``reasoning_content`` / ``reasoning_details``）。它走的是**状态通道**，
    不会被计入最终回复。
    """
    if not isinstance(chunk, str):
        return str(chunk), None
    text = chunk.strip()
    for prefix, kind in [
        ("[调用工具:", "start"),
        ("[工具结果:", "result"),
        ("[工具执行出错:", "error"),
        ("[思考:", "reasoning"),
    ]:
        if text.startswith(prefix) and text.endswith("]"):
            inner = text[len(prefix):-1].strip()
            if kind in {"start", "reasoning"}:
                return "", {"kind": kind, "name": inner or "tool"}
            # result / error: 格式为 "工具名 | 内容"
            if " | " in inner:
                name, preview = inner.split(" | ", 1)
                return "", {"kind": kind, "name": name.strip(), "preview": preview.strip()}
            return "", {"kind": kind, "name": "tool", "preview": inner}
    return chunk, None


def _tool_indicator(state: dict) -> Text:
    status = state.get("status", "done")
    name = state.get("name", "tool")
    preview = state.get("preview", "")
    ind = _assemble(("  ", SayacodeColors.TEXT_DIM))

    if status == "running":
        ind.append("* ", style=SayacodeColors.SECONDARY)
        ind.append(name, style=f"bold {SayacodeColors.TEXT_DIM}")
        ind.append(f" {tr('common.running')}", style=SayacodeColors.TEXT_DIM)
    elif status == "switching":
        ind.append("~ ", style=SayacodeColors.SECONDARY)
        ind.append(preview, style=SayacodeColors.TEXT_DIM)
    elif status == "error":
        ind.append("x ", style=SayacodeColors.ERROR)
        ind.append(name, style=f"bold {SayacodeColors.TEXT_DIM}")
        p = _shorten_tool_preview(preview, 80)
        if p:
            ind.append(f" [{tr('common.error').lower()}: {p}]", style=SayacodeColors.ERROR)
    else:
        ind.append("+ ", style=SayacodeColors.SUCCESS)
        ind.append(name, style=SayacodeColors.TEXT_DIM)
        p = _shorten_tool_preview(preview, 80)
        if p:
            ind.append(f" ({p})", style=SayacodeColors.TEXT_DIM)
    return ind


def _format_elapsed(seconds: float) -> str:
    """把已耗时格式化成 ``12s`` / ``1m05s`` / ``1h02m``。"""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def _build_work_status_line(
    phase: str,
    message: Optional[str] = None,
    *,
    elapsed: Optional[float] = None,
) -> Group:
    """状态行。

    **带上已耗时是刻意的**：长模型调用期间这是唯一能证明「还活着、且在推进」的信息。
    实测有一次 grep + 模型调用让用户盯着一个没有任何时间信息的「思考中…」等了 6 分钟，
    无法判断是卡死还是在跑。
    """
    label = message or phase
    if elapsed is not None and elapsed >= 1:
        label = f"{label} {_format_elapsed(elapsed)}"
    return Group(
        Padding(
            Spinner(
                "dots",
                text=_safe_text(label, style=SayacodeColors.TEXT_DIM),
                style=SayacodeColors.SECONDARY,
            ),
            (0, 0, 0, 2),
        )
    )


def _take_reasoning_flush(buffer: str, *, force: bool) -> tuple[str, str]:
    """把攒着的思考文本切成「现在落盘的」和「继续攒着的」两部分。

    ``force=True`` 表示这段思考已经结束（来了别的事件，或流已经收尾），全部落盘。
    否则只在攒够 ``REASONING_FLUSH_CHARS`` 时落盘，并尽量切在换行处，
    让落盘的段落读起来完整一些。
    """
    if not buffer.strip():
        return "", ""
    if not force and len(buffer) < REASONING_FLUSH_CHARS:
        return "", buffer
    if not force:
        cut = buffer.rfind("\n")
        if cut >= REASONING_FLUSH_CHARS // 2:
            return buffer[:cut], buffer[cut:]
    return buffer, ""


def _print_reasoning_paragraph(text: str, *, label: bool) -> None:
    """把一段思考链**持久**打印出来：暗色、缩进，与正文在视觉上明确区分。"""
    body = text.strip("\n")
    if not body.strip():
        return
    if label:
        console.print(
            _assemble(("  · ", SayacodeColors.TEXT_DIM), (tr("reasoning.label"), SayacodeColors.TEXT_DIM))
        )
    console.print(Padding(_safe_text(body, style=SayacodeColors.TEXT_DIM), (0, 0, 0, 4)))


def _format_tool_log_line(entry: dict) -> Text:
    name = entry.get("name", "tool")
    status = entry.get("status", "done")
    preview = _shorten_tool_preview(entry.get("preview", ""), 96)
    if status == "running":
        line = _assemble(("  * ", SayacodeColors.SECONDARY), (name, f"bold {SayacodeColors.TEXT_DIM}"))
        line.append(f" {tr('common.running')}", style=SayacodeColors.TEXT_DIM)
        return line
    if status == "error":
        line = _assemble(("  x ", SayacodeColors.ERROR), (name, f"bold {SayacodeColors.ERROR}"))
        if preview:
            line.append(f"  {preview}", style=SayacodeColors.ERROR)
        return line
    line = _assemble(("  + ", SayacodeColors.SUCCESS), (name, SayacodeColors.TEXT_DIM))
    if preview:
        line.append(f"  {preview}", style=SayacodeColors.TEXT_DIM)
    return line


def _clip_response_for_live(content: str) -> str:
    lines = str(content).splitlines()
    if len(lines) <= LIVE_RESPONSE_LINE_LIMIT:
        return content
    clipped = lines[-LIVE_RESPONSE_LINE_LIMIT:]
    return "\n".join(["..."] + clipped)


# ═══════════════════════════════════════════════════════════════════════════════
# 流式渲染
# ═══════════════════════════════════════════════════════════════════════════════

def render_streaming_agent_message(
    chunks: Iterable[str],
    *,
    thinking_message: Optional[str] = None,
    stream_text: bool = True,
) -> str:
    """流式渲染 Agent 回复 —— 思考链与工具活动**按发生顺序持久打印**。

    「时序」和「持久」都是刻意的。此前所有内容都塞在一个 ``transient=True`` 的
    ``Live`` 区域里，退出时整块被终端擦掉，屏幕上只留下一行折叠摘要：用户既看不到
    思考过程，也无法回看这一轮到底调用过哪些工具。实测一次回答里 47 个 chunk 带推理、
    只有 7 个带正文；一次 grep 加模型调用能让用户盯着一个「思考中…」等 6 分钟 ——
    这些信息看完就没了，等于没有。

    现在的分工：

    * **持久层**（``console.print``，落进终端滚动区，回合结束仍在）：思考链段落、
      工具调用/结果行、正文段落。打印的先后就是事件真实发生的先后。
    * **临时层**（``Live``，``transient=True``）：只负责底部那行「现在在做什么 +
      已耗时」，外加正在生成、尚未落盘的正文预览。它表达的本来就是「此刻」，
      被擦掉是正确的。

    ``stream_text=False`` 只影响正文：正文不在流中逐段落盘，改为结尾一次性给出；
    思考链与工具活动照旧实时且持久 —— 这正是 ``/prefs`` 里关掉「流式输出」之后
    用户依然需要看到的进展信息。
    """
    thinking_message = thinking_message or tr("thinking")
    started_at = time.monotonic()

    full_response = ""      # 本轮全部正文，作为返回值
    text_buffer = ""        # 已收到、还没落盘的正文
    reasoning_buffer = ""   # 已收到、还没落盘的思考
    reasoning_open = False  # 当前这段思考是否已经打过「思考」标签
    header_printed = False
    active_tool: Optional[str] = None

    def _print_header() -> None:
        """在第一个持久行之前打一次 ``● SAYA``；没有任何内容就什么都不打。"""
        nonlocal header_printed
        if not header_printed:
            console.print(_agent_header())
            header_printed = True

    def _flush_reasoning(*, force: bool = False) -> None:
        nonlocal reasoning_buffer, reasoning_open
        ready, reasoning_buffer = _take_reasoning_flush(reasoning_buffer, force=force)
        if not ready.strip():
            return
        _print_header()
        _print_reasoning_paragraph(ready, label=not reasoning_open)
        reasoning_open = True

    def _close_reasoning() -> None:
        """思考段结束（来了工具事件或正文）：剩下的全部落盘，标签留给下一段重打。"""
        nonlocal reasoning_open
        _flush_reasoning(force=True)
        reasoning_open = False

    def _flush_text(*, force: bool = False) -> None:
        """把攒下的正文落盘。

        只在**段落边界**落盘（工具调用开始 / 本轮结束），这样每个 Markdown 块都是完整的。
        ``stream_text=False`` 时中途不落盘，结尾由 ``force=True`` 一次性给出。
        """
        nonlocal text_buffer
        if not text_buffer.strip():
            text_buffer = ""
            return
        if not force and not stream_text:
            return
        _print_header()
        console.print(Padding(_safe_markdown(_compact_markdown(text_buffer)), (0, 0, 0, 2)))
        text_buffer = ""

    def _print_tool(entry: dict) -> None:
        _print_header()
        console.print(_format_tool_log_line(entry))

    def _live_message() -> str:
        """底部状态行的文案：永远是「此刻在做什么」，加上已耗时。"""
        if active_tool:
            return f"{active_tool} {tr('common.running')}"
        if text_buffer.strip():
            return tr("stream.generating")
        return thinking_message

    def _live_renderable() -> Group:
        """底部临时状态区：尚未落盘的正文预览 +「在做什么 + 已耗时」。"""
        body: list = [_agent_header(streaming=True)]
        preview = text_buffer if stream_text else ""
        if preview.strip():
            body.append(
                Padding(
                    _safe_markdown(_compact_markdown(_clip_response_for_live(preview))),
                    (0, 0, 0, 2),
                )
            )
        body.append(
            _build_work_status_line("thinking", _live_message(), elapsed=time.monotonic() - started_at)
        )
        body.append(Text(""))
        return Group(*body)

    # 两个参数都传，缺一不可：
    #
    # * 位置参数（初值）—— ``Live.__enter__`` 用 ``_renderable is not None`` 决定要不要
    #   绘制**首帧**（``start(refresh=...)``）。只给 ``get_renderable`` 的话首帧不画。
    # * ``get_renderable`` —— ``Live.renderable`` 每次重绘都会调用它，因此「已耗时」
    #   会自己走。用 ``update(预构建的 Group)`` 会把时间**冻在构造那一刻**，
    #   而真正需要看时间恰恰是收不到任何东西的时候（实测一次网关停摆，py-spy 栈停在
    #   httpcore 的 ``_receive_response_headers``，状态行一直显示不出耗时）。
    #
    # 循环里的 ``console.print`` 是安全的：rich 的 ``Live`` 以 render hook 的形式接在
    # ``Console.print`` 上，每次打印都会先擦掉 Live 区域、写完内容再把 Live 区域重画到
    # 下面。持久内容因此按顺序留在滚动区，而 Live 区域永远在最后一行。
    with Live(
        _live_renderable(),
        get_renderable=_live_renderable,
        console=console,
        refresh_per_second=10,
        transient=True,
    ) as live:
        for chunk in chunks:
            if not chunk:
                continue
            display_text, tool_event = _parse_tool_stream_message(chunk)

            if tool_event is None:
                if display_text:
                    text_buffer += display_text
                    full_response += display_text
                    # 正文出现了，说明刚才那段思考已经结束
                    if text_buffer.strip():
                        _close_reasoning()
                live.refresh()
                continue

            if tool_event["kind"] == "reasoning":
                reasoning_buffer += tool_event.get("name", "")
                _flush_reasoning()  # 攒够阈值就先落一段，别让人干等
                continue

            # 工具事件：先把比它更早发生的思考与正文落盘，再打工具行 —— 顺序才对得上
            _close_reasoning()
            _flush_text()
            name = tool_event.get("name", "tool")
            if tool_event["kind"] == "start":
                active_tool = name
                _print_tool({"name": name, "status": "running", "preview": ""})
            elif tool_event["kind"] == "result":
                active_tool = None
                _print_tool({"name": name, "status": "done", "preview": tool_event.get("preview", "")})
            elif tool_event["kind"] == "error":
                active_tool = None
                _print_tool({"name": name, "status": "error", "preview": tool_event.get("preview", "")})
            live.refresh()

    _close_reasoning()
    _flush_text(force=True)

    return full_response


# ═══════════════════════════════════════════════════════════════════════════════
# 引导与帮助
# ═══════════════════════════════════════════════════════════════════════════════

def print_welcome() -> None:
    print_summary_card(
        tr("welcome.title"),
        {"Role": tr("welcome.role"), "Commands": tr("welcome.commands"), "Start": tr("welcome.start")},
        subtitle=tr("welcome.subtitle"), footer=tr("welcome.footer"))
    console.print()


def print_farewell() -> None:
    console.print()
    console.print(_safe_text(tr("session.ended"), style=SayacodeColors.TEXT_DIM))
    console.print()


def print_feature_guide(startup: bool = False) -> None:
    title = tr("guide.quick_start") if startup else tr("guide.guide")
    subtitle = tr("guide.starter") if startup else tr("guide.walkthrough")
    print_banner(title, subtitle)

    wf = Table(box=box.SIMPLE_HEAD, border_style=SayacodeColors.BORDER,
               header_style=f"bold {SayacodeColors.PRIMARY}", expand=True, show_edge=False)
    wf.add_column(tr("guide.goal"), style=f"bold {SayacodeColors.TEXT_BRIGHT}", no_wrap=True)
    wf.add_column(tr("guide.how"), style=SayacodeColors.TEXT)
    wf.add_row(tr("guide.inspect"), tr("guide.inspect_desc"))
    wf.add_row(tr("guide.debug"), tr("guide.debug_desc"))
    wf.add_row(tr("guide.edit"), tr("guide.edit_desc"))
    wf.add_row(tr("guide.commands"), tr("guide.commands_desc"))
    wf.add_row(tr("guide.mcp"), tr("guide.mcp_desc"))
    wf.add_row(tr("guide.paths"), tr("guide.paths_desc"))
    wf.add_row(tr("guide.models"), tr("guide.models_desc"))
    console.print(Panel(wf, title=_assemble((tr("guide.starter"), f"bold {SayacodeColors.PRIMARY}")),
                        border_style=SayacodeColors.BORDER_BRIGHT, box=box.ROUNDED))
    console.print()
    print_summary_card(
        tr("guide.examples"),
        {tr("guide.project_scan_label"): tr("guide.project_scan"),
         tr("guide.bug_fix_label"): tr("guide.bug_fix"),
         tr("guide.code_search_label"): tr("guide.code_search"),
         tr("guide.commands_label"): tr("guide.commands_example"),
         tr("guide.mcp_label"): tr("guide.mcp_example"),
         tr("guide.paths_label"): tr("guide.paths_example")},
        footer=tr("guide.footer"))
    console.print()


def print_tool_call(tool_name: str, args: dict) -> None:
    console.print(_assemble(("  -> ", SayacodeColors.TEXT_DIM), (tool_name, "")))
    if args:
        for k, v in list(args.items())[:3]:
            console.print(_assemble((f"     {k}: ", SayacodeColors.TEXT_DIM), (str(v), "")))


def print_thinking(message: str = "") -> None:
    console.print(_assemble(("[*] ", SayacodeColors.WARNING),
                            (message or tr("thinking"), "")))


# ═══════════════════════════════════════════════════════════════════════════════
# 状态信息
# ═══════════════════════════════════════════════════════════════════════════════

def print_status_info(workspace: str, model: str, mcp_servers: int = 0,
                      stream_output: Optional[bool] = None) -> None:
    rows = {tr("status.workspace"): _shorten_value(workspace),
            tr("status.model"): _shorten_value(model)}
    if mcp_servers:
        rows[tr("status.mcp")] = tr("status.server_count", count=mcp_servers)
    if stream_output is not None:
        rows[tr("status.streaming")] = on_off(stream_output)
    rows[tr("status.commands")] = tr("session.commands")
    print_summary_card(tr("session.title"), rows, subtitle=tr("session.subtitle"),
                       footer=tr("session.footer"))
    console.print()


def print_help() -> None:
    print_banner(tr("help.title"), tr("help.subtitle"))
    cmds = Table(box=box.SIMPLE_HEAD, border_style=SayacodeColors.BORDER,
                 header_style=f"bold {SayacodeColors.PRIMARY}", expand=True, show_edge=False)
    cmds.add_column(tr("help.category"), style=SayacodeColors.TEXT_DIM, no_wrap=True)
    cmds.add_column(tr("help.command"), style=f"bold {SayacodeColors.TEXT_BRIGHT}", no_wrap=True)
    cmds.add_column(tr("help.description"), style=SayacodeColors.TEXT)
    for category, entries in [
        (tr("help.category_start"),     [("/help", tr("help.page")), ("/guide", tr("help.guide")), ("/start", tr("help.start"))]),
        (tr("help.category_inspect"),   [("/status", tr("help.status")), ("/workspace", tr("help.workspace")), ("/context", tr("help.context")), ("/symbols", tr("help.symbols")), ("/analyze", tr("help.analyze")), ("/history", tr("help.history"))]),
        (tr("help.category_sessions"),  [("/sessions", tr("help.sessions")), ("/session new", tr("help.session_new")), ("/session use", tr("help.session_use")), ("/session list", tr("help.session_list")), ("/session current", tr("help.session_current")), ("/session rename", tr("help.session_rename"))]),
        (tr("help.category_config"),    [("/model", tr("help.model")), ("/model list", tr("help.model_list")), ("/model use", tr("help.model_use")), ("/model add", tr("help.model_add")), ("/model test", tr("help.model_test")), ("/model show", tr("help.model_show")), ("/mode", tr("help.mode")), ("/prefs", tr("help.prefs")), ("/settings", tr("help.settings")), ("/config", tr("help.config")), ("/lang", tr("lang.command.desc")), ("/style", tr("style.command.desc"))]),
        (tr("help.category_agent"),     [("/reset", tr("help.reset")), ("/compact", tr("help.compact")), ("/git", tr("help.git"))]),
        (tr("help.category_tools"),     [("/tools", tr("help.tools")), ("/commands", tr("help.commands")), ("/mcp", tr("help.mcp")), ("/paths", tr("help.paths")), ("/stats", tr("help.stats"))]),
        (tr("help.category_security"),  [("/permissions", tr("help.permissions")), ("/doctor", tr("help.doctor")), ("/hooks", tr("help.hooks"))]),
        (tr("help.category_exit"),      [("/clear", tr("help.clear")), ("/quit", tr("help.quit"))]),
    ]:
        for cmd, desc in entries:
            cmds.add_row(category, cmd, desc)
    console.print(Panel(cmds, title=_assemble(("Commands", f"bold {SayacodeColors.PRIMARY}")),
                        border_style=SayacodeColors.BORDER_BRIGHT, box=box.ROUNDED))
    console.print()
    print_split_summary_cards(
        tr("help.shortcuts"),
        {"Ctrl+C": tr("help.shortcut_interrupt"), "Ctrl+L": tr("help.shortcut_clear"), "Arrow keys": tr("help.shortcut_protocol")},
        tr("help.high_signal"),
        {"/status": tr("help.signal_status"), "/workspace": tr("help.signal_workspace"),
         "/sessions": tr("help.signal_sessions"), "/model": tr("help.signal_model")},
        right_footer=tr("help.footer"))
    console.print()


# ═══════════════════════════════════════════════════════════════════════════════
# 导出
# ═══════════════════════════════════════════════════════════════════════════════

__all__ = [
    'console', 'plain_console', 'SayacodeColors', 'SAYACODE_LOGO',
    'reset_logo_state',
    '_assemble', '_safe_text', '_safe_markdown',
    'print_logo', 'print_welcome', 'print_farewell',
    'print_summary_card', 'print_split_summary_cards', 'print_message_header',
    'print_help', 'short_prompt', 'print_status', 'print_success',
    'print_warning', 'print_error', 'print_info', 'print_divider',
    'print_banner', 'confirm_action', 'print_user_message',
    'print_agent_message', 'render_streaming_agent_message',
    'print_tool_call', 'print_thinking', 'print_status_info',
    'print_feature_guide', 'format_token_hint', 'agent_status_text',
]
