"""截断续写与收尾保护的回归测试。

真实事故：ansible 任务最后一次模型调用 output_tokens=16000（撞 max_output_tokens），
但网关把 stop_reason 错报成 `tool_use`，截断续写因此没有触发，run 直接完成且
final response 只剩 "Now I'll implement the module."。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from sayacode.agent import AgentRuntime
from sayacode.agent.graph import CompletionGuardMiddleware, TruncationContinuationMiddleware
from sayacode.application import SayacodeApp
from sayacode.config import Config, ConfigRepository, Profile
from sayacode.paths import AppPaths
from tests.support import ContractModel


def make_ai(
    content: str = "partial",
    *,
    stop_reason: str | None = "end_turn",
    finish_reason: str | None = None,
    output_tokens: int | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> AIMessage:
    usage = None
    if output_tokens is not None:
        usage = {
            "input_tokens": 10,
            "output_tokens": output_tokens,
            "total_tokens": 10 + output_tokens,
        }
    metadata: dict[str, Any] = {"model_provider": "anthropic"}
    if stop_reason is not None:
        metadata["stop_reason"] = stop_reason
    if finish_reason is not None:
        metadata["finish_reason"] = finish_reason
    return AIMessage(
        content=content,
        usage_metadata=usage,
        response_metadata=metadata,
        tool_calls=tool_calls or [],
    )


async def evaluate(middleware: Any, messages: list[Any]) -> dict[str, Any] | None:
    return await middleware.aafter_model({"messages": messages}, None)


async def test_length_stop_reason_triggers_continuation() -> None:
    middleware = TruncationContinuationMiddleware(max_output_tokens=16_000)
    result = await evaluate(
        middleware, [HumanMessage(content="go"), make_ai(stop_reason="max_tokens")]
    )
    assert result is not None
    assert result["jump_to"] == "model"
    injected = result["messages"][0]
    assert injected.additional_kwargs["sayacode_continuation"] is True
    assert "Continue your previous answer" in injected.content


async def test_output_ceiling_triggers_even_when_gateway_reports_tool_use() -> None:
    """核心回归：网关把截断错报成 tool_use 时，仍按输出用量续写。"""
    middleware = TruncationContinuationMiddleware(max_output_tokens=16_000)
    answer = make_ai(
        "Now I'll implement the module.", stop_reason="tool_use", output_tokens=16_000
    )
    result = await evaluate(middleware, [HumanMessage(content="go"), answer])
    assert result is not None
    assert result["jump_to"] == "model"


async def test_openai_finish_reason_length_triggers_continuation() -> None:
    middleware = TruncationContinuationMiddleware(max_output_tokens=16_000)
    answer = make_ai(stop_reason=None, finish_reason="length", output_tokens=100)
    result = await evaluate(middleware, [HumanMessage(content="go"), answer])
    assert result is not None
    assert result["jump_to"] == "model"


async def test_normal_completion_does_not_continue() -> None:
    middleware = TruncationContinuationMiddleware(max_output_tokens=16_000)
    answer = make_ai("all done", stop_reason="end_turn", output_tokens=120)
    assert await evaluate(middleware, [HumanMessage(content="go"), answer]) is None


async def test_tool_call_payload_is_never_interrupted() -> None:
    middleware = TruncationContinuationMiddleware(max_output_tokens=16_000)
    answer = make_ai(
        "x",
        stop_reason="tool_use",
        output_tokens=16_000,
        tool_calls=[{"name": "read_file", "args": {"path": "a"}, "id": "c1", "type": "tool_call"}],
    )
    assert await evaluate(middleware, [HumanMessage(content="go"), answer]) is None


async def test_continuation_is_bounded() -> None:
    middleware = TruncationContinuationMiddleware(max_output_tokens=16_000, max_continuations=1)
    messages = [
        HumanMessage(content="go"),
        make_ai("p1", stop_reason="max_tokens"),
        HumanMessage(content="cont", additional_kwargs={"sayacode_continuation": True}),
        make_ai("p2", stop_reason="max_tokens"),
    ]
    assert await evaluate(middleware, messages) is None


def guard_messages(last_text: str = "Now let me verify...") -> list[Any]:
    return [
        HumanMessage(content="fix bug"),
        make_ai(
            "",
            tool_calls=[
                {"name": "read_file", "args": {"path": "a.py"}, "id": "c1", "type": "tool_call"}
            ],
        ),
        ToolMessage(content="file body", tool_call_id="c1", name="read_file"),
        make_ai(last_text, stop_reason="end_turn", output_tokens=130),
    ]


async def test_completion_guard_is_disabled_by_default() -> None:
    middleware = CompletionGuardMiddleware(max_output_tokens=16_000, max_nudges=0)
    assert await evaluate(middleware, guard_messages()) is None


async def test_completion_guard_nudges_once_after_tool_activity() -> None:
    middleware = CompletionGuardMiddleware(max_output_tokens=16_000)
    result = await evaluate(middleware, guard_messages())
    assert result is not None
    assert result["jump_to"] == "model"
    assert result["messages"][0].additional_kwargs["sayacode_continue_nudge"] is True


async def test_completion_guard_skips_when_no_tool_activity() -> None:
    middleware = CompletionGuardMiddleware(max_output_tokens=16_000)
    messages = [HumanMessage(content="hi"), make_ai("hello", output_tokens=10)]
    assert await evaluate(middleware, messages) is None


async def test_completion_guard_skips_after_write_activity() -> None:
    """本轮已发生写操作时，纯文本收尾视为「总结」而非「空转」，不再催促。"""
    middleware = CompletionGuardMiddleware(max_output_tokens=16_000)
    messages = [
        HumanMessage(content="fix"),
        make_ai(
            "",
            tool_calls=[
                {
                    "name": "write_file",
                    "args": {"path": "a.py", "content": "x"},
                    "id": "w1",
                    "type": "tool_call",
                }
            ],
        ),
        ToolMessage(content="ok", tool_call_id="w1", name="write_file"),
        make_ai("Done. Tests pass.", stop_reason="end_turn", output_tokens=40),
    ]
    assert await evaluate(middleware, messages) is None


async def test_completion_guard_still_nudges_after_read_only() -> None:
    """只读活动后纯文本收尾仍催促（说明可能没真正动手）。"""
    middleware = CompletionGuardMiddleware(max_output_tokens=16_000)
    assert await evaluate(middleware, guard_messages()) is not None


async def test_completion_guard_does_not_duplicate_truncation_continuation() -> None:
    middleware = CompletionGuardMiddleware(max_output_tokens=16_000)
    messages = guard_messages()
    messages[-1] = make_ai("partial", stop_reason="tool_use", output_tokens=16_000)
    assert await evaluate(middleware, messages) is None


async def test_completion_guard_nudges_only_once() -> None:
    middleware = CompletionGuardMiddleware(max_output_tokens=16_000)
    messages = [
        HumanMessage(content="fix"),
        make_ai(
            "",
            tool_calls=[
                {"name": "read_file", "args": {"path": "a"}, "id": "c1", "type": "tool_call"}
            ],
        ),
        ToolMessage(content="body", tool_call_id="c1", name="read_file"),
        HumanMessage(content="nudge", additional_kwargs={"sayacode_continue_nudge": True}),
        make_ai("still text", output_tokens=50),
    ]
    assert await evaluate(middleware, messages) is None


async def make_app(
    tmp_path: Path, model: ContractModel, *, completion_guard: bool
) -> SayacodeApp:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = AppPaths.resolve(tmp_path / "state")
    config = Config(
        default_profile="test",
        profiles={
            "test": Profile(
                name="test",
                protocol="openai_chat_completions",
                base_url="https://unused.test/v1",
                api_key="test-key",
                model_id="test",
                context_length=192_000,
                max_output_tokens=16_000,
                file_search=False,
                summary_trigger_ratio=None,
                summary_trigger_tokens=None,
                model_retries=0,
                tool_retries=0,
                completion_guard=completion_guard,
            )
        },
    )
    runtime = await AgentRuntime.open(paths.home)
    app = SayacodeApp(
        paths=paths,
        repository=ConfigRepository(paths.home),
        config=config,
        runtime=runtime,
        workspace=workspace,
        session_id="truncation-test",
        trust_level="ask",
        profile_name="test",
        model_override=model,
    )
    return await app.initialize()


async def test_graph_continues_after_truncated_final_answer(tmp_path: Path) -> None:
    model = ContractModel(
        responses=[
            make_ai(
                "Now I'll implement the module.",
                stop_reason="tool_use",
                output_tokens=16_000,
            ),
            AIMessage(content="Implemented and verified."),
        ]
    )
    app = await make_app(tmp_path, model, completion_guard=False)
    try:
        result = await app.run("实现模块")
        assert result["status"] == "completed"
        assert model.calls >= 2
        continuation = [
            message
            for message in model.received[-1]
            if isinstance(message, HumanMessage)
            and message.additional_kwargs.get("sayacode_continuation")
        ]
        assert continuation, "第二次模型调用应收到续写提示"
    finally:
        await app.aclose()


async def test_completion_guard_config_adds_one_extra_turn(tmp_path: Path) -> None:
    model = ContractModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_file",
                        "args": {"path": "a.py"},
                        "id": "c1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="Now let me verify..."),
            AIMessage(content="Done."),
        ]
    )
    app = await make_app(tmp_path, model, completion_guard=True)
    try:
        result = await app.run("修复问题")
        assert result["status"] == "completed"
        assert model.calls == 3
    finally:
        await app.aclose()
