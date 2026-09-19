# agent.py 第一波：纯函数与壳调用（无 runner）。

from types import SimpleNamespace

import pytest

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from lib.agent import SAIAgent
from lib.agent.stream import AgentStreamExtractor
from lib.agent import recovery as agent_recovery
from lib.agent import usage as agent_usage
from lib.agent import loop as agent_loop


def _shell(**attrs):
    # 裸壳固件：只挂调用所需的属性。
    agent = SAIAgent.__new__(SAIAgent)
    agent._last_extra = {}
    for key, value in attrs.items():
        setattr(agent, key, value)
    return agent


class TestPureHelpers:
    def test_classify(self):
        from lib.agent.recovery import classify_error as _classify_error

        assert _classify_error("context_length_exceeded blah") == "prompt_too_long"
        assert _classify_error("max tokens reached") == "max_output_tokens"
        assert _classify_error("connection reset boom") == "recoverable"
        assert _classify_error("invalid parameter xyz") == "fatal"
        assert _classify_error("weird unique xyz") == "fatal"

    def test_retry_delay(self):
        from lib.agent.recovery import retry_delay as _retry_delay

        assert _retry_delay(1) <= _retry_delay(3)
        assert _retry_delay(0) >= 0

    def test_format_error(self):
        from lib.agent.recovery import format_execution_error as _format_execution_error

        out = _format_execution_error("boom", {"path": "retry_backoff", "attempt": 2})
        assert "boom" in out

    def test_safe_token_count(self):
        from lib.agent.usage import safe_token_count as _safe_token_count

        assert _safe_token_count(5) == 5
        assert _safe_token_count("7") == 7
        assert _safe_token_count(None) == 0
        assert _safe_token_count("oops") == 0
        assert _safe_token_count(-3) == 0

    def test_tool_label(self):
        assert AgentStreamExtractor.format_tool_call_label(["a"]) == "a"
        assert AgentStreamExtractor.format_tool_call_label(["a", "a", "b"]) == "a x2, b"

    def test_coerce_delta(self):
        assert agent_loop.coerce_stream_delta("", "x") == ""
        assert agent_loop.coerce_stream_delta("hello world", "hello ") == "world"
        assert agent_loop.coerce_stream_delta("new", "old") == "new"
        assert agent_loop.coerce_stream_delta("x", "") == "x"

    def test_split_mode(self):
        assert AgentStreamExtractor.split_mode_event(("messages", "p")) == ("messages", "p")
        assert AgentStreamExtractor.split_mode_event(("nope", "p")) == (None, ("nope", "p"))
        assert AgentStreamExtractor.split_mode_event("x") == (None, "x")
        assert AgentStreamExtractor.split_mode_event(("a", "b", "c")) == (None, ("a", "b", "c"))

    def test_detect_interrupt(self):
        assert agent_recovery.detect_interrupt(("updates", {"__interrupt__": ["a"]})) == ["a"]
        assert agent_recovery.detect_interrupt(("updates", {"__interrupt__": "a"})) == ["a"]
        assert agent_recovery.detect_interrupt(("messages", {"__interrupt__": ["a"]})) == ["a"]
        assert agent_recovery.detect_interrupt({"x": 1}) is None
        assert agent_recovery.detect_interrupt(("updates", {})) is None


class TestExtractShell:
    def test_token_event_shapes(self):
        a = AgentStreamExtractor()
        assert a.extract_token_event(ToolMessage(content="x", tool_call_id="1")).text == ""
        assert a.extract_token_event(HumanMessage(content="hi")).text == ""
        assert a.extract_token_event(SystemMessage(content="s")).text == ""

    def test_token_reasoning(self):
        a = AgentStreamExtractor()
        msg = AIMessage(content="", additional_kwargs={"reasoning_content": "think"})
        assert a.extract_token_event(msg).kind == "reasoning"

    def test_token_text(self):
        a = AgentStreamExtractor()
        assert a.extract_token_event(AIMessage(content="hi")).kind == "text"
        assert a.extract_token_event(AIMessage(content="")).text == ""
        ev = a.extract_token_event(AIMessage(content="hi"))
        assert (ev.text, ev.kind == "tool_start") == ("hi", False)

    def test_stream_delta_modes(self):
        a = AgentStreamExtractor()
        ev = a.extract_stream_delta(("messages", AIMessage(content="hi")))
        assert ev is not None and a.tokens_seen is True
        ev = a.extract_stream_delta(("updates", {"agent": {"messages": [AIMessage(content="x", tool_calls=[])]}}))
        assert ev is not None

    def test_stream_delta_dict_paths(self):
        a = AgentStreamExtractor()
        assert a.extract_stream_delta({"agent": {"messages": []}}) is None
        assert a.extract_stream_delta({"agent": {"messages": [HumanMessage(content="q")]}}) is not None
        assert a.extract_stream_delta({"agent": [AIMessage(content="x", tool_calls=[])]}) is not None
        assert a.extract_stream_delta({"tools": {"messages": [ToolMessage(content="ok", tool_call_id="1", name="t")]}}) is not None
        assert a.extract_stream_delta({"tools": [ToolMessage(content="ok", tool_call_id="1", name="t")]}) is not None
        assert a.extract_stream_delta({"messages": [AIMessage(content="x", tool_calls=[])]}) is not None
        assert a.extract_stream_delta({"custom": {"messages": [AIMessage(content="y", tool_calls=[])]}}) is not None
        assert a.extract_stream_delta({"empty": {}}) is None
        assert a.extract_stream_delta((AIMessage(content="z", tool_calls=[]),)) is not None
        assert a.extract_stream_delta(({},)) is None
        _ev = a.extract_stream_delta({"messages": []})
        assert (_ev.text if _ev is not None else "") == ""

    def test_message_event(self):
        a = AgentStreamExtractor()
        assert a.extract_message_event(ToolMessage(content="ok", tool_call_id="1", name="t")).kind == "tool_result"
        assert a.extract_message_event(HumanMessage(content="q")).text == ""
        msg = AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "1", "type": "tool_call"}])
        assert a.extract_message_event(msg).kind == "tool_start"
        a.tokens_seen = True
        assert a.extract_message_event(AIMessage(content="dup")).text == ""
        a.tokens_seen = False
        assert a.extract_message_event(AIMessage(content="live")).kind == "text"
        assert a.extract_message_event(AIMessage(content="")).text == ""
        assert a.extract_message_event("raw").kind == "text"
        assert a.extract_message_event(123).text == ""
        _mev = a.extract_message_event(msg)
        assert _mev.kind == "tool_start" and _mev.tool_name == "t"

    def test_tool_event(self):
        a = AgentStreamExtractor()
        assert a.extract_tool_event(ToolMessage(content="工具执行失败", tool_call_id="1", name="t")).kind == "tool_error"
        assert a.extract_tool_event(ToolMessage(content="❌ bad", tool_call_id="1", name="t")).kind == "tool_error"
        assert a.extract_tool_event(ToolMessage(content="⚠️ warn", tool_call_id="1", name="t")).kind == "tool_error"
        ev = a.extract_tool_event(ToolMessage(content="x" * 300, tool_call_id="1", name="t"))
        assert ev.kind == "tool_result" and "x" * 300 not in ev.display_text
        assert a.extract_tool_event(ToolMessage(content="ok", tool_call_id="1")).display_text != ""
        assert a.extract_tool_event(object()).text == ""
        _tev = a.extract_tool_event(ToolMessage(content="ok", tool_call_id="1", name="t"))
        assert _tev.kind == "tool_result"

    def test_extract_response(self):
        a = _shell()
        assert a._extract_response({"messages": [HumanMessage(content="q"), AIMessage(content="hi", additional_kwargs={"k": 1})]}) == "hi"
        assert a._last_extra == {"k": 1}
        assert a._extract_response({"messages": [HumanMessage(content="q")]}) == ""
        assert a._extract_response("raw") == "raw"
        blocks = [{"type": "text", "text": "p1"}, {"type": "other"}]
        assert a._extract_response({"messages": [AIMessage(content=blocks)]}) == "p1"
        assert a._extract_response({"messages": [AIMessage(content=[{"type": "other"}])]}) == str([{"type": "other"}])
        obj_block = AIMessage.model_construct(content=[SimpleNamespace(type="text", text="p2")])
        assert a._extract_response({"messages": [obj_block]}) == "p2"
        assert a._extract_response({"messages": [AIMessage.model_construct(content=42)]}) == "42"

    def test_require_runner(self):
        a = _shell(runner=None)
        with pytest.raises(RuntimeError):
            a._require_runner()
        a.runner = object()
        assert a._require_runner() is a.runner

    def test_resume_no_runner(self):
        a = _shell(runner=None, interrupt_handler=None)
        with pytest.raises(RuntimeError):
            agent_recovery.resume_after_interrupt(a.runner, [], a.interrupt_handler)

    def test_iter_no_runner(self):
        a = _shell(runner=None)
        assert a._iter_agent_stream([]) is None

    def test_resolve_interrupt(self):
        from lib.core.middleware import INTERRUPT_TOOL_ASK

        a = _shell(interrupt_handler=lambda payload: {"approved": True})
        assert agent_recovery.resolve_interrupt([{"kind": INTERRUPT_TOOL_ASK, "tool": "t"}], a.interrupt_handler) == {"approved": True}
        assert agent_recovery.resolve_interrupt([{"kind": INTERRUPT_TOOL_ASK}, {"kind": INTERRUPT_TOOL_ASK}], a.interrupt_handler) == [{"approved": True}, {"approved": True}]
        assert agent_recovery.resolve_interrupt(["oops"], a.interrupt_handler) is None
        assert agent_recovery.resolve_interrupt(["x", "y"], a.interrupt_handler) == [None, None]
        b = _shell(interrupt_handler=None)
        assert agent_recovery.resolve_interrupt([{"kind": INTERRUPT_TOOL_ASK, "tool": "t"}], b.interrupt_handler) == {"approved": False}

    def test_drain_guard(self):
        a = _shell(runner=None, interrupt_handler=None)
        assert agent_recovery.drain_invoke_interrupts(a.runner, {"messages": []}, a.interrupt_handler) == {"messages": []}
        assert agent_recovery.drain_invoke_interrupts(a.runner, "oops", a.interrupt_handler) == "oops"
        with pytest.raises(RuntimeError, match="未知中断无法恢复"):
            agent_recovery.drain_invoke_interrupts(a.runner, {"__interrupt__": ["oops"]}, a.interrupt_handler)

    def test_record_no_model(self):
        model = SimpleNamespace()
        agent_usage.record_invoke_result(model, {"messages": []})
        agent_usage.record_stream_chunk(model, {})
        agent_usage.estimate_result(model, {"messages": []})

    def test_estimate_usage(self):
        model = SimpleNamespace(recorded=None)
        model._record_usage = lambda u: setattr(model, "recorded", u)
        agent_usage.estimate_result(model, {"messages": [HumanMessage(content="hello world"), AIMessage(content="hi there")]})
        assert model.recorded.total_tokens > 0

    def test_record_usage_paths(self):
        from langchain_core.messages import AIMessage as _AI
        seen = []
        model = SimpleNamespace(_record_usage=lambda u: seen.append(u))
        msg = _AI(content="x", usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7})
        agent_usage.record_invoke_result(model, {"messages": [msg]})
        assert seen[0].total_tokens == 7
        msg2 = _AI(content="x", response_metadata={"token_usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}})
        agent_usage.record_invoke_result(model, {"messages": [msg2]})
        msg3 = _AI(content="x", additional_kwargs={"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}})
        agent_usage.record_invoke_result(model, {"messages": [msg3]})
        assert len(seen) == 3

    def test_stream_usage_paths(self):
        from langchain_core.messages import AIMessage as _AI

        seen = []
        model = SimpleNamespace(_record_usage=lambda u: seen.append(u))
        msg = _AI(content="x", usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3})
        agent_usage.record_stream_chunk(model, {"messages": [msg]})
        agent_usage.record_stream_chunk(model, {"agent": {"messages": [msg]}})
        agent_usage.record_stream_chunk(model, [msg])
        agent_usage.record_stream_chunk(model, {"nested": {"deep": [msg]}})
        assert len(seen) == 4

    def test_stream_usage_response_meta(self):
        seen = []
        model = SimpleNamespace(_record_usage=lambda u: seen.append(u))
        msg = AIMessage(content="x", response_metadata={"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}})
        agent_usage.record_stream_chunk(model, {"messages": [msg]})
        assert seen[0].total_tokens == 3

    def test_stream_usage_object_meta(self):
        seen = []
        model = SimpleNamespace(_record_usage=lambda u: seen.append(u))
        msg = SimpleNamespace(usage_metadata=SimpleNamespace(input_tokens=1, output_tokens=2, total_tokens=3))
        agent_usage.record_stream_chunk(model, {"messages": [msg]})
        assert seen[0].total_tokens == 3
        agent_usage.record_stream_chunk(model, {"messages": [SimpleNamespace(content="x")]})
        assert len(seen) == 1

    def test_record_usage_object_meta(self):
        seen = []
        model = SimpleNamespace(_record_usage=lambda u: seen.append(u))
        msg = SimpleNamespace(usage_metadata=SimpleNamespace(input_tokens=1, output_tokens=2, total_tokens=3))
        agent_usage.record_invoke_result(model, {"messages": [msg]})
        assert seen[0].total_tokens == 3

    def test_normalize_tools(self):
        a = _shell()
        assert a._normalize_tools([None]) == []
        t1 = SimpleNamespace(name="b")
        t2 = SimpleNamespace(name="a")
        out = a._normalize_tools([t1, t2, t1])
        assert sorted(t.name for t in out) == ["a", "b"]
        anon = SimpleNamespace()
        out = a._normalize_tools([anon])
        assert len(out) == 1 and anon.name == "SimpleNamespace"

    def test_load_mcp_disabled(self):
        a = _shell(_mcp_runtime=None, _enable_mcp=False)
        assert a._load_mcp_tools() == []

    def test_load_mcp_crash(self, monkeypatch):
        import lib.core.mcp_runtime as _mcp_mod

        a = _shell(_mcp_runtime=None, _enable_mcp=True, _permissions_runtime=None,
                   _hooks_runtime=None, workspace=".", _mcp_servers=None)
        monkeypatch.setattr(_mcp_mod, "MCPRuntime", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert a._load_mcp_tools() == []

    def test_mcp_list_empty(self):
        assert _shell(_mcp_runtime=None).get_mcp_tool_list() == []
        assert _shell(_mcp_runtime=None).get_mcp_registry() is None

    def test_mcp_list_crash(self):
        rt = SimpleNamespace(status=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert _shell(_mcp_runtime=rt).get_mcp_tool_list() == []
        assert _shell(_mcp_runtime=rt).get_mcp_registry() is None

    def test_close_empty(self):
        _shell(_mcp_runtime=None).close()

    def test_close_runtime(self):
        rt = SimpleNamespace(shutdown=lambda: None)
        a = _shell(_mcp_runtime=rt)
        a.close()
        assert a._mcp_runtime is None
