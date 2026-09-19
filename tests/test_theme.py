from rich.console import Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text

import lib.cli.theme as theme
from lib.cli.theme import (
    SayacodeColors,
    _build_agent_message,
    _build_summary_panel,
    _build_user_message,
    _format_tool_log_line,
    _parse_tool_stream_message,
    _shorten_tool_preview,
    agent_status_text,
)
from lib.agent.stream import AgentStreamExtractor


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
    from lib.runtime.events import StreamEvent

    text, event = _parse_tool_stream_message(StreamEvent.tool_start("shell_command"))
    assert text == "[调用工具: shell_command]"
    assert event == {"kind": "start", "name": "shell_command"}

    text, event = _parse_tool_stream_message(StreamEvent.tool_result("shell_command", "ok\nnext"))
    assert text == "[工具结果: shell_command | ok\nnext]"
    assert event == {"kind": "result", "name": "shell_command", "preview": "ok\nnext"}

    rendered = _format_tool_log_line({"name": "shell_command", "status": "done", "preview": "ok\nnext"})
    assert "shell_command" in rendered.plain
    assert "ok next" in rendered.plain


def test_stream_event_parses_directly():
    """StreamEvent 直接传入也走同一路径（新协议）。"""
    from lib.runtime.events import StreamEvent

    text, event = _parse_tool_stream_message(StreamEvent.reasoning("先看目录"))
    assert text == "[思考: 先看目录]"
    assert event == {"kind": "reasoning", "name": "先看目录"}

    text, event = _parse_tool_stream_message(StreamEvent.tool_start("grep_search"))
    assert text == "[调用工具: grep_search]"
    assert event == {"kind": "start", "name": "grep_search"}

    text, event = _parse_tool_stream_message(StreamEvent.tool_result("grep_search", "3 matches", "c1"))
    assert text == "[工具结果: grep_search | 3 matches]"
    assert event == {"kind": "result", "name": "grep_search", "preview": "3 matches"}

    text, event = _parse_tool_stream_message(StreamEvent.tool_error("grep_search", "failed", "c1"))
    assert text == "[工具执行出错: grep_search | failed]"
    assert event == {"kind": "error", "name": "grep_search", "preview": "failed"}


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


def test_streaming_prints_every_event_once_in_chronological_order(monkeypatch):
    """思考、工具、正文都**持久**落到滚动区，落盘顺序就是发生顺序。

    回归保护：此前所有内容都塞在一个 ``transient=True`` 的 Live 区域里，退出时被终端
    整块擦掉，只在屏幕上留下一行折叠摘要 —— 用户既看不到思考过程，也回看不了这一轮
    到底调过哪些工具。
    """
    prints = []
    live_calls = {}

    class FakeLive:
        def __init__(self, renderable, *, get_renderable, console, refresh_per_second, transient):
            live_calls["console"] = console
            live_calls["refresh_per_second"] = refresh_per_second
            live_calls["transient"] = transient
            live_calls["refreshes"] = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        # 重绘走 refresh()：渲染函数在**重绘时**才求值，耗时才能自己走。
        # 用 update(预构建的 Group) 会把时间冻在构造那一刻。
        def refresh(self):
            live_calls["refreshes"] += 1

    monkeypatch.setattr(theme, "Live", FakeLive)
    monkeypatch.setattr(theme.console, "print", lambda renderable: prints.append(_render_to_text(renderable)))

    from lib.i18n import get_language_preference, set_language
    from lib.runtime.events import StreamEvent

    # 工具状态文案随语言变化；显式固定，避免依赖运行环境的系统语言。
    previous = get_language_preference()
    set_language("zh")
    try:
        response = theme.render_streaming_agent_message(
            [
                StreamEvent.reasoning("先看目录"),
                StreamEvent.tool_start("shell_command"),
                StreamEvent.tool_result("shell_command", "ok"),
                "Done.",
            ]
        )
    finally:
        set_language(previous)

    assert response == "Done."
    assert live_calls["transient"] is True
    # 思考 chunk 不重绘 Live：它落的是持久区，Live 区域的内容没变。
    # 真正需要重绘的是正文预览和工具行（工具行本身在持久区，同样靠 print 触发重绘）。
    assert live_calls["refreshes"] == 3, "每个改变屏幕内容的 chunk 重绘一次"

    logged = "\n".join(prints)
    assert logged.index("先看目录") < logged.index("shell_command"), "思考必须先于它之后的工具落盘"
    assert logged.index("运行中") < logged.index("ok"), "工具开始先于工具结果"
    assert logged.index("ok") < logged.index("Done."), "正文必须在工具之后落盘"
    assert logged.count("Done.") == 1, "正文只落盘一次"


def test_elapsed_keeps_ticking_while_nothing_arrives(monkeypatch):
    """停摆期间状态行必须继续走 —— 恰恰是最需要它的时候。

    回归保护：此前 Live 的是**预构建**的 Group，耗时在构造时就固定了，只有收到
    新 chunk 才重算。实测一次网关停摆（栈停在 httpcore 的
    ``_receive_response_headers``）期间，状态行一直不显示耗时，
    用户无法判断是卡死还是在跑。
    """
    from lib.i18n import get_language_preference, set_language

    captured = {}

    class FakeLive:
        def __init__(self, renderable, *, get_renderable, **_kwargs):
            captured["get_renderable"] = get_renderable

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def refresh(self):
            pass

    monkeypatch.setattr(theme, "Live", FakeLive)
    monkeypatch.setattr(theme.console, "print", lambda renderable: None)

    clock = {"now": 1000.0}
    monkeypatch.setattr(theme.time, "monotonic", lambda: clock["now"])

    previous = get_language_preference()
    set_language("zh")
    try:
        # 只有一个 chunk：之后不再有任何数据到达
        theme.render_streaming_agent_message(["[思考: 先看看]"])
        first = _render_to_text(captured["get_renderable"]())

        clock["now"] = 1065.0  # 65 秒后，仍然没有任何新 chunk
        later = _render_to_text(captured["get_renderable"]())
    finally:
        set_language(previous)

    assert "思考中" in first
    assert "1m05s" in later, f"停摆期间耗时必须继续走，实际渲染：{later!r}"


def test_tool_call_label_uses_ascii_counts():
    assert AgentStreamExtractor.format_tool_call_label(["read_file", "read_file", "grep_search"]) == "read_file x2, grep_search"


# ── 思考链 ────────────────────────────────────────────────────────────────────
#
# 真机背景：一次回答里 47 个 chunk 带推理、只有 7 个带正文。只渲染正文的话，用户在整个
# 模型调用期间只看到一个「思考中…」——实测有一次等了 6 分钟无法判断是卡死还是在跑。


def test_reasoning_marker_parses_as_its_own_event():
    from lib.runtime.events import StreamEvent

    text, event = _parse_tool_stream_message(StreamEvent.reasoning("先看目录结构"))
    assert text == "[思考: 先看目录结构]"
    assert event == {"kind": "reasoning", "name": "先看目录结构"}


def test_reasoning_marker_keeps_brackets_inside():
    """推理文本里带 ``]`` 也不能把事件截断。"""
    from lib.runtime.events import StreamEvent

    text, event = _parse_tool_stream_message(StreamEvent.reasoning("检查 a[0] 与 b[1]"))

    assert text == "[思考: 检查 a[0] 与 b[1]]"
    assert event["kind"] == "reasoning"
    assert event["name"] == "检查 a[0] 与 b[1]"


def _render_to_text(renderable) -> str:
    """把任意 rich renderable 渲染成纯文本（Group / Padding / Markdown 都覆盖）。"""
    import io

    from rich.console import Console as _Console

    recorder = _Console(width=200, record=True, file=io.StringIO())
    recorder.print(renderable)
    return recorder.export_text()


def test_reasoning_is_persisted_before_the_body(monkeypatch):
    """思考链落盘成持久段落，排在正文之前，而且**不再**折叠成一行摘要。

    折叠摘要等于把思考过程从屏幕上删掉；这里要的正是「回看时还在」。
    """
    from lib.i18n import get_language_preference, set_language

    prints = []

    class FakeLive:
        def __init__(self, renderable, *, get_renderable, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def refresh(self):
            pass

    monkeypatch.setattr(theme, "Live", FakeLive)
    monkeypatch.setattr(theme.console, "print", lambda renderable: prints.append(_render_to_text(renderable)))

    # 文案随语言变化；显式固定，避免依赖运行环境的系统语言。
    previous = get_language_preference()
    set_language("zh")
    try:
        from lib.runtime.events import StreamEvent

        response = theme.render_streaming_agent_message(
            [StreamEvent.reasoning("先确认路径"), StreamEvent.reasoning("再读文件"), "答案是 42。"]
        )
    finally:
        set_language(previous)

    assert response == "答案是 42。"

    logged = "\n".join(prints)
    assert "先确认路径" in logged
    assert "再读文件" in logged
    assert "思考" in logged, "思考段落要带标签"
    assert "答案是 42。" in logged
    assert logged.index("再读文件") < logged.index("答案是 42。"), "思考先于正文落盘"
    assert "已思考" not in logged, "思考过程本身必须留在屏幕上，不能折叠成摘要"


def test_reasoning_is_flushed_incrementally_before_the_stream_ends(monkeypatch):
    """长思考不能等到结尾才落盘 —— 那又变成「盯着一个思考中干等」。"""
    prints = []

    class FakeLive:
        def __init__(self, renderable, *, get_renderable, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def refresh(self):
            pass

    monkeypatch.setattr(theme, "Live", FakeLive)
    monkeypatch.setattr(theme.console, "print", lambda renderable: prints.append(_render_to_text(renderable)))

    # 每个 chunk 都是独立的一次推理增量；单个 chunk 就超过阈值时就该先落一段盘
    snapshots: list[str] = []

    def _chunks():
        for idx in range(4):
            from lib.runtime.events import StreamEvent

            yield StreamEvent.reasoning(f"{('推理' * 200)}{idx}")
            # 生成器在两次 yield 之间被恢复，此时正好能看见「这个 chunk 处理完之后」的屏幕内容
            snapshots.append("\n".join(prints))

    theme.render_streaming_agent_message(_chunks())

    assert "推理" in snapshots[0], "超过阈值的思考必须当段落盘，而不是等流结束"
    assert snapshots[0].count("推理") < snapshots[-1].count("推理"), "后续增量继续落盘"



def test_stream_text_off_defers_body_but_keeps_activity_persistent(monkeypatch):
    """关掉流式输出只影响正文：活动照旧实时**持久**落盘，正文结尾一次性给出。"""
    prints = []
    frames = []

    class FakeLive:
        def __init__(self, renderable, *, get_renderable, **_kwargs):
            self._get_renderable = get_renderable
            frames.append(get_renderable())

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def refresh(self):
            frames.append(self._get_renderable())

    monkeypatch.setattr(theme, "Live", FakeLive)
    monkeypatch.setattr(theme.console, "print", lambda r: prints.append(_render_to_text(r)))

    theme.render_streaming_agent_message(
        ["[调用工具: grep_search]", "最终答案在这里。"],
        stream_text=False,
    )

    live = [_render_to_text(frame) for frame in frames]
    body_prints = [idx for idx, text in enumerate(prints) if "最终答案在这里。" in text]

    assert not any("最终答案在这里。" in text for text in live), "流中不应预览正文"
    assert body_prints == [len(prints) - 1], "正文只在结尾落盘一次，且是最后一条"
    assert any("grep_search" in text for text in prints), "工具活动必须实时且持久可见"


