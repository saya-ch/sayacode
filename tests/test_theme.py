from rich.console import Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text

import lib.theme as theme
from lib.theme import (
    SayacodeColors,
    _build_agent_message,
    _build_summary_panel,
    _build_user_message,
    _format_tool_log_line,
    _parse_tool_stream_message,
    _recent_tool_log,
    _shorten_tool_preview,
    _summarize_tool_log,
    agent_status_text,
)
from lib.agent import SAIAgent


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
    preview = _shorten_tool_preview("\U0001f4c4 a\nb\t\u26a0\ufe0f " + ("c" * 120), max_chars=20)

    assert "\n" not in preview
    assert "\t" not in preview
    assert "\U0001f4c4" not in preview
    assert "\u26a0" not in preview
    assert preview.endswith("...")
    assert len(preview) == 20


def test_agent_status_text_uses_saya_header():
    status = agent_status_text("Thinking...")

    assert "SAYA" in status.plain
    assert "Thinking..." in status.plain


def test_streaming_status_is_transient_and_prints_one_final_message(monkeypatch):
    live_calls = {}
    final_prints = []

    class FakeLive:
        def __init__(self, renderable, *, console, refresh_per_second, transient):
            live_calls["initial"] = renderable
            live_calls["console"] = console
            live_calls["refresh_per_second"] = refresh_per_second
            live_calls["transient"] = transient
            live_calls["updates"] = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def update(self, renderable, refresh=False):
            live_calls["updates"].append((renderable, refresh))

    monkeypatch.setattr(theme, "Live", FakeLive)
    monkeypatch.setattr(theme.console, "print", lambda renderable: final_prints.append(renderable))

    response = theme.render_streaming_agent_message(
        ["[调用工具: shell_command]", "[工具结果: shell_command | ok]", "Done."]
    )

    assert response == "Done."
    assert live_calls["transient"] is True
    assert len(live_calls["updates"]) == 3
    assert len(final_prints) == 1


def test_tool_log_is_bounded_and_final_summary_is_compact():
    entries = [
        {"name": "read_file", "status": "done", "preview": f"file {idx}"}
        for idx in range(8)
    ]
    entries.append({"name": "grep_search", "status": "error", "preview": "\U0001f50d failed"})

    recent = _recent_tool_log(entries)
    summary = _summarize_tool_log(entries)

    assert len(recent) == 6
    assert recent[0]["preview"] == "file 3"
    assert "read_file x8" in summary.plain
    assert "grep_search" in summary.plain
    assert "failed" in summary.plain
    assert "\U0001f50d" not in summary.plain


def test_tool_call_label_uses_ascii_counts():
    assert SAIAgent._format_tool_call_label(["read_file", "read_file", "grep_search"]) == "read_file x2, grep_search"


# ── 思考链 ────────────────────────────────────────────────────────────────────
#
# 真机背景：一次回答里 47 个 chunk 带推理、只有 7 个带正文。只渲染正文的话，用户在整个
# 模型调用期间只看到一个「思考中…」——实测有一次等了 6 分钟无法判断是卡死还是在跑。


def test_reasoning_marker_parses_as_its_own_event():
    text, event = _parse_tool_stream_message("[思考: 先看目录结构]")

    assert text == ""
    assert event == {"kind": "reasoning", "name": "先看目录结构"}


def test_reasoning_marker_keeps_brackets_inside():
    """推理文本里带 ``]`` 也不能把事件截断。"""
    text, event = _parse_tool_stream_message("[思考: 检查 a[0] 与 b[1]]")

    assert text == ""
    assert event["kind"] == "reasoning"
    assert event["name"] == "检查 a[0] 与 b[1]"


def _render_to_text(renderable) -> str:
    """把任意 rich renderable 渲染成纯文本（Group / Padding / Markdown 都覆盖）。"""
    import io

    from rich.console import Console as _Console

    recorder = _Console(width=200, record=True, file=io.StringIO())
    recorder.print(renderable)
    return recorder.export_text()


def test_streaming_renders_reasoning_then_summarises_it(monkeypatch):
    """未出正文时展示思考链；出正文后折叠成一行摘要。"""
    from lib.i18n import get_language_preference, set_language

    snapshots = []

    class FakeLive:
        def __init__(self, renderable, **_kwargs):
            snapshots.append(renderable)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def update(self, renderable, refresh=False):
            snapshots.append(renderable)

    monkeypatch.setattr(theme, "Live", FakeLive)
    monkeypatch.setattr(theme.console, "print", lambda renderable: snapshots.append(renderable))

    # 文案随语言变化；显式固定，避免依赖运行环境的系统语言。
    previous = get_language_preference()
    set_language("zh")
    try:
        theme.render_streaming_agent_message(
            ["[思考: 先确认路径]", "[思考: 再读文件]", "答案是 42。"]
        )
    finally:
        set_language(previous)

    live_text = " ".join(_render_to_text(snapshot) for snapshot in snapshots)

    assert "先确认路径" in live_text, "思考链内容必须在流中出现"
    assert "答案是 42。" in live_text, "正文必须出现"
    assert "已思考" in live_text, "结尾应把思考链折叠成摘要"



def test_stream_text_off_hides_body_until_the_end(monkeypatch):
    """关掉流式输出时，活动照旧实时可见，但正文只在结尾出现一次。"""
    snapshots = []

    class FakeLive:
        def __init__(self, renderable, **_kwargs):
            snapshots.append(("live", renderable))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def update(self, renderable, refresh=False):
            snapshots.append(("live", renderable))

    monkeypatch.setattr(theme, "Live", FakeLive)
    monkeypatch.setattr(theme.console, "print", lambda r: snapshots.append(("final", r)))

    theme.render_streaming_agent_message(
        ["[调用工具: grep_search]", "最终答案在这里。"],
        stream_text=False,
    )

    live = [_render_to_text(r) for kind, r in snapshots if kind == "live"]
    final = [_render_to_text(r) for kind, r in snapshots if kind == "final"]

    assert not any("最终答案在这里。" in text for text in live), "流中不应渲染正文"
    assert len(final) == 1
    assert "最终答案在这里。" in final[0]
    assert any("grep_search" in text for text in live), "工具活动必须实时可见"


