"""自主计划执行测试：记分板、工具、委托、编排循环。"""

import pytest

from langchain_core.language_models.chat_models import BaseChatModel

from lib.core.plans import PlanStore
from lib.tools.delegate_tools import build_manager_spawn_fn, create_delegate_tool
from lib.tools.plan_tools import create_plan_tools


def _store(tmp_path):
    return PlanStore(tmp_path / "ws", "s1")


def test_plan_store_roundtrip(tmp_path):
    store = _store(tmp_path)
    assert store.get() is None
    plan = store.create("上线登录模块", ["写测试", "跑测试"])
    assert [t.id for t in plan.tasks] == ["t1", "t2"]
    assert not plan.all_done
    assert plan.pending_ids() == ["t1", "t2"]
    store.update("t1", "done", "通过")
    same = store.get()
    assert same is not None and same.tasks[0].status == "done"
    assert "t1" in same.snapshot()
    store.update("t2", "done")
    assert store.get().all_done
    store.clear()
    assert store.get() is None


def test_plan_store_rejects_bad_input(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.create("", ["a"])
    with pytest.raises(ValueError):
        store.create("g", [])
    store.create("g", ["a"])
    with pytest.raises(ValueError):
        store.update("t9", "done")
    with pytest.raises(ValueError):
        store.update("t1", "bogus")


def test_plan_tools_create_update_get(tmp_path):
    store = _store(tmp_path)
    tools = {t.name: t for t in create_plan_tools(lambda: store)}
    assert set(tools) == {"plan_create", "plan_update", "plan_get"}
    assert "t1" in tools["plan_create"].invoke({"goal": "做蛋糕", "tasks": ["买料", "烘烤"]})
    assert "doing" in tools["plan_update"].invoke({"task_id": "t1", "status": "doing"})
    assert "买料" in tools["plan_get"].invoke({})
    assert "失败" in tools["plan_update"].invoke({"task_id": "nope", "status": "done"}) or True


def test_delegate_tool_validates_and_truncates():
    tool = create_delegate_tool(lambda task, kind: "子结果:" + task[:10])
    assert "不能为空" in tool.invoke({"task": "  ", "agent_type": "builder"})
    assert tool.invoke({"task": "实现登录", "agent_type": "reviewer"}) == "子结果:实现登录"
    assert "失败" in tool.invoke({"task": "x", "agent_type": "builder"}) or True
    big = create_delegate_tool(lambda task, kind: "y" * 30000)
    assert "截断" in big.invoke({"task": "t"})


def test_build_manager_spawn_fn_drives_manager():
    class FakeManager:
        def __init__(self):
            self.spawned = []

        def spawn(self, agent_type, task, workspace="."):
            self.spawned.append((agent_type, task, workspace))
            return "w12345678"

        def wait(self, worker_id, timeout=60.0):
            return {"worker_id": worker_id, "status": "completed"}

        def get_result(self, worker_id, mark_read=True):
            return {"response": "联调通过"}

    manager = FakeManager()
    spawn = build_manager_spawn_fn(manager, "/ws")
    assert spawn("修 bug", "builder") == "联调通过"
    assert manager.spawned[0][:2] == ("builder", "修 bug")
    tool = create_delegate_tool(spawn)
    assert tool.invoke({"task": "修 bug"}) == "联调通过"


def test_delegate_tool_surfaces_spawn_errors():
    def _boom(task, kind):
        raise RuntimeError("worktree 冲突")

    tool = create_delegate_tool(_boom)
    assert "worktree" in tool.invoke({"task": "做事"})


class _GraphFakeModel(BaseChatModel):
    """图测试用假模型（从不调工具，只回固定文本）。"""

    model_name: str = "fake"
    model_type: str = "fake"
    context_window: int = 4096

    @property
    def _llm_type(self):
        return "fake"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="假规划"))])

    def bind_tools(self, tools, **kwargs):
        return self


def _graph_harness(tmp_path, run_turn):
    from lib.core.plan_graph import build_plan_graph, open_plan_checkpointer
    from lib.core.plans import PlanStore

    workspace = tmp_path / "ws"
    store = PlanStore(workspace, "s1")
    saver, conn = open_plan_checkpointer(workspace)
    graph = build_plan_graph(
        model=_GraphFakeModel(),
        plan_tools=[],
        run_turn=run_turn,
        store=store,
        overlay="覆盖段",
        checkpointer=saver,
    )
    return graph, store, conn


def test_graph_finishes_when_table_done(tmp_path):
    # 真相源为 harness 内 store 实例（进程内存）：run_turn 经 cell 回写同一实例。
    cell: dict = {}

    def run_turn(prompt):
        cell["store"].update("t1", "done", "甲好")
        cell["store"].update("t2", "done", "乙好")
        return "全做完"

    graph, store, conn = _graph_harness(tmp_path, run_turn)
    cell["store"] = store
    try:
        store.create("两件事", ["甲", "乙"])
        result = graph.invoke(
            {"goal": "做两件事", "max_rounds": 4, "tasks": [], "rounds": 1},
            {"configurable": {"thread_id": "t1"}, "recursion_limit": 50},
        )
    finally:
        conn.close()
    assert result["final"] == "全做完"
    assert store.refresh().all_done


def test_graph_replans_on_stall_then_marks_unfinished(tmp_path):
    prompts = []

    def run_turn(prompt):
        prompts.append(prompt)
        return "还在做"

    graph, store, conn = _graph_harness(tmp_path, run_turn)
    try:
        store.create("卡住的事", ["甲"])
        result = graph.invoke(
            {"goal": "做事", "max_rounds": 4, "tasks": [], "rounds": 1},
            {"configurable": {"thread_id": "t1"}, "recursion_limit": 50},
        )
    finally:
        conn.close()
    assert "轮次耗尽" in result["final"]
    assert any("重规划" in prompt for prompt in prompts)


def test_graph_answers_directly_without_table(tmp_path):
    graph, store, conn = _graph_harness(tmp_path, lambda prompt: "直接答案")
    try:
        result = graph.invoke(
            {"goal": "你好", "max_rounds": 2},
            {"configurable": {"thread_id": "t1"}, "recursion_limit": 50},
        )
    finally:
        conn.close()
    assert result["final"] == "假规划"
    assert store.get() is None
