# agent.py 第二波：真构造、同步、恢复路径、计划、尾方法。

from typing import Any, Optional

import pytest

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from lib.agent import SAIAgent
from lib.core.permissions import PermissionRuntime, SessionPermissionState


class ScriptedModel(BaseChatModel):
    # 按剧本出牌的 fake 模型。
    script: list = []
    calls_made: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages: list[BaseMessage], stop: Optional[list[str]] = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs: Any) -> ChatResult:
        idx = min(self.calls_made, len(self.script) - 1)
        self.calls_made += 1
        return ChatResult(generations=[ChatGeneration(message=self.script[idx])])

    def bind_tools(self, tools, **kwargs):
        return self


@tool
def echo_tool(text: str) -> str:
    """回声工具。"""
    return f"echo:{text}"


def _agent(tmp_path, script, **kw):
    # 隔离权限的真 agent 固件。
    kw.setdefault("permissions", PermissionRuntime(session=SessionPermissionState()))
    kw.setdefault("interrupt_handler", lambda payload: {"approved": True})
    return SAIAgent(model=ScriptedModel(script=list(script)), workspace=tmp_path,
                    tools=[echo_tool], checkpoint_path=str(tmp_path / "ckpt.sqlite3"), **kw)


class TestBuild:
    def test_prompt_style_rebuild(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="hi")])
        assert agent.set_prompt_style("concise") == "concise"
        assert agent.set_agent_mode("plan") == "plan"
        assert agent.set_agent_mode("ghost") == "build"

    def test_reminder_state(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="hi")])
        state = agent._reminder_state()
        assert state["agent_mode"] == "build"

    def test_refresh_prompt(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="hi")])
        assert isinstance(agent._refresh_turn_prompt(), str)
        agent.runner = None
        assert isinstance(agent._refresh_turn_prompt(), str)

    def test_sync_first_turn(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="hi")])
        msgs = agent._sync_turn_state("hello")
        assert any(isinstance(m, HumanMessage) for m in msgs)

    def test_sync_compacted(self, tmp_path, monkeypatch):
        agent = _agent(tmp_path, [AIMessage(content="hi")])
        agent.run("first")
        monkeypatch.setattr(agent.session, "maybe_compact", lambda: True)
        msgs = agent._sync_turn_state("second")
        assert any(isinstance(m, HumanMessage) for m in msgs)

    def test_reset_no_runner(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="hi")])
        agent.runner = None
        agent._reset_graph_state_for_retry("hello")

    def test_reset_with_runner(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="hi")])
        agent._reset_graph_state_for_retry("hello")

    def test_tool_context(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="hi")])
        assert agent._tool_execution_context().workspace == agent.workspace

    def test_force_compact_none(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="hi")])
        agent._force_compact_session()


class TestRunPaths:
    def test_run_ok(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="done")])
        assert agent.run("hello") == "done"
        assert agent.last_turn_state is not None

    def test_run_empty_response(self, tmp_path, monkeypatch):
        agent = _agent(tmp_path, [AIMessage(content="")])
        monkeypatch.setattr(agent, "_invoke_with_messages", lambda messages: "fallback")
        out = agent.run("hello")
        assert isinstance(out, str)

    def test_run_recoverable_then_ok(self, tmp_path, monkeypatch):
        import lib.agent as _amod

        agent = _agent(tmp_path, [AIMessage(content="ok")])
        calls = {"n": 0}
        real_invoke = agent.runner.invoke

        def flaky(messages):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("connection reset")
            return real_invoke(messages)

        monkeypatch.setattr(agent.runner, "invoke", flaky)
        monkeypatch.setattr(_amod.time, "sleep", lambda s: None)
        assert agent.run("hello") == "ok"

    def test_run_fatal(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="x")])
        agent.runner = None
        out = agent.run("hello")
        assert "执行出错" in out

    def test_run_max_retries(self, tmp_path, monkeypatch):
        import lib.agent as _amod

        agent = _agent(tmp_path, [AIMessage(content="x")])
        monkeypatch.setattr(agent.runner, "invoke", lambda messages: (_ for _ in ()).throw(ConnectionError("down")))
        monkeypatch.setattr(_amod.time, "sleep", lambda s: None)
        out = agent.run("hello")
        assert "执行出错" in out

    def test_run_empty_exhausted(self, tmp_path, monkeypatch):
        agent = _agent(tmp_path, [AIMessage(content="x")])
        monkeypatch.setattr(agent.runner, "invoke", lambda messages: {"messages": []})
        out = agent.run("hello")
        assert "耗尽" in out

    def test_run_max_tokens_recovery(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="tailed")])
        calls = {"n": 0}
        real = agent.runner.invoke

        def flaky(messages):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("max_output_tokens hit")
            return real(messages)

        agent.runner.invoke = flaky
        assert agent.run("hello") == "tailed"

    def test_run_prompt_too_long_recovery(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="after-compact")])
        for i in range(12):
            agent.session.add_user_message(f"q{i}")
            agent.session.add_assistant_message(f"a{i}")
        calls = {"n": 0}
        real = agent.runner.invoke

        def flaky(messages):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("context_length_exceeded")
            return real(messages)

        agent.runner.invoke = flaky
        assert agent.run("hello") == "after-compact"

    def test_run_compact_fails(self, tmp_path, monkeypatch):
        agent = _agent(tmp_path, [AIMessage(content="x")])
        for i in range(3):
            agent.session.add_user_message(f"q{i}")
            agent.session.add_assistant_message(f"a{i}")
        monkeypatch.setattr(agent, "_force_compact_session",
                            lambda: (_ for _ in ()).throw(RuntimeError("nope")))
        monkeypatch.setattr(agent.runner, "invoke", lambda messages: (_ for _ in ()).throw(ValueError("context_length_exceeded")))
        out = agent.run("hello")
        assert "执行出错" in out

    def test_invoke_aborted(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="x")])
        agent._abort_controller.abort("stop")
        from lib.tools.context import tool_execution_session
        with tool_execution_session(agent._tool_execution_context()):
            out = agent._invoke_with_messages([])
        assert "中止" in out
        agent._abort_controller.reset()

    def test_drain_unknown_interrupt(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="x")])
        with pytest.raises(RuntimeError, match="未知中断无法恢复"):
            agent._drain_invoke_interrupts({"__interrupt__": ["oops"], "messages": []})

    def test_drain_resume_none(self, tmp_path, monkeypatch):
        agent = _agent(tmp_path, [AIMessage(content="x")])
        monkeypatch.setattr(agent.runner, "invoke_command", lambda answer: None)
        out = agent._drain_invoke_interrupts({"__interrupt__": [{"kind": "tool_ask", "tool": "t"}], "messages": []})
        assert "__interrupt__" in out


class TestStreamPaths:
    def test_stream_ok(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="streamed")])
        assert "".join(agent.stream_run("hello")) == "streamed"

    def test_stream_callback(self, tmp_path):
        seen = []
        agent = _agent(tmp_path, [AIMessage(content="s")], stream_callback=seen.append)
        list(agent.stream_run("hello"))
        assert seen != []

    def test_stream_no_status(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="s")])
        assert "".join(agent.stream_run("hello", emit_tool_status=False)) == "s"

    def test_stream_event_callback(self, tmp_path):
        seen = []
        agent = _agent(tmp_path, [AIMessage(content="s")])
        list(agent.stream_run("hello", event_callback=seen.append))
        assert seen != []

    def test_stream_interrupt_resume(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="after")])
        agent.runner.stream = lambda messages: iter([("updates", {"__interrupt__": [{"kind": "tool_ask", "tool": "t"}]})])
        agent.runner.resume = lambda answer: iter([("messages", (AIMessage(content="after"), {}))])
        assert "".join(agent.stream_run("hello")) == "after"

    def test_stream_error_recoverable(self, tmp_path, monkeypatch):
        import lib.agent as _amod

        agent = _agent(tmp_path, [AIMessage(content="ok")])
        calls = {"n": 0}

        def flaky(messages):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("connection reset")
            return iter([("messages", (AIMessage(content="ok"), {}))])

        agent.runner.stream = flaky
        monkeypatch.setattr(_amod.time, "sleep", lambda s: None)
        assert "".join(agent.stream_run("hello")) == "ok"

    def test_stream_call_crash(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="x")])
        agent.runner.stream = lambda messages: (_ for _ in ()).throw(ValueError("invalid parameter x"))
        out = "".join(agent.stream_run("hello"))
        assert "执行出错" in out

    def test_stream_inner_fatal_fallback(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="x")])

        def flaky(messages):
            yield ("messages", (AIMessage(content=""), {}))
            raise ValueError("invalid parameter x")

        agent.runner.stream = flaky
        assert "".join(agent.stream_run("hello")) == "x"

    def test_stream_inner_fatal_callback(self, tmp_path):
        seen = []
        agent = _agent(tmp_path, [AIMessage(content="x")], stream_callback=seen.append)

        def flaky(messages):
            yield ("messages", (AIMessage(content=""), {}))
            raise ValueError("invalid parameter x")

        agent.runner.stream = flaky
        list(agent.stream_run("hello"))
        assert seen == ["x"]

    def test_stream_inner_recoverable(self, tmp_path, monkeypatch):
        import lib.agent as _amod

        agent = _agent(tmp_path, [AIMessage(content="x")])
        calls = {"n": 0}

        def flaky(messages):
            calls["n"] += 1
            if calls["n"] == 1:
                yield ("messages", (AIMessage(content="t1"), {}))
                raise ConnectionError("connection reset")
            yield ("messages", (AIMessage(content="t2"), {}))

        agent.runner.stream = flaky
        monkeypatch.setattr(_amod.time, "sleep", lambda s: None)
        assert "".join(agent.stream_run("hello")) == "t1t2"

    def test_stream_inner_exhaust(self, tmp_path, monkeypatch):
        import lib.agent as _amod

        agent = _agent(tmp_path, [AIMessage(content="x")])

        def flaky(messages):
            yield ("messages", (AIMessage(content=""), {}))
            raise ConnectionError("connection reset")

        agent.runner.stream = flaky
        monkeypatch.setattr(_amod.time, "sleep", lambda s: None)
        out = "".join(agent.stream_run("hello"))
        assert "执行出错" in out

    def test_stream_max_retries(self, tmp_path, monkeypatch):
        import lib.agent as _amod

        agent = _agent(tmp_path, [AIMessage(content="x")])
        agent.runner.stream = lambda messages: (_ for _ in ()).throw(ConnectionError("down"))
        monkeypatch.setattr(_amod.time, "sleep", lambda s: None)
        out = "".join(agent.stream_run("hello"))
        assert "执行出错" in out

    def test_stream_max_tokens(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="tail")])
        calls = {"n": 0}

        def flaky(messages):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("max_output_tokens hit")
            return iter([("messages", (AIMessage(content="tail"), {}))])

        agent.runner.stream = flaky
        assert "".join(agent.stream_run("hello")) == "tail"

    def test_stream_prompt_too_long(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="compacted")])
        for i in range(12):
            agent.session.add_user_message(f"q{i}")
            agent.session.add_assistant_message(f"a{i}")
        calls = {"n": 0}

        def flaky(messages):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("context_length_exceeded")
            return iter([("messages", (AIMessage(content="compacted"), {}))])

        agent.runner.stream = flaky
        assert "".join(agent.stream_run("hello")) == "compacted"

    def test_stream_partial_continue(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="full")])

        def flaky(messages):
            yield ("messages", (AIMessage(content="part"), {}))
            raise ValueError("invalid parameter x")

        agent.runner.stream = flaky
        agent._invoke_with_messages = lambda messages: "continued"
        out = "".join(agent.stream_run("hello-partial"))
        assert "continued" in out

    def test_stream_empty_fallback(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="x")])
        agent.runner.stream = lambda messages: iter([])
        agent._invoke_with_messages = lambda messages: "fb"
        assert "".join(agent.stream_run("hello")) == "fb"

    def test_continue_failed(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="x")])
        agent._invoke_with_messages = lambda messages: (_ for _ in ()).throw(RuntimeError("down"))
        assert agent._continue_after_stream_interrupt([], "part") == ""


class TestPlanTail:
    def test_run_with_plan(self, tmp_path, monkeypatch):
        import lib.core.plan_graph as _pg

        agent = _agent(tmp_path, [AIMessage(content="hi")])
        monkeypatch.setattr(_pg, "build_plan_graph",
                            lambda **kw: SimpleNamespaceShim({"final": "plan-done"}))
        assert agent.run_with_plan("do things") == "plan-done"

    def test_run_with_plan_close_raises(self, tmp_path, monkeypatch):
        import lib.core.plan_graph as _pg

        agent = _agent(tmp_path, [AIMessage(content="hi")])
        monkeypatch.setattr(_pg, "build_plan_graph",
                            lambda **kw: SimpleNamespaceShim({"final": "x"}))

        class BadConn:
            def close(self):
                raise RuntimeError("busy")

        monkeypatch.setattr(_pg, "open_plan_checkpointer", lambda ws: (None, BadConn()))
        assert agent.run_with_plan("do things") == "x"

    def test_summaries(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="hi")])
        assert isinstance(agent.get_context_summary(), str)
        assert isinstance(agent.get_memory_summary(), str)
        assert isinstance(agent.get_recent_history(), str)
        assert agent.analyze_project() is not None
        assert agent.get_tool_list() != []
        stats = agent.get_stats()
        assert stats["tools_count"] > 0

    def test_stats_with_usage(self, tmp_path):
        from lib.models.vocabulary import TokenUsage

        class UsageModel(ScriptedModel):
            @property
            def last_usage(self):
                return TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)

            @property
            def session_usage(self):
                return TokenUsage(prompt_tokens=4, completion_tokens=5, total_tokens=9)

        from lib.core.permissions import PermissionRuntime, SessionPermissionState

        agent = SAIAgent(model=UsageModel(script=[AIMessage(content="hi")]), workspace=tmp_path,
                         tools=[echo_tool], checkpoint_path=str(tmp_path / "ckpt.sqlite3"),
                         permissions=PermissionRuntime(session=SessionPermissionState()),
                         interrupt_handler=lambda payload: {"approved": True})
        stats = agent.get_stats()
        assert stats["last_total_tokens"] == 3
        assert stats["session_total_tokens"] == 9

    def test_reset(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="hi")])
        agent.session.add_user_message("q")
        agent.reset(clear_memory=True, clear_session=True)
        assert agent.session.is_empty()
        agent.reset(clear_memory=False, clear_session=False)

    def test_reload_close(self, tmp_path):
        from types import SimpleNamespace

        agent = _agent(tmp_path, [AIMessage(content="hi")])
        assert agent.reload_mcp_tools() == []
        agent._mcp_runtime = SimpleNamespace(shutdown=lambda: None)
        assert agent._load_mcp_tools() == []
        assert agent._mcp_runtime is None
        agent.close()

    def test_execute_mcp_none(self, tmp_path):
        import asyncio as _asyncio
        from types import SimpleNamespace

        agent = _agent(tmp_path, [AIMessage(content="hi")])
        assert _asyncio.run(agent.execute_mcp_tool("t")) == "❌ MCP runtime is not initialized"
        agent._mcp_runtime = SimpleNamespace(call_tool=lambda name, params: f"{name}:{params}")
        assert _asyncio.run(agent.execute_mcp_tool("t", {"a": 1})) == "t:{'a': 1}"
        agent._mcp_runtime = None

    def test_factory(self, tmp_path, monkeypatch):
        import lib.agent as _amod

        monkeypatch.setattr(_amod, "SAIAgent", lambda **kw: ("agent", kw))
        out, kw = _amod.create_sai_agent(model_type="ollama", model_name="m", workspace=str(tmp_path))
        assert out == "agent" and str(kw["workspace"]) == str(tmp_path)


class SimpleNamespaceShim:
    # run_with_plan 图替身：invoke 返回固定终态。
    def __init__(self, result):
        self._result = result

    def invoke(self, *a, **k):
        return self._result
