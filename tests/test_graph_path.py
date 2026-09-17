"""图路径集成测试：fake 模型 + 真 SqliteSaver，钉住增量语义与中断全链。

核心断言（旧路径做不到、必须由图保证）：
- 第二轮只追加 [Human, AI]，历史不重复（thread 消息数 3 → 5）；
- ask 工具走 interrupt → handler 批准 → grant_once → 工具执行 → 内联 check 不再弹窗；
- 同一剧本下图路径与旧路径答案、镜像完全一致。
"""

from typing import Any, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from lib.agent import SAIAgent
from lib.core.permissions import PermissionRuntime, SessionPermissionState


class ScriptedModel(BaseChatModel):
    """按 script 出牌的 fake 模型（调用次数决定出哪张，耗尽后重复最后一张）。"""

    script: list = []
    calls_made: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        idx = min(self.calls_made, len(self.script) - 1)
        self.calls_made += 1
        return ChatResult(generations=[ChatGeneration(message=self.script[idx])])

    def bind_tools(self, tools, **kwargs):
        return self


@tool
def echo_tool(text: str) -> str:
    """回声工具。"""
    return f"echo:{text}"


def _isolated_permissions() -> PermissionRuntime:
    return PermissionRuntime(session=SessionPermissionState())


def _agent(tmp_path, script, **kwargs):
    model = ScriptedModel(script=list(script))
    return SAIAgent(
        model=model,
        workspace=tmp_path,
        tools=[echo_tool],
        checkpoint_path=str(tmp_path / "ckpt.sqlite3"),
        permissions=_isolated_permissions(),
        interrupt_handler=lambda payload: {"approved": True},
        **kwargs,
    )


def test_graph_turns_are_incremental_not_rebuilt(tmp_path):
    agent = _agent(tmp_path, [AIMessage(content="hi-1"), AIMessage(content="hi-2")])

    assert agent._graph_mode() is True
    assert agent.run("hello") == "hi-1"
    assert agent.runner.thread_message_count() == 3  # System + Human + AI

    assert agent.run("again") == "hi-2"
    # 增量：只多了本轮的 Human + AI。旧路径每次重传全量，这里必须恰好是 5。
    assert agent.runner.thread_message_count() == 5

    mirror = agent.session.get_messages(include_system=False)
    roles = [m["role"] for m in mirror]
    assert roles == ["user", "assistant", "user", "assistant"]


def test_graph_and_legacy_paths_answer_identically(tmp_path):
    script = [AIMessage(content="same-1"), AIMessage(content="same-2")]

    graph_agent = _agent(tmp_path / "g", script)

    def _legacy(tmp):
        model = ScriptedModel(script=list(script))
        return SAIAgent(
            model=model,
            workspace=tmp,
            tools=[echo_tool],
            permissions=_isolated_permissions(),
            interrupt_handler=lambda payload: {"approved": True},
        )

    legacy_agent = _legacy(tmp_path / "l")

    assert legacy_agent._graph_mode() is False
    assert graph_agent.run("hello") == legacy_agent.run("hello") == "same-1"
    assert graph_agent.run("again") == legacy_agent.run("again") == "same-2"

    graph_roles = [m["role"] for m in graph_agent.session.get_messages(include_system=False)]
    legacy_roles = [m["role"] for m in legacy_agent.session.get_messages(include_system=False)]
    assert graph_roles == legacy_roles == ["user", "assistant", "user", "assistant"]


def test_session_resumes_incrementally_in_a_new_process(tmp_path):
    """重启进程后同一 session 不再全量导入：thread 里有 3 条，接着只追加 2 条。

    这是 checkpointer 存在的核心理由；旧路径每次重传全量，这里必须恰好是 5。
    """
    from lib.core.session import SessionManager

    first = _agent(tmp_path, [AIMessage(content="hi-1")])
    assert first.run("hello") == "hi-1"
    assert first.runner.thread_message_count() == 3
    session_id = first.session.session_id
    first.runner.close()

    second = _agent(
        tmp_path,
        [AIMessage(content="hi-2")],
        session_manager=SessionManager(session_id=session_id),
    )
    assert second.run("again") == "hi-2"
    assert second.runner.thread_message_count() == 5


def test_ask_tool_flows_through_interrupt_and_runs_once(tmp_path):
    approvals = []

    def _handler(payload):
        approvals.append(payload)
        return {"approved": True}

    ask_call = AIMessage(
        content="",
        tool_calls=[{"name": "echo_tool", "args": {"text": "x"}, "id": "c1", "type": "tool_call"}],
    )
    agent = _agent(tmp_path, [ask_call, AIMessage(content="used-tool")])
    agent.interrupt_handler = _handler

    # 默认策略下未知工具是 ask：先确认前提，否则本测试是空转。
    assert agent._permissions_runtime.peek("echo_tool", {"text": "x"}).action == "ask"

    assert agent.run("use the tool") == "used-tool"
    assert len(approvals) == 1
    assert approvals[0]["tool"] == "echo_tool"
    assert approvals[0]["kind"] == "tool_ask"
    # 拒绝消息没有进历史（批准了，工具真跑了）。
    assert agent.runner.thread_message_count() == 5  # Sys,Human,AI(tool),Tool,AI


def test_interrupt_deny_short_circuits_tool(tmp_path):
    ask_call = AIMessage(
        content="",
        tool_calls=[{"name": "echo_tool", "args": {"text": "x"}, "id": "c1", "type": "tool_call"}],
    )
    agent = _agent(tmp_path, [ask_call, AIMessage(content="no-tool")])
    agent.interrupt_handler = lambda payload: {"approved": False}

    assert agent.run("use the tool") == "no-tool"
    contents = [
        str(m.content)
        for m in agent.runner.agent.get_state(agent.runner._thread_config()).values["messages"]
    ]
    assert any("Permission denied for tool" in c for c in contents)
    assert not any("echo:x" in c for c in contents)


def test_no_handler_means_fail_closed_deny(tmp_path):
    ask_call = AIMessage(
        content="",
        tool_calls=[{"name": "echo_tool", "args": {"text": "x"}, "id": "c1", "type": "tool_call"}],
    )
    agent = _agent(tmp_path, [ask_call, AIMessage(content="no-tool")])
    agent.interrupt_handler = None

    assert agent.run("use the tool") == "no-tool"
