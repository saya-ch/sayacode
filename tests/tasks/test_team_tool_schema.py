"""委派保持可调用，服务商使用 JSON 工具结构。"""

from __future__ import annotations

from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage

from sayacode.tasks.tools import child_tools
from tests.support import contract_app


async def test_delegation_runtime_is_injected_outside_provider_schema(tmp_path) -> None:
    app = await contract_app(tmp_path)
    try:
        delegate = next(item for item in app._team_tools() if item.name == "delegate_to_subagent")
        schema = delegate.tool_call_schema.model_json_schema()
        assert set(schema["properties"]) == {"task", "role"}
        assert "runtime" not in schema["properties"]
    finally:
        await app.aclose()


async def test_child_can_report_a_finding_to_its_direct_parent(tmp_path) -> None:
    app = await contract_app(tmp_path)
    parent_lock = app._thread_lock(app.session_id)
    await parent_lock.acquire()
    try:
        record = await app._spawn_task(
            "inspect", role="reviewer", parent_thread_id=app.session_id
        )
        await app.tasks.wait(record.task_id)
        report = child_tools(app)[0]
        runtime = ToolRuntime(
            state={},
            context=app._context(
                record.thread_id,
                record.trust_level,
                workspace=app.workspace,
                task_id=record.task_id,
                background=True,
            ),
            config={},
            stream_writer=lambda _data: None,
            tool_call_id="report-1",
            store=app.runtime.store,
            tools=[report],
        )
        sent = await report.coroutine(message="The interface assumption changed", runtime=runtime)
        pending = await app.task_inbox.pending(app.session_id)
        assert any(
            item.message_id == sent["message_id"]
            and item.kind == "subagent_message"
            and "assumption changed" in item.content
            for item in pending
        )
    finally:
        parent_lock.release()
        await app.aclose()


async def test_delegation_captures_parent_goal_and_plan_without_sharing_thread_state(
    tmp_path,
) -> None:
    app = await contract_app(tmp_path)
    try:
        tools = app._team_tools()
        delegate = next(item for item in tools if item.name == "delegate_to_subagent")
        runtime = ToolRuntime(
            state={
                "messages": [HumanMessage(content="Implement the feature and verify it")],
                "todos": [
                    {"content": "Inspect design", "status": "completed"},
                    {"content": "Implement feature", "status": "in_progress"},
                ],
            },
            context=app._context(app.session_id, "ask"),
            config={},
            stream_writer=lambda _data: None,
            tool_call_id="delegate-1",
            store=app.runtime.store,
            tools=tools,
        )
        created = await delegate.coroutine(
            task="Implement the bounded component and return test evidence",
            role="planner",
            runtime=runtime,
        )
        record = await app.tasks.get(created["task_id"])
        assert record.thread_id != app.session_id
        assert record.context_snapshot is not None
        assert record.context_snapshot["user_goal"] == "Implement the feature and verify it"
        assert record.context_snapshot["parent_plan"][1]["status"] == "in_progress"
        await app.tasks.wait(record.task_id)
    finally:
        await app.aclose()
