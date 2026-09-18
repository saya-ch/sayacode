"""Supervisor 调度的回归测试（离线可跑，不需要真模型）。

覆盖：
- 子 agent 工厂的中间件链（Hook → Permission → Safety → Prompt，无参 Safety）；
- TeamSupervisor.spawn 直接调子图（FakeModel），结果从图 state 读；
- TeamManager.spawn 的 worktree 隔离 + config 名单 + get_delivery 只读链。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage
from langchain_core.tools import tool


@tool
def _echo_tool(text: str = "") -> str:
    """测试回声工具。"""
    return f"echo:{text}"


class _FakeModel:
    """最小 chat model：首轮带 tool_calls，次轮给终答。"""

    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content="",
                tool_calls=[{"name": "_echo_tool", "args": {"text": "hi"}, "id": "call-1"}],
            )
        return AIMessage(content="done-ok")

    __call__ = invoke


def _runtime(tmp_path: Path):
    from lib.core.permissions import create_permission_runtime
    from lib.core.hooks import create_hook_runtime

    permissions = create_permission_runtime(tmp_path)
    try:
        permissions.update_session_rules({"_echo_tool": "allow"}, source="test")
    except Exception:
        pass
    return SimpleNamespace(
        workspace=tmp_path,
        model_type="fake",
        model_name="fake",
        model_config={},
        model=None,
        memory=None,
        safety=None,
        project_context=None,
        session=None,
        agent_mode="build",
        permissions=permissions,
        hooks=create_hook_runtime(tmp_path),
        tools=[],
        tool_registry=None,
    )


def test_mode_mapping():
    from lib.core.team_agents import _mode_for_agent_type

    assert _mode_for_agent_type("builder") == "build"
    assert _mode_for_agent_type("planner") == "plan"
    assert _mode_for_agent_type("reviewer") == "review"


def test_supervisor_spawn_reads_graph_state(tmp_path):
    from lib.core.team_supervisor import TeamSupervisor

    model = _FakeModel()
    runtime = _runtime(tmp_path)
    sup = TeamSupervisor(
        model=model, workspace=tmp_path, runtime=runtime, tools=[_echo_tool], home=tmp_path
    )
    worker_id = sup.spawn("planner", "hello", workspace=str(tmp_path))
    assert worker_id.startswith("w")
    result = sup.get_result(worker_id)
    assert result is not None and result["ok"] is True
    assert "done-ok" in str(result["response"])
    assert sup.wait(worker_id) is not None
    assert "supervisor" in sup.get_status()


def test_manager_spawn_planner_no_worktree(tmp_path):
    from lib.core.team_manager import TeamManager

    home = tmp_path / "home"
    home.mkdir()
    manager = TeamManager(home)
    runtime = _runtime(tmp_path)
    manager.bind_supervisor_context(
        model=_FakeModel(), workspace=tmp_path, runtime=runtime, tools=[_echo_tool]
    )
    worker_id = manager.spawn("planner", "plan task", workspace=str(tmp_path))
    state = manager.get_worker_state(worker_id)
    assert state is not None
    assert manager.get_result(worker_id)["ok"] is True
    # planner 只读：不建 worktree，delivery 为 None。
    assert manager.get_delivery(worker_id) is None
    assert worker_id in manager.get_status()
    manager.cleanup()


def test_parallel_spawns_share_one_checkpointer(tmp_path):
    """并行委托同文件写：共享单连接 + WAL，不得 database is locked。"""
    import threading

    from lib.core.team_manager import TeamManager

    home = tmp_path / "home"
    home.mkdir()
    managers = []
    for _ in range(2):
        manager = TeamManager(home)
        manager.bind_supervisor_context(
            model=_FakeModel(), workspace=tmp_path, runtime=_runtime(tmp_path), tools=[_echo_tool]
        )
        managers.append(manager)
    savers = {id(m.supervisor._team_checkpointer()) for m in managers}
    assert len(savers) == 1, "同文件必须共享单连接"
    errors = []

    def run_spawn(manager, index):
        try:
            worker_id = manager.spawn("planner", f"task {index}", workspace=str(tmp_path))
            record = manager.wait(worker_id, timeout=60.0)
            assert record is not None
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run_spawn, args=(managers[i % 2], i), daemon=True) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert errors == []
    for manager in managers:
        manager.cleanup()


def test_manager_rejects_shared_builder(tmp_path):
    from lib.core.team_manager import TeamManager

    home = tmp_path / "home2"
    home.mkdir()
    manager = TeamManager(home)
    runtime = _runtime(tmp_path)
    manager.bind_supervisor_context(
        model=_FakeModel(), workspace=tmp_path, runtime=runtime, tools=[_echo_tool]
    )
    try:
        manager.spawn("shared-builder", "x", workspace=str(tmp_path))
    except RuntimeError as exc:
        assert "shared-builder" in str(exc)
    else:
        raise AssertionError("shared-builder 应该被拒绝")
