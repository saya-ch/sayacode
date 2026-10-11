"""用真实 Web 图验证 Skill 设置不会生成待执行任务或破坏审批。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END

from sayacode.agent.runtime import can_rewind_checkpoint
from sayacode.host.application import WebHost
from tests.support import ContractModel
from tests.web.test_host import _configured_host
from tests.web.test_runtime_controls import _GateModel


def _write_skill(workspace: Path, name: str, body: str) -> None:
    directory = workspace / ".agents" / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 离线方法\n---\n\n{body}\n",
        encoding="utf-8",
    )


async def _wait_run(host: WebHost, thread_id: str) -> None:
    await asyncio.wait_for(host._runs[thread_id].task, timeout=15)


@pytest.mark.parametrize("prior_turn", [False, True])
async def test_selected_skill_keeps_main_thread_idle_and_enters_next_model(
    tmp_path: Path, prior_turn: bool
) -> None:
    host = await _configured_host(tmp_path)
    try:
        app = await host._app_for_workspace(host.initial_workspace_id)
        model = ContractModel()
        app.model_override = model
        thread_id = app.session_id
        _write_skill(app.workspace, "review", "逐项核对唯一正文")
        if prior_turn:
            await host.start_run(thread_id, "先完成上一轮")
            await _wait_run(host, thread_id)
        before = await host.thread_snapshot(thread_id)

        await host.activate_skill(thread_id, "review")
        handle, _ = await app._context_for_thread(thread_id)
        state = await host.runtime.get_state(handle, thread_id)
        assert state.next == () and not state.interrupts
        assert state.values["active_skills"] == {"review": "逐项核对唯一正文"}
        snapshot = await host.thread_snapshot(thread_id)
        assert not snapshot["pending_steps"]
        assert not snapshot["resume_available"]
        assert snapshot["messages"] == before["messages"]
        assert state.metadata["sayacode_state_phase"] == "complete"
        history = [
            item
            async for item in handle.graph.aget_state_history(
                app.runtime.thread_config(thread_id), limit=2
            )
        ]
        assert history[1].metadata["sayacode_state_phase"] == "write"
        assert history[1].metadata["sayacode_state_operation_id"] == (
            state.metadata["sayacode_state_operation_id"]
        )

        await host.start_run(thread_id, "按选中的方法检查")
        await _wait_run(host, thread_id)
        assert "逐项核对唯一正文" in str(model.received[-1][0].content)
    finally:
        await host.aclose()


async def test_idle_update_rejects_running_thread_without_writing(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        app = await host._app_for_workspace(host.initial_workspace_id)
        app.model_override = _GateModel()
        model = app.model_override
        thread_id = app.session_id
        await host.start_run(thread_id, "运行中不能修改")
        await asyncio.wait_for(model.started.wait(), timeout=10)
        handle, _ = await app._context_for_thread(thread_id)
        before = await app.runtime.get_state(handle, thread_id)
        with pytest.raises(RuntimeError, match="正在运行"):
            await app.runtime.update_idle_state(
                handle,
                {"active_skills": {"review": "不应写入"}},
                thread_id=thread_id,
                operation="skill.activate",
            )
        after = await app.runtime.get_state(handle, thread_id)
        # 启动中的模型本身会产生运行检查点；本次闲态更新不能再写入 Skill。
        assert (after.values or {}).get("active_skills") == (before.values or {}).get(
            "active_skills"
        )
        assert not after.values.get("active_skills")
        model.release.set()
        await _wait_run(host, thread_id)
    finally:
        if isinstance(app.model_override, _GateModel):
            app.model_override.release.set()
        await host.aclose()


async def test_selected_skill_keeps_child_idle_and_applies_to_followup(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        app = await host._app_for_workspace(host.initial_workspace_id)
        model = ContractModel()
        app.model_override = model
        _write_skill(app.workspace, "review", "子线程方法唯一正文")
        child = await host.spawn_task(app.session_id, "reviewer", "检查", "子任务", False)
        await asyncio.wait_for(host.tasks.wait(child["id"]), timeout=15)
        if app._wake_runs:
            await asyncio.wait_for(asyncio.gather(*list(app._wake_runs.values())), timeout=15)

        await host.activate_skill(child["thread_id"], "review")
        handle, _ = await app._context_for_thread(child["thread_id"])
        state = await host.runtime.get_state(handle, child["thread_id"])
        assert state.next == () and not state.interrupts
        assert not (await host.thread_snapshot(child["thread_id"]))["pending_steps"]
        await host.task_action(child["id"], "followup", {"message": "继续核对"})
        if app._wake_runs:
            await asyncio.wait_for(asyncio.gather(*list(app._wake_runs.values())), timeout=15)
        await asyncio.wait_for(host.tasks.wait(child["id"]), timeout=15)
        assert any("子线程方法唯一正文" in str(messages[0].content) for messages in model.received)
    finally:
        await host.aclose()


async def test_deactivation_keeps_other_skills_and_survives_restart(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        app = await host._app_for_workspace(host.initial_workspace_id)
        app.model_override = ContractModel()
        thread_id = app.session_id
        _write_skill(app.workspace, "first", "第一个正文")
        _write_skill(app.workspace, "second", "第二个正文")
        await host.activate_skill(thread_id, "first")
        await host.activate_skill(thread_id, "second")
        assert await app.deactivate_skill("first", thread_id=thread_id)
        assert not await app.deactivate_skill("first", thread_id=thread_id)
        workspace, home = app.workspace, host.paths.home
        await host.aclose()
        host = await WebHost.open(workspace, home=home)
        app = await host._app_for_workspace(host.initial_workspace_id)
        model = ContractModel()
        app.model_override = model
        handle, _ = await app._context_for_thread(thread_id)
        state = await host.runtime.get_state(handle, thread_id)
        assert state.values["active_skills"] == {"second": "第二个正文"}
        assert state.next == ()
        await host.start_run(thread_id, "继续检查")
        await _wait_run(host, thread_id)
        system = str(model.received[-1][0].content)
        assert "第二个正文" in system
        assert "第一个正文" not in system
    finally:
        await host.aclose()


async def test_activation_then_compaction_leaves_web_ready_for_new_input(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        app = await host._app_for_workspace(host.initial_workspace_id)
        app.model_override = ContractModel()
        thread_id = app.session_id
        await host.start_run(thread_id, "完成上一轮")
        await _wait_run(host, thread_id)
        _write_skill(app.workspace, "review", "摘要后仍保留的方法")
        await host.activate_skill(thread_id, "review")
        assert (await host.compact_thread(thread_id, "保留待处理问题"))["compacted"]
        handle, _ = await app._context_for_thread(thread_id)
        state = await host.runtime.get_state(handle, thread_id)
        assert state.next == () and not state.interrupts
        assert state.values["active_skills"] == {"review": "摘要后仍保留的方法"}
        assert any(
            item.additional_kwargs.get("lc_source") == "summarization"
            for item in state.values["messages"]
        )
        assert state.metadata["sayacode_state_operation"] == "compact"
        assert not (await host.thread_snapshot(thread_id))["pending_steps"]
        await host.start_run(thread_id, "继续任务")
        await _wait_run(host, thread_id)
    finally:
        await host.aclose()


async def test_rewind_only_accepts_completed_turns_and_preserves_tool_effects(tmp_path: Path) -> None:
    host = await _configured_host(tmp_path)
    try:
        app = await host._app_for_workspace(host.initial_workspace_id)
        app.model_override = ContractModel(
            responses=[
                AIMessage(content="第一轮已完成"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"path": "sample.txt", "content": "工具执行已落盘"},
                            "id": "write-once",
                        }
                    ],
                ),
                AIMessage(content="第二轮已完成"),
            ]
        )
        thread_id = app.session_id
        await host.set_trust(thread_id, "full")
        for message in ["第一轮", "写入测试文件"]:
            await host.start_run(thread_id, message)
            await _wait_run(host, thread_id)
        handle, _ = await app._context_for_thread(thread_id)
        history = await app.runtime.get_history(handle, thread_id)
        intermediate = next(
            item for item in history
            if item.next and item.values.get("messages")
            and isinstance(item.values["messages"][-1], AIMessage)
            and item.values["messages"][-1].tool_calls
        )
        completed = [item for item in history if can_rewind_checkpoint(item)]
        assert len(completed) == 2
        _write_skill(app.workspace, "review", "回退测试正文")
        await app.activate_skill("review", thread_id=thread_id)
        skill_update = await app.runtime.get_state(handle, thread_id)
        await host.compact_thread(thread_id)
        summary_update = await app.runtime.get_state(handle, thread_id)
        assert not can_rewind_checkpoint(skill_update)
        assert not can_rewind_checkpoint(summary_update)
        before = await app.runtime.get_state(handle, thread_id)
        history_count = len(await app.runtime.get_history(handle, thread_id))

        for rejected in [intermediate, skill_update, summary_update]:
            checkpoint_id = rejected.config["configurable"]["checkpoint_id"]
            with pytest.raises(RuntimeError, match="已完成轮次"):
                await app.runtime.rewind(handle, thread_id, checkpoint_id)
            assert (await app.runtime.get_state(handle, thread_id)).config == before.config
        assert len(await app.runtime.get_history(handle, thread_id)) == history_count
        assert (app.workspace / "sample.txt").read_text(encoding="utf-8") == "工具执行已落盘"

        checkpoint_id = completed[-1].config["configurable"]["checkpoint_id"]
        await host.rewind_thread(thread_id, checkpoint_id)
        rewound = await app.runtime.get_state(handle, thread_id)
        assert not rewound.next and not rewound.interrupts
        assert len(rewound.values["messages"]) == 2
        await host.start_run(thread_id, "回退后发起新任务")
        await _wait_run(host, thread_id)
        assert (app.workspace / "sample.txt").read_text(encoding="utf-8") == "工具执行已落盘"
    finally:
        await host.aclose()


@pytest.mark.parametrize("end_failure", [False, True])
async def test_idle_update_propagates_write_or_end_failure_without_clearing_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, end_failure: bool
) -> None:
    host = await _configured_host(tmp_path)
    try:
        app = await host._app_for_workspace(host.initial_workspace_id)
        app.model_override = ContractModel()
        handle, _ = await app._context_for_thread(app.session_id)
        original_update = handle.graph.aupdate_state

        async def fail_selected(config: Any, values: Any, **kwargs: Any) -> Any:
            if (kwargs.get("as_node") == END) == end_failure:
                raise OSError("注入保存故障")
            return await original_update(config, values, **kwargs)

        monkeypatch.setattr(handle.graph, "aupdate_state", fail_selected)
        with pytest.raises(OSError, match="保存故障"):
            await app.runtime.update_idle_state(
                handle,
                {"active_skills": {"review": "故障测试正文"}},
                thread_id=app.session_id,
                operation="skill.activate",
            )
        state = await app.runtime.get_state(handle, app.session_id)
        assert bool(state.next) is end_failure
        if end_failure:
            assert state.values["active_skills"]["review"] == "故障测试正文"
            assert state.metadata["sayacode_state_phase"] == "write"
            with pytest.raises(RuntimeError, match="待执行"):
                await app.runtime.update_idle_state(
                    handle, {}, thread_id=app.session_id, operation="skill.deactivate"
                )
            assert (await app.runtime.get_state(handle, app.session_id)).config == state.config
    finally:
        await host.aclose()


@pytest.mark.parametrize("blocked", ["approval", "drain"])
async def test_real_pending_execution_is_preserved_by_skill_and_compact_controls(
    tmp_path: Path, blocked: str
) -> None:
    host = await _configured_host(tmp_path)
    try:
        app = await host._app_for_workspace(host.initial_workspace_id)
        thread_id = app.session_id
        _write_skill(app.workspace, "review", "不能清理真实执行")
        model = _GateModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "delete_file", "args": {"path": "sample.txt"}, "id": "delete"}
                    ],
                )
            ]
        )
        app.model_override = model
        (app.workspace / "sample.txt").write_text("保持原样", encoding="utf-8")
        await host.start_run(thread_id, "执行工具")
        await asyncio.wait_for(model.started.wait(), timeout=10)
        if blocked == "drain":
            host._runs[thread_id].control.request_drain("验证停止")
        model.release.set()
        await _wait_run(host, thread_id)
        handle, context = await app._context_for_thread(thread_id)
        before = await app.runtime.get_state(handle, thread_id)
        assert before.next
        assert bool(before.interrupts) is (blocked == "approval")
        pattern = "等待审批" if blocked == "approval" else "待执行"
        with pytest.raises(RuntimeError, match=pattern):
            await app.activate_skill("review", thread_id=thread_id)
        with pytest.raises(RuntimeError, match=pattern):
            await app.deactivate_skill("review", thread_id=thread_id)
        with pytest.raises(RuntimeError, match=pattern):
            await app.runtime.compact(handle, context, thread_id=thread_id)
        with pytest.raises((RuntimeError, ValueError), match="等待审批|待执行|已停止"):
            await host.compact_thread(thread_id)
        after = await app.runtime.get_state(handle, thread_id)
        assert after.config == before.config
        assert after.next == before.next
        assert after.interrupts == before.interrupts
        assert (app.workspace / "sample.txt").read_text(encoding="utf-8") == "保持原样"
    finally:
        await host.aclose()
