# agent_runtime 全覆盖：构造器、runner、消息工具函数。

from types import SimpleNamespace
from typing import Any, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from lib.core.agent_runtime import (
    AgentRunner,
    ConversationManager,
    PromptBuilder,
    TurnState,
    TurnTransition,
    content_to_text,
    extract_tool_names,
    message_kind,
    message_to_chat_dict,
)
from lib.core.memory import MemoryManager
from lib.core.session import SessionManager


class ScriptedModel(BaseChatModel):
    # 按剧本出牌的 fake 模型。
    script: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages: list[BaseMessage], stop: Optional[list[str]] = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs: Any) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self.script[0])])

    def bind_tools(self, tools, **kwargs):
        return self


def _runner(tmp_path, **kw):
    # 真 runner 固件（假模型 + 隔离 checkpoint）。
    kw.setdefault("checkpoint_path", str(tmp_path / "ckpt.sqlite3"))
    runner = AgentRunner(model=ScriptedModel(script=[AIMessage(content="hi")]), tools=[],
                         system_prompt="sys", **kw)
    runner.rebuild()
    return runner


class TestTurn:
    def test_terminal(self):
        assert TurnState().is_terminal is True
        assert TurnState(transition=TurnTransition.NEXT_TURN).is_terminal is False
        assert TurnState(transition=TurnTransition.NEXT_TURN, needs_follow_up=True).should_continue is True
        assert TurnState(transition=TurnTransition.NEXT_TURN).should_continue is False


class TestBuilder:
    def _pb(self, tmp_path):
        from lib.core.context import ProjectContext

        return PromptBuilder(workspace=tmp_path, project_context=ProjectContext(str(tmp_path)))

    def test_prompts(self, tmp_path):
        pb = self._pb(tmp_path)
        assert "SAYA" in pb.build_system_prompt()
        session = SessionManager()
        assert isinstance(pb.build_system_content(session, "base"), str)
        assert "提醒" in pb.build_system_content(session, "base", reminder_state={"agent_mode": "plan"})
        assert pb.build_system_content(session, "base", include_context=False) == "base"

    def test_history(self, tmp_path):
        session = SessionManager()
        session.add_user_message("q")
        session.add_assistant_message("a")
        msgs = PromptBuilder.history_messages(session)
        assert len(msgs) == 1
        session.add_message("system", "sys", metadata={"compressed": True, "type": "boundary"})
        session.add_user_message("q2")
        session.add_assistant_message("a2")
        msgs = PromptBuilder.history_messages(session)
        assert any(isinstance(m, SystemMessage) for m in msgs)

    def test_build_messages(self, tmp_path):
        pb = self._pb(tmp_path)
        session = SessionManager()
        msgs = pb.build_messages("hello", session, "base")
        assert isinstance(msgs[0], SystemMessage) and isinstance(msgs[-1], HumanMessage)

    def test_conversation(self):
        session, memory = SessionManager(), MemoryManager(session_id="s")
        cm = ConversationManager(session, memory)
        assert cm.start_turn("hi", enhancer=str.upper) == ("hi", "HI")
        cm.finish_turn("hi", "yo", metadata={"k": 1})
        assert len(memory.interactions) == 1


class TestRunner:
    def test_require_raise(self, tmp_path):
        runner = AgentRunner(model=ScriptedModel(script=[]), tools=[], system_prompt="s",
                             checkpoint_path=str(tmp_path / "c.sqlite3"))
        assert runner.invoke([]) is None
        assert runner.stream([]) is None

    def test_close_conn_fail(self, tmp_path):
        runner = AgentRunner(model=ScriptedModel(script=[]), tools=[], system_prompt="s",
                             checkpoint_path=str(tmp_path / "c.sqlite3"))

        class BadConn:
            def close(self):
                raise RuntimeError("busy")

        runner._saver_conn = BadConn()
        runner.close()
        assert runner._saver_conn is None
        runner.close()

    def test_resume_paths(self, tmp_path):
        runner = _runner(tmp_path)
        assert runner.thread_message_count() >= 0
        runner.sync_messages([HumanMessage(content="hi")])
        assert runner.thread_message_count() == 1
        runner.refresh_prompt("new-sys")
        runner.prompt_middleware = None
        runner.refresh_prompt("new-sys")
        runner.remember_turn(1, "q", "a")

    def test_remember_no_store(self, tmp_path):
        runner = _runner(tmp_path)
        runner._store = None
        runner.remember_turn(1, "q", "a")

    def test_remember_crash(self, tmp_path):
        runner = _runner(tmp_path)

        class BadStore:
            def put(self, *a, **k):
                raise RuntimeError("busy")

        runner._store = BadStore()
        runner.remember_turn(1, "q", "a")

    def test_bind_fallback(self, tmp_path):
        runner = AgentRunner(model=SimpleNamespace(), tools=[], system_prompt="s",
                             checkpoint_path=str(tmp_path / "c.sqlite3"))
        assert runner._bind_tools() is runner.model

    def test_bind_crash(self, tmp_path, monkeypatch):

        model = SimpleNamespace(bind_tools=lambda tools: (_ for _ in ()).throw(RuntimeError("boom")))
        runner = AgentRunner(model=model, tools=[], system_prompt="s",
                             checkpoint_path=str(tmp_path / "c.sqlite3"))
        assert runner._bind_tools() is model

    def test_no_checkpoint(self, tmp_path):
        runner = AgentRunner(model=ScriptedModel(script=[]), tools=[], system_prompt="s", checkpoint_path="")
        assert runner._open_checkpointer() is None

    def test_sqlite_import_fail(self, tmp_path, monkeypatch):
        import sys as _sys

        runner = AgentRunner(model=ScriptedModel(script=[]), tools=[], system_prompt="s",
                             checkpoint_path=str(tmp_path / "c.sqlite3"))
        monkeypatch.setitem(_sys.modules, "langgraph.checkpoint.sqlite", None)
        assert runner._open_checkpointer() is None

    def test_store_variants(self, tmp_path, monkeypatch):
        import sys as _sys

        runner = AgentRunner(model=ScriptedModel(script=[]), tools=[], system_prompt="s",
                             checkpoint_path=str(tmp_path / "c.sqlite3"))
        assert runner._open_store() is not None
        monkeypatch.setitem(_sys.modules, "langgraph.store.memory", None)
        assert runner._open_store() is None

    def test_create_no_factory(self, tmp_path, monkeypatch):
        import lib.core.agent_runtime as _ar

        runner = AgentRunner(model=ScriptedModel(script=[]), tools=[], system_prompt="s",
                             checkpoint_path=str(tmp_path / "c.sqlite3"))
        runner.model_with_tools = runner.model
        monkeypatch.setattr(_ar, "create_langchain_agent", None)
        assert runner._create_agent() is None

    def test_create_bad_model(self, tmp_path):
        runner = AgentRunner(model=SimpleNamespace(), tools=[], system_prompt="s",
                             checkpoint_path=str(tmp_path / "c.sqlite3"))
        runner.model_with_tools = SimpleNamespace()
        assert runner._create_agent() is None

    def test_graph_no_invoke(self, tmp_path, monkeypatch):

        runner = AgentRunner(model=SimpleNamespace(), tools=[], system_prompt="s",
                             checkpoint_path=str(tmp_path / "c.sqlite3"))
        runner.model_with_tools = SimpleNamespace()
        assert runner._create_graph_agent() is None


class TestHelpers:
    def test_message_dict(self):
        assert message_to_chat_dict(SystemMessage(content="s")) == {"role": "system", "content": "s"}
        assert message_to_chat_dict(AIMessage(content="a"))["role"] == "assistant"
        assert message_to_chat_dict(HumanMessage(content="h"))["role"] == "user"
        assert message_to_chat_dict(ToolMessage(content="t", tool_call_id="1"))["role"] == "user"

    def test_content_text(self):
        assert content_to_text("s") == "s"
        assert content_to_text([{"type": "text", "text": "a"}, {"type": "other"}]) == "a"
        assert content_to_text([SimpleNamespace(type="text", text="b")]) == "b"
        assert content_to_text([SimpleNamespace(type="text", text="")]) == ""
        assert content_to_text(None) == ""
        assert content_to_text(42) == "42"

    def test_kind(self):
        assert message_kind(HumanMessage(content="x")) == "human"
        assert message_kind(SimpleNamespace()) == "simplenamespace"

    def test_tool_names(self):
        assert extract_tool_names([{"name": "a"}]) == ["a"]
        assert extract_tool_names([{"function": {"name": "b"}}]) == ["b"]
        assert extract_tool_names([{}]) == ["unknown"]
        assert extract_tool_names([SimpleNamespace(name="c")]) == ["c"]
        assert extract_tool_names([SimpleNamespace(function={"name": "d"})]) == ["d"]
        assert extract_tool_names([SimpleNamespace(function=SimpleNamespace(name="e"))]) == ["e"]
        assert extract_tool_names([SimpleNamespace()]) == ["unknown"]
        assert extract_tool_names(None) == []
