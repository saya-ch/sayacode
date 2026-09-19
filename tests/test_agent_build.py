# agent.py 第三波：构造分支、流状态通道、延续回调、真工厂。


from langchain_core.messages import AIMessage

from lib.agent import SAIAgent
from lib.core.permissions import PermissionRuntime, SessionPermissionState

from tests.test_agent_run import ScriptedModel, _agent, echo_tool


class TestBuildGaps:
    def test_init_wiring(self, tmp_path):
        class WiredModel(ScriptedModel):
            @property
            def context_window(self):
                return 8000

            def chat(self, messages):
                return "hi"

        agent = SAIAgent(model=WiredModel(script=[AIMessage(content="hi")]), workspace=tmp_path,
                         tools=[echo_tool], checkpoint_path=str(tmp_path / "ckpt.sqlite3"),
                         permissions=PermissionRuntime(session=SessionPermissionState()))
        assert agent.session.model_context_limit == 8000

    def test_force_compact_fallback(self, tmp_path):
        from types import SimpleNamespace

        agent = _agent(tmp_path, [AIMessage(content="hi")])
        from lib.agent import recovery as _recovery

        agent.session = SimpleNamespace(compact=lambda: "c")
        agent._recovery_state = {}
        _recovery.force_compact_session(agent.session, agent._recovery_state)
        assert agent._recovery_state["compact_api"] == "compact_fallback"

    def test_default_tools(self, tmp_path):
        agent = SAIAgent(model=ScriptedModel(script=[AIMessage(content="hi")]), workspace=tmp_path,
                         tools=None, checkpoint_path=str(tmp_path / "ckpt.sqlite3"),
                         permissions=PermissionRuntime(session=SessionPermissionState()))
        assert len(agent.tools) > 0

    def test_delegate_build_failure(self, tmp_path, monkeypatch):
        import lib.core.team_supervisor as _ts

        monkeypatch.setattr(_ts, "TeamSupervisor",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        agent = _agent(tmp_path, [AIMessage(content="hi")])
        assert agent.tools is not None

    def test_frozen_tool_name(self):
        a = SAIAgent.__new__(SAIAgent)

        class Frozen:
            __slots__ = ()

        out = a._normalize_tools([Frozen()])
        assert len(out) == 1

    def test_no_permissions_fallback(self, tmp_path):
        agent = SAIAgent(model=ScriptedModel(script=[AIMessage(content="hi")]), workspace=tmp_path,
                         tools=[echo_tool], checkpoint_path=str(tmp_path / "ckpt.sqlite3"))
        assert agent.run("hello") == "hi"

    def test_enable_mcp_empty(self, tmp_path):
        agent = SAIAgent(model=ScriptedModel(script=[AIMessage(content="hi")]), workspace=tmp_path,
                         tools=[echo_tool], checkpoint_path=str(tmp_path / "ckpt.sqlite3"),
                         permissions=PermissionRuntime(session=SessionPermissionState()),
                         enable_mcp=True)
        assert agent._mcp_tools == []

    def test_usage_estimate_fallback(self):
        from types import SimpleNamespace

        from lib.agent import usage as agent_usage

        seen = []
        model = SimpleNamespace(_record_usage=lambda u: seen.append(u))
        agent_usage.record_invoke_result(model, {"messages": [AIMessage(content="plain")]})
        assert seen[0].total_tokens > 0

    def test_zero_usage_skipped(self):
        from types import SimpleNamespace

        from lib.agent import usage as agent_usage

        seen = []
        model = SimpleNamespace(_record_usage=lambda u: seen.append(u))
        msg = AIMessage(content="x", usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
        agent_usage.record_stream_chunk(model, {"messages": [msg]})
        assert seen == []

    def test_last_extra_metadata(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="hi", additional_kwargs={"k": 1})])
        assert agent.run("hello") == "hi"
        assert agent.runner is not None


class TestStreamStatus:
    def _tool_chunk(self):
        return ("updates", {"agent": {"messages": [AIMessage(content="", tool_calls=[
            {"name": "echo_tool", "args": {"text": "x"}, "id": "1", "type": "tool_call"}])]}})

    def test_tool_yield_status(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="x")])
        agent.runner.stream = lambda messages: iter([self._tool_chunk()])
        agent._invoke_with_messages = lambda messages: "fb"
        out = "".join(agent.stream_run("hello"))
        assert "echo_tool" in out

    def test_tool_no_status(self, tmp_path):
        agent = _agent(tmp_path, [AIMessage(content="x")])
        agent.runner.stream = lambda messages: iter([self._tool_chunk()])
        assert "".join(agent.stream_run("hello", emit_tool_status=False)) == "x"

    def test_tool_with_callback(self, tmp_path):
        seen = []
        agent = _agent(tmp_path, [AIMessage(content="x")], stream_callback=seen.append)
        agent.runner.stream = lambda messages: iter([self._tool_chunk()])
        list(agent.stream_run("hello"))
        assert seen != []

    def test_continuation_callback(self, tmp_path):
        seen = []
        agent = _agent(tmp_path, [AIMessage(content="x")], stream_callback=seen.append)

        def flaky(messages):
            yield ("messages", (AIMessage(content="part"), {}))
            raise ConnectionError("cut")

        agent.runner.stream = flaky
        agent._invoke_with_messages = lambda messages: "rest"
        list(agent.stream_run("hello"))
        assert any("rest" in s for s in seen)

    def test_fallback_callback(self, tmp_path):
        seen = []
        agent = _agent(tmp_path, [AIMessage(content="x")], stream_callback=seen.append)
        agent.runner.stream = lambda messages: iter([])
        agent._invoke_with_messages = lambda messages: "fb"
        list(agent.stream_run("hello"))
        assert seen == ["fb"]

    def test_real_factory(self, tmp_path):
        from lib.agent import create_sai_agent

        agent = create_sai_agent(model_type="ollama", model_name="m", workspace=str(tmp_path))
        assert agent.workspace == tmp_path.resolve()
        agent.close()
