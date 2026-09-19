# theme 全覆盖：打印函数扫一遍 + 纯函数分支。


import lib.cli.theme as th
from lib.runtime.events import StreamEvent


class TestPure:
    def test_assemble(self):
        assert th._assemble("plain", ("x", "bold")).plain == "plainx"

    def test_logo_reset(self):
        th.reset_logo_state()
        th.print_logo(show_full=False)
        th.print_logo(show_full=True)
        th.print_logo(show_full=False)

    def test_shorten(self):
        assert th._shorten_value("abc", 10) == "abc"
        assert "..." in th._shorten_value("x" * 100)

    def test_ctx_label(self):
        assert th._ctx_label(0) == ""
        assert th._ctx_label(-1) == ""
        assert "90%" in th._ctx_label(0.9)
        assert "70%" in th._ctx_label(0.7)
        assert "30%" in th._ctx_label(0.3)

    def test_short_prompt(self):
        assert th.short_prompt() is not None
        assert th.short_prompt("ws", 0.9) is not None
        assert th.short_prompt("ws", 0.7) is not None
        assert th.short_prompt("ws", 0.3) is not None
        assert th.short_prompt("a-very-long-workspace-name-here", None) is not None

    def test_token_hint(self):
        assert th.format_token_hint(0) == ""
        assert th.format_token_hint(-5) == ""
        assert th.format_token_hint(500) == "500t"
        assert th.format_token_hint(2500) == "2.5kt"

    def test_plan_status(self):
        for s in ("done", "doing", "failed", "skipped", "todo", "other"):
            assert th._plan_status_cell(s) is not None

    def test_plan_table_shapes(self):
        from types import SimpleNamespace

        plan = SimpleNamespace(goal="g", rounds=1, tasks=[{"id": "1", "title": "t", "status": "done", "result": "r"},
                                                          SimpleNamespace(id="2", title="t2", status="doing", result="")])
        th.print_plan_table(plan)
        th.print_plan_table(SimpleNamespace(goal="g", rounds=0, tasks=None))

    def test_elapsed(self):
        assert th._format_elapsed(5) == "5s"
        assert th._format_elapsed(65) == "1m05s"
        assert th._format_elapsed(3720) == "1h02m"

    def test_tool_preview(self):
        assert th._shorten_tool_preview("x" * 300, 10).endswith("...")
        assert th._shorten_tool_preview("short") == "short"
        assert "\U0001F600" not in th._sanitize_tool_preview("emoji \U0001F600 test  double")

    def test_parse_stream(self):
        text, ev = th._parse_tool_stream_message(StreamEvent.reasoning("r"))
        assert ev["kind"] == "reasoning"
        text, ev = th._parse_tool_stream_message("raw")
        assert (text, ev) == ("raw", None)
        text, ev = th._parse_tool_stream_message(123)
        assert ev is None

    def test_event_dict(self):
        assert th._stream_event_to_dict(StreamEvent.tool_start("t"))["kind"] == "start"
        assert th._stream_event_to_dict(StreamEvent.tool_result("t", "p"))["kind"] == "result"
        assert th._stream_event_to_dict(StreamEvent.tool_error("t", "p"))["kind"] == "error"
        assert th._stream_event_to_dict(StreamEvent.text_delta("x"))["kind"] == "text"

    def test_indicator(self):
        assert th._tool_indicator({"status": "running", "name": "t"}) is not None
        assert th._tool_indicator({"status": "switching", "preview": "p"}) is not None
        assert th._tool_indicator({"status": "error", "name": "t", "preview": "p"}) is not None
        assert th._tool_indicator({"status": "error", "name": "t"}) is not None
        assert th._tool_indicator({"status": "done", "name": "t", "preview": "p"}) is not None
        assert th._tool_indicator({"status": "done", "name": "t"}) is not None
        assert th._tool_indicator({}) is not None

    def test_status_line(self):
        assert th._build_work_status_line("thinking", "msg", elapsed=5) is not None
        assert th._build_work_status_line("thinking") is not None

    def test_reasoning_flush(self):
        assert th._take_reasoning_flush("   ", force=False) == ("", "")
        assert th._take_reasoning_flush("short", force=False) == ("", "short")
        before, after = th._take_reasoning_flush("x" * 200 + "\n" + "y" * 200, force=False)
        assert before.endswith("x" * 200) and after.startswith("\ny")
        assert th._take_reasoning_flush("a" * 5000, force=False)[0] != ""
        assert th._take_reasoning_flush("line1\nline2\n" + "x" * 5000, force=False)[0] != ""
        assert th._take_reasoning_flush("buf", force=True) == ("buf", "")

    def test_reasoning_paragraph(self):
        th._print_reasoning_paragraph("   \n  ", label=True)
        th._print_reasoning_paragraph("think text", label=True)
        th._print_reasoning_paragraph("think text", label=False)

    def test_tool_log_line(self):
        assert th._format_tool_log_line({"name": "t", "status": "running"}) is not None
        assert th._format_tool_log_line({"name": "t", "status": "error", "preview": "p"}) is not None
        assert th._format_tool_log_line({"name": "t", "status": "error"}) is not None
        assert th._format_tool_log_line({"name": "t", "status": "done", "preview": "p"}) is not None
        assert th._format_tool_log_line({"name": "t", "status": "done"}) is not None

    def test_clip(self):
        assert th._clip_response_for_live("a\nb") == "a\nb"
        assert th._clip_response_for_live("\n".join(str(i) for i in range(100))).startswith("...")

    def test_spinner_modes(self):
        from lib.cli.theme import SpinnerMode

        assert SpinnerMode.is_valid("thinking") is True
        assert SpinnerMode.is_valid("ghost") is False
        assert len(SpinnerMode.all_modes()) > 0


class TestPrint:
    def test_cards(self):
        th.print_summary_card("t", {"k": "v", "e": ""}, subtitle="s", footer="f")
        th.print_split_summary_cards("l", {"k": "v"}, "r", {"k": "v"})
        th.print_message_header("agent", "red", meta="m")
        th.print_message_header("agent", "red")
        th.print_status("s")
        th.print_success("s")
        th.print_warning("w")
        th.print_error("e")
        th.print_info("i")
        th.print_divider()
        th.print_banner("t")
        th.print_banner("t", "sub")
        th.print_user_message("hello")
        th.print_agent_message("hi", show_header=True)
        th.print_agent_message("", show_header=False)
        th.print_delegate_notice("h", True, "p")
        th.print_delegate_notice("h", False, "p")
        th.print_welcome()
        th.print_farewell()
        th.print_feature_guide(startup=True)
        th.print_feature_guide(startup=False)
        th.print_tool_call("t", {"a": 1, "b": 2, "c": 3, "d": 4})
        th.print_tool_call("t", {})
        th.print_thinking()
        th.print_thinking("custom")

    def test_confirm(self, monkeypatch):
        import lib.cli.theme as _theme

        monkeypatch.setattr(_theme.Confirm, "ask", lambda *a, **k: True)
        assert th.confirm_action("go?") is True

    def test_agent_message_variants(self):
        th._build_agent_message("", show_header=True)
        th._build_agent_message("content", show_header=True, streaming=True)
        th._build_agent_message("content", show_header=True, tool_states=[{"status": "done", "name": "t"}])
        th._build_agent_message("", show_header=False, loading_message="wait")
        th._build_agent_message("", show_header=False)
        assert th._compact_markdown("") == ""
        assert "```" in th._compact_markdown("a\n\n\n\nb\n```code```")

    def test_status_info(self):
        th.print_status_info(workspace="w", model="m", mcp_servers=2, stream_output=True)


class TestRender:
    def test_text_stream(self):
        out = th.render_streaming_agent_message(iter(["hello ", "world", "", StreamEvent.text_delta("!")]))
        assert "hello" in out and "world" in out

    def test_reasoning_stream(self):
        out = th.render_streaming_agent_message(iter([StreamEvent.reasoning("think" * 200), StreamEvent.text_delta("done")]), stream_text=False)
        assert isinstance(out, str)

    def test_tool_stream(self):
        out = th.render_streaming_agent_message(iter([
            StreamEvent.tool_start("echo_tool"),
            StreamEvent.tool_result("echo_tool", "preview"),
            StreamEvent.tool_error("bad_tool", "boom"),
            "tail",
        ]))
        assert "tail" in out

    def test_text_then_tool_no_stream(self):
        out = th.render_streaming_agent_message(iter([
            "buffered",
            StreamEvent.tool_start("t"),
            StreamEvent.tool_result("t", "p"),
        ]), stream_text=False)
        assert isinstance(out, str)

    def test_print_help(self):
        th.print_help()

    def test_empty_stream(self):
        assert th.render_streaming_agent_message(iter([])) == ""

    def test_agent_status(self):
        assert th.agent_status_text("hi") is not None
        assert th._saya_prefix() != ""
        assert th._agent_header() is not None
