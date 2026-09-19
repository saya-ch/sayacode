"""
CLI 主题归属 lib.cli 包，由 theme 模块承载。

所有 Rich 渲染遵循一条铁律：外部内容（用户输入、模型输出、工具结果）绝不嵌入
f"[color]...[/]" 格式的 markup 字符串。必须通过 Text(markup=False) 或 Markdown() 等
Rich renderable 对象传递，从根源杜绝 [/] 被误解析为 closing tag 导致的崩溃。
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Iterable, Optional

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

from ..i18n import on_off, tr


# ═══════════════════════════════════════════════════════════════════════════════
# 色彩常量
# ═══════════════════════════════════════════════════════════════════════════════

class SayacodeColors:
    """集中存放 CLI 配色常量，供 Rich 主题与组件复用。"""
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
        """判断状态值是否合法。"""
        return value in cls._ALL

    @classmethod
    def all_modes(cls) -> frozenset[str]:
        """返回全部合法状态集合。"""
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
# 思考链攒够这么多字符就先落一段盘。
# 太小会刷屏且切碎段落，太大则长时间无输出。
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
    """重置 Logo 展示状态。"""
    global _logo_displayed
    _logo_displayed = False


def print_logo(show_full: bool = True) -> None:
    """打印 SAYACODE 标识横幅。"""
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
    return f"ctx:{ratio:.0%}"


def short_prompt(workspace_name: str = "", context_usage: Optional[float] = None) -> Text:
    """组装短提示符（含工作区与 context 用量）。"""
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
    """格式化 token 用量提示。"""
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
    """打印单张摘要卡片。"""
    console.print(_build_summary_panel(title, rows, subtitle=subtitle, footer=footer))


def print_split_summary_cards(
    left_title: str, left_rows: Dict[str, str],
    right_title: str, right_rows: Dict[str, str],
    left_subtitle: Optional[str] = None, right_subtitle: Optional[str] = None,
    left_footer: Optional[str] = None, right_footer: Optional[str] = None,
) -> None:
    """并排打印两张摘要卡片。"""
    layout = Table.grid(expand=True, padding=(0, 1))
    layout.add_column(ratio=1)
    layout.add_column(ratio=1)
    layout.add_row(
        _build_summary_panel(left_title, left_rows, subtitle=left_subtitle, footer=left_footer),
        _build_summary_panel(right_title, right_rows, subtitle=right_subtitle, footer=right_footer),
    )
    console.print(layout)


def print_message_header(label: str, color: str, meta: Optional[str] = None) -> None:
    """打印消息头（标识加标签）。"""
    h = _assemble(("● ", color), (label, f"bold {color}"))
    if meta:
        h.append(f"  {meta}", style=SayacodeColors.TEXT_DIM)
    console.print(h)


# ═══════════════════════════════════════════════════════════════════════════════
# 基础输出
# ═══════════════════════════════════════════════════════════════════════════════

def print_status(message: str) -> None:
    """打印普通状态行。"""
    console.print(_line("·", SayacodeColors.INFO, message))


def print_success(message: str) -> None:
    """打印成功状态行。"""
    console.print(_line("✓", SayacodeColors.SUCCESS, message))


def print_warning(message: str) -> None:
    """打印警告状态行。"""
    console.print(_line("!", SayacodeColors.WARNING, message))


def print_error(message: str) -> None:
    """打印错误状态行。"""
    console.print(_line("✗", SayacodeColors.ERROR, message))


def print_info(message: str) -> None:
    """打印提示状态行。"""
    console.print(_line("i", SayacodeColors.TEXT_DIM, message))


def print_divider() -> None:
    """打印分隔线。"""
    console.print(Rule(style=SayacodeColors.BORDER))


def print_banner(title: str, subtitle: Optional[str] = None) -> None:
    """打印标题横幅。"""
    banner = _assemble((title, f"bold {SayacodeColors.PRIMARY}"))
    if subtitle:
        banner.append(f"  {subtitle}", style=SayacodeColors.TEXT_DIM)
    console.print()
    console.print(Rule(banner, style=SayacodeColors.BORDER_BRIGHT))
    console.print()


def confirm_action(prompt: str, default: bool = False) -> bool:
    """弹出确认提示并返回用户选择。"""
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
    """打印用户消息块。"""
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
    """返回 Agent 状态行文本。"""
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
    """打印 Agent 回复块。"""
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


def _parse_tool_stream_message(chunk: Any) -> tuple[str, Optional[dict]]:
    """解析流式块，把结构化事件转成展示文本。

    只收结构化事件，字符串即普通文本，不再解析旧标记。
    返回展示文本与事件，事件类型为开始，结果，报错，思考之一。
    """
    from lib.runtime.events import StreamEvent

    if isinstance(chunk, StreamEvent):
        return chunk.display_text, _stream_event_to_dict(chunk)
    return str(chunk) if not isinstance(chunk, str) else chunk, None


def _stream_event_to_dict(event: Any) -> dict:
    """StreamEvent → 旧事件 dict（_parse_tool_stream_message 的返回形状）。

    中间件层（权限/安全）按这个形状判定 kind；theme 渲染层用 display_text。
    """
    if event.kind == "reasoning":
        return {"kind": "reasoning", "name": event.text}
    if event.kind == "tool_start":
        return {"kind": "start", "name": event.tool_name}
    if event.kind == "tool_result":
        return {"kind": "result", "name": event.tool_name, "preview": event.preview}
    if event.kind == "tool_error":
        return {"kind": "error", "name": event.tool_name, "preview": event.preview}
    return {"kind": "text", "name": event.text}


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
    """把已耗时格式化成 12s / 1m05s / 1h02m。"""
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

    带上已耗时，长调用期间这是唯一能证明还在推进的信息。
    无时间信息会无法判断是卡死还是在跑。
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

    force=True 表示这段思考已经结束（来了别的事件，或流已经收尾），全部落盘。
    否则只在攒够 REASONING_FLUSH_CHARS 时落盘，并尽量切在换行处，
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
    """把一段思考链持久打印出来：暗色、缩进，与正文在视觉上明确区分。"""
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
    chunks: Iterable[Any],
    *,
    thinking_message: Optional[str] = None,
    stream_text: bool = True,
) -> str:
    """流式渲染回复，思考链与工具活动按发生顺序持久打印。

    只收结构化事件与纯文本，字符串即原文，不再做标记解析。
    持久层负责滚动区内容，回合结束仍在，包括思考段落，工具行，正文段落，
    打印先后即事件先后。
    临时层只负责底部当前状态加已耗时，以及未落盘的正文预览，被擦掉是正确的。
    关闭正文流式只影响正文，改为结尾一次性给出，思考与工具活动照旧实时持久。
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
        """在第一个持久行之前打一次 ● SAYA；没有任何内容就什么都不打。"""
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

        只在段落边界落盘（工具调用开始 / 本轮结束），这样每个 Markdown 块都是完整的。
        stream_text=False 时中途不落盘，结尾由 force=True 一次性给出。
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

    # 两个参数都传，缺一不可。
    # 位置参数决定首帧绘制，只给刷新函数首帧不画。
    # 刷新函数每次重绘都会调用，已耗时会自己走。
    # 用预构建对象更新会把时间冻在构造那一刻，
    # 而需要看时间恰恰是收不到任何东西的时候。
    #
    # 循环里的 console.print 是安全的：rich 的 Live 以 render hook 的形式接在
    # Console.print 上，每次打印都会先擦掉 Live 区域、写完内容再把 Live 区域重画到
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
    """打印欢迎卡片。"""
    print_summary_card(
        tr("welcome.title"),
        {"Role": tr("welcome.role"), "Commands": tr("welcome.commands"), "Start": tr("welcome.start")},
        subtitle=tr("welcome.subtitle"), footer=tr("welcome.footer"))
    console.print()


def print_farewell() -> None:
    """打印会话结束语。"""
    console.print()
    console.print(_safe_text(tr("session.ended"), style=SayacodeColors.TEXT_DIM))
    console.print()


def print_feature_guide(startup: bool = False) -> None:
    """打印功能引导表。"""
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
    """打印工具调用摘要。"""
    console.print(_assemble(("  -> ", SayacodeColors.TEXT_DIM), (tool_name, "")))
    if args:
        for k, v in list(args.items())[:3]:
            console.print(_assemble((f"     {k}: ", SayacodeColors.TEXT_DIM), (str(v), "")))


def print_thinking(message: str = "") -> None:
    """打印思考中提示。"""
    console.print(_assemble(("[*] ", SayacodeColors.WARNING),
                            (message or tr("thinking"), "")))


# ═══════════════════════════════════════════════════════════════════════════════
# 状态信息
# ═══════════════════════════════════════════════════════════════════════════════

def print_status_info(workspace: str, model: str, mcp_servers: int = 0,
                      stream_output: Optional[bool] = None) -> None:
    """打印工作区与模型状态概览。"""
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
    """打印命令帮助总览。"""
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
        (tr("help.category_agent"),     [("/reset", tr("help.reset")), ("/compact", tr("help.compact")), ("/git", tr("help.git")), ("/team", tr("help.team")), ("/trace", tr("help.trace")), ("/plan", tr("help.plan")), ("/rewind", tr("help.rewind"))]),
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
# 计划表与委托通知
# ═══════════════════════════════════════════════════════════════════════════════

def _plan_status_cell(status: str) -> Text:
    """计划任务状态格（含颜色）。"""
    text = str(status or "")
    lowered = text.lower()
    if lowered == "done":
        return Text(text, style=SayacodeColors.SUCCESS)
    if lowered == "doing":
        return Text(text, style=SayacodeColors.WARNING)
    if lowered in ("failed", "error"):
        return Text(text, style=SayacodeColors.ERROR)
    if lowered == "skipped":
        return Text(text, style=SayacodeColors.TEXT_DIM)
    return Text(text, style=SayacodeColors.TEXT)


def _task_field(task: Any, name: str, default: str = "") -> str:
    """兼容 dict 与对象两种任务形状。"""
    if isinstance(task, dict):
        value = task.get(name, default)
        return str(value if value is not None else default)
    return str(getattr(task, name, default) or default)


def _build_plan_table(plan: Any) -> Panel:
    """构建自主计划表（目标 + 轮次 + 任务行）。"""
    goal = str(getattr(plan, "goal", "") or "")
    rounds = getattr(plan, "rounds", 0) or 0
    tasks = getattr(plan, "tasks", None) or []
    table = Table(box=box.SIMPLE_HEAD, border_style=SayacodeColors.BORDER,
                  header_style=f"bold {SayacodeColors.PRIMARY}", expand=True, show_edge=False)
    table.add_column("ID", style=SayacodeColors.TEXT_DIM, no_wrap=True)
    table.add_column("Title", style=SayacodeColors.TEXT)
    table.add_column("Status", style=SayacodeColors.TEXT, no_wrap=True)
    table.add_column("Result", style=SayacodeColors.TEXT_DIM)
    for task in tasks:
        tid = _task_field(task, "id")
        title = _task_field(task, "title")
        status = _task_field(task, "status", "todo")
        result = _task_field(task, "result")
        if len(result) > 120:
            result = result[:117] + "..."
        table.add_row(tid, _safe_text(title), _plan_status_cell(status), _safe_text(result))
    title = _assemble((goal or "plan", f"bold {SayacodeColors.PRIMARY}"),
                      (f"  rounds:{rounds}", SayacodeColors.TEXT_DIM))
    return Panel(table, title=title,
                 border_style=SayacodeColors.BORDER_BRIGHT, box=box.ROUNDED)


def print_plan_table(plan: Any) -> None:
    """打印自主计划表。"""
    console.print(_build_plan_table(plan))


def print_delegate_notice(handle: str, completed: bool, preview: str) -> None:
    """打印后台委托完成通知（轮后 drain 用，一次一行）。"""
    icon = "✓" if completed else "·"
    style = SayacodeColors.SUCCESS if completed else SayacodeColors.INFO
    preview_text = str(preview or "")
    if len(preview_text) > 500:
        preview_text = preview_text[:497] + "..."
    console.print(_line(icon, style, f"{handle}: {preview_text}"))


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
    '_plan_status_cell', '_build_plan_table', 'print_plan_table',
    'print_delegate_notice',
]
