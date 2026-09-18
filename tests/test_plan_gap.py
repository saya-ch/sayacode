"""计划与委托的边角覆盖：错误分支、守卫、导出面。"""

import pytest

from lib.core.plans import PlanStore


def _store(tmp_path, sid="s1"):
    return PlanStore(tmp_path / "ws", sid)


def test_note_round_counts_and_clear_tolerates_missing(tmp_path):
    store = _store(tmp_path)
    assert store.note_round() == 0
    store.create("g", ["a"])
    assert store.note_round() == 1
    assert store.note_round() == 2
    store.clear()
    store.clear()
    assert store.get() is None


def test_plan_tool_error_paths(tmp_path):
    from lib.tools.plan_tools import create_plan_tools

    store = _store(tmp_path)
    tools = {t.name: t for t in create_plan_tools(lambda: store)}
    assert "失败" in tools["plan_create"].invoke({"goal": "", "tasks": ["a"]})
    assert "无计划" in tools["plan_get"].invoke({})


def test_graph_build_guards():
    import lib.core.plan_graph as pg

    with pytest.raises(RuntimeError):
        pg.build_plan_graph(model=None, plan_tools=[], run_turn=lambda p: "",
                            store=None, overlay="", checkpointer=None)


def test_graph_build_guards_missing_deps(monkeypatch):
    import lib.core.plan_graph as pg

    monkeypatch.setattr(pg, "StateGraph", None)
    with pytest.raises(RuntimeError):
        pg.build_plan_graph(model=None, plan_tools=[], run_turn=lambda p: "",
                            store=None, overlay="", checkpointer=object())


def test_planner_failure_surface(tmp_path):
    from lib.core.plan_graph import build_plan_graph, open_plan_checkpointer

    class BoomModel:
        def bind_tools(self, tools, **kwargs):
            return self

    workspace = tmp_path / "ws"
    store = PlanStore(workspace, "s1")
    saver, conn = open_plan_checkpointer(workspace)
    try:
        graph = build_plan_graph(model=BoomModel(), plan_tools=[], run_turn=lambda p: "",
                                 store=store, overlay="o", checkpointer=saver)
        result = graph.invoke({"goal": "g", "max_rounds": 1},
                              {"configurable": {"thread_id": "t1"}, "recursion_limit": 20})
    finally:
        conn.close()
    assert "规划失败" in result["final"]


def test_last_text_output_fallback():
    from lib.core.plan_graph import _last_text

    assert _last_text({"output": "o"}) == "o"
    assert _last_text("raw") == "raw"
    assert _last_text({}) == "{}" or _last_text({}) == ""


def test_lazy_exports_resolve():
    import lib

    for name in ("SAIAgent", "MemoryManager", "SessionManager", "RuntimeContext", "ToolRegistry"):
        assert getattr(lib, name) is not None
    with pytest.raises(AttributeError):
        getattr(lib, "NoSuchExport")


def test_package_main_calls_cli_main(monkeypatch, tmp_path):
    import runpy
    import sys

    import lib.cli

    called = {}
    monkeypatch.setattr(lib.cli, "main", lambda *a, **k: called.setdefault("yes", True))
    monkeypatch.setattr(sys, "argv", ["sayacode"])
    runpy.run_module("lib.__main__", run_name="__main__", alter_sys=True)
    assert called.get("yes") is True


def test_agent_close_releases_runner(tmp_path):
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from lib.agent import SAIAgent

    class FakeModel(BaseChatModel):
        model_name: str = "fake"
        model_type: str = "fake"
        context_window: int = 64

        @property
        def _llm_type(self):
            return "fake"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="hi"))])

        def chat(self, messages):
            return "hi"

        def bind_tools(self, tools, **kwargs):
            return self

    agent = SAIAgent(model=FakeModel(), workspace=tmp_path / "ws")
    assert agent.runner is not None
    agent.close()
    assert agent.runner._saver_conn is None
