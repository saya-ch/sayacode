# 原生中间件升级：上下文剪枝与限额护栏均用 LangChain 自带实现。

from typing import Any, Optional

from langchain.agents.middleware import (
    ContextEditingMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from lib.agent import SAIAgent
from lib.core.middleware import (
    CONTEXT_PRUNE_KEEP,
    CONTEXT_PRUNE_RATIO,
    MAX_MODEL_CALLS_PER_RUN,
    MAX_TOOL_CALLS_PER_RUN,
    build_context_editing_middleware,
    build_guardrail_middlewares,
)
from lib.core.permissions import PermissionRuntime, SessionPermissionState


class RecordingModel(BaseChatModel):
    """按剧本出牌，并记下每次模型调用看到的消息。"""

    script: list = []
    calls_made: int = 0
    context_window: int = 0
    seen: list = []

    @property
    def _llm_type(self) -> str:
        return "recording"

    def _generate(self, messages: list[BaseMessage], stop: Optional[list[str]] = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None, **kwargs: Any) -> ChatResult:
        self.seen.append(list(messages))
        idx = min(self.calls_made, len(self.script) - 1)
        self.calls_made += 1
        return ChatResult(generations=[ChatGeneration(message=self.script[idx])])

    def bind_tools(self, tools, **kwargs):
        return self


@tool
def big_tool(chunk: int) -> str:
    """返回一大段填充文本。"""
    return ("X" * 3000) + f"-{chunk}"


def _agent(tmp_path, script, context_window=0):
    return SAIAgent(
        model=RecordingModel(script=list(script), context_window=context_window),
        workspace=tmp_path,
        tools=[big_tool],
        checkpoint_path=str(tmp_path / "ckpt.sqlite3"),
        permissions=PermissionRuntime(session=SessionPermissionState()),
        interrupt_handler=lambda payload: {"approved": True},
    )


class TestContextEditingFactory:
    def test_unknown_limit_disables_pruning(self):
        assert build_context_editing_middleware(0) is None
        assert build_context_editing_middleware(-1) is None

    def test_trigger_follows_preventive_ratio(self):
        middleware = build_context_editing_middleware(100_000)
        assert isinstance(middleware, ContextEditingMiddleware)
        edits = list(middleware.edits)
        assert len(edits) == 1
        assert edits[0].trigger == int(100_000 * CONTEXT_PRUNE_RATIO)
        assert edits[0].keep == CONTEXT_PRUNE_KEEP

    def test_tiny_limit_floors_trigger(self):
        edits = list(build_context_editing_middleware(100).edits)
        assert edits[0].trigger == 1000


class TestGuardrails:
    def test_only_run_limits_are_set(self):
        tool_middleware, model_middleware = build_guardrail_middlewares()
        assert isinstance(tool_middleware, ToolCallLimitMiddleware)
        assert isinstance(model_middleware, ModelCallLimitMiddleware)
        # thread 级计数跨整个会话累积，长会话必然误伤：只允许 run_limit。
        assert tool_middleware.thread_limit is None
        assert model_middleware.thread_limit is None
        assert tool_middleware.run_limit == MAX_TOOL_CALLS_PER_RUN
        assert model_middleware.run_limit == MAX_MODEL_CALLS_PER_RUN
        assert model_middleware.exit_behavior == "end"

    def test_context_limit_reaches_the_builder(self, tmp_path, monkeypatch):
        import lib.core.middleware as mw

        seen = {}
        real = mw.build_context_editing_middleware

        def spy(limit):
            seen["limit"] = limit
            return real(limit)

        monkeypatch.setattr(mw, "build_context_editing_middleware", spy)
        agent = _agent(tmp_path, [AIMessage(content="hi")], context_window=4000)
        agent.run("go")
        assert seen["limit"] == 4000


class TestToolOutputPruning:
    def test_old_tool_outputs_are_cleared(self, tmp_path):
        script = [
            AIMessage(content="", tool_calls=[{
                "name": "big_tool", "args": {"chunk": i}, "id": f"c{i}", "type": "tool_call",
            }])
            for i in range(10)
        ] + [AIMessage(content="done")]
        agent = _agent(tmp_path, script, context_window=4000)
        agent.run("go")

        latest = agent.model.seen[-1]
        contents = [str(getattr(message, "content", "")) for message in latest]
        assert any("[cleared]" in text for text in contents)
        # 最近一条工具结果必须留下：剪枝只动旧的。
        tool_contents = [text for text in contents if text.startswith("X")]
        assert any("-9" in text for text in tool_contents)

class TestRunawayGuardrail:
    def test_endless_tool_loop_is_cut_off(self, tmp_path):
        """脚本模型永不停手：单轮模型调用上限必须结束本轮，而不是转到底。"""
        forever = AIMessage(content="", tool_calls=[{
            "name": "big_tool", "args": {"chunk": 1}, "id": "loop", "type": "tool_call",
        }])
        agent = _agent(tmp_path, [forever], context_window=0)
        out = agent.run("go")
        assert isinstance(out, str)
        assert agent.model.calls_made <= MAX_MODEL_CALLS_PER_RUN + 1
