from rich.console import Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text

from lib.theme import (
    SayacodeColors,
    _build_agent_message,
    _build_summary_panel,
    _build_user_message,
    _format_tool_log_line,
    _parse_tool_stream_message,
    _shorten_tool_preview,
    agent_status_text,
)


def test_session_borders_use_soft_pink_theme():
    panel = _build_summary_panel("Session", {"Model": "test"})

    assert SayacodeColors.SESSION_BORDER == "#FFDDE8"
    assert SayacodeColors.BORDER == SayacodeColors.SESSION_BORDER
    assert SayacodeColors.BORDER_BRIGHT == SayacodeColors.SESSION_BORDER
    assert str(panel.border_style) == SayacodeColors.SESSION_BORDER


def test_user_input_border_stays_separate_from_session_border():
    assert SayacodeColors.USER_INPUT_BORDER == "#FFFFFF"
    assert SayacodeColors.USER_INPUT_BORDER != SayacodeColors.SESSION_BORDER


def test_conversation_messages_use_lightweight_groups():
    user_message = _build_user_message("[red]literal[/]\nsecond line")
    agent_message = _build_agent_message("Hello\n```py\nprint(1)\n```")

    assert isinstance(user_message, Group)
    assert isinstance(agent_message, Group)
    assert not isinstance(user_message, Panel)
    assert not isinstance(agent_message, Panel)

    user_parts = list(user_message.renderables)
    assert isinstance(user_parts[1], Text)
    assert "[red]literal[/]" in user_parts[1].plain
    assert "second line" in user_parts[1].plain

    agent_parts = list(agent_message.renderables)
    assert any(isinstance(part, Padding) for part in agent_parts)
    markdown_body = next(part for part in agent_parts if isinstance(part, Padding)).renderable
    assert isinstance(markdown_body, Markdown)


def test_tool_stream_messages_parse_and_render_short_status_lines():
    text, event = _parse_tool_stream_message("[调用工具: shell_command]")
    assert text == ""
    assert event == {"kind": "start", "name": "shell_command"}

    text, event = _parse_tool_stream_message("[工具结果: shell_command | ok\nnext]")
    assert text == ""
    assert event == {"kind": "result", "name": "shell_command", "preview": "ok\nnext"}

    rendered = _format_tool_log_line({"name": "shell_command", "status": "done", "preview": "ok\nnext"})
    assert "shell_command" in rendered.plain
    assert "ok next" in rendered.plain


def test_tool_preview_is_collapsed_and_truncated():
    preview = _shorten_tool_preview("a\nb\t" + ("c" * 120), max_chars=20)

    assert "\n" not in preview
    assert "\t" not in preview
    assert preview.endswith("...")
    assert len(preview) == 20


def test_agent_status_text_uses_saya_header():
    status = agent_status_text("Thinking...")

    assert "SAYA" in status.plain
    assert "Thinking..." in status.plain
