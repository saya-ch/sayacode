"""组装官方智能体图的唯一工厂。

本模块只做组装，不跑对话，不存状态。
中间件按固定顺序叠加，调用方只需调一次工厂。"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence, cast

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ClearToolUsesEdit,
    ContextEditingMiddleware,
    FilesystemFileSearchMiddleware,
    HumanInTheLoopMiddleware,
    LLMToolSelectorMiddleware,
    ModelRetryMiddleware,
    ProviderToolSearchMiddleware,
    SummarizationMiddleware,
    TodoListMiddleware,
    ToolErrorMiddleware,
    ToolRetryMiddleware,
)
from langchain.agents.middleware.todo import (
    WRITE_TODOS_SYSTEM_PROMPT,
    WRITE_TODOS_TOOL_DESCRIPTION,
)
from langchain.agents.middleware.types import hook_config
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.runtime import Runtime
from langgraph.store.sqlite.aio import AsyncSqliteStore

from ..approvals import QUERY_TOOLS
from ..config import Profile
from .context import AgentContext, AgentHandle

_SAYACODE_PLANNING_PROMPT = f"""{WRITE_TODOS_SYSTEM_PROMPT}

## SAYACODE planning and asynchronous collaboration

- Skip the todo list for a simple answer or one obvious action. Use it when work has multiple
  verifiable stages, the user asks for a plan, or delegation needs coordination.
- Treat the todo list as the single plan for this thread. Do not maintain a second plan in prose.
- Write outcome-oriented todos with observable completion evidence, not narration of every tool call.
- After a failed verification, a changed user requirement, or a relevant background-task result,
  reconsider the remaining todos and call `write_todos` when the plan should change.
- Background tasks are asynchronous. After delegation, continue any independent work instead of
  waiting by default. Use task status or wait tools only when their result blocks the next action.
- When delegating, give the child a bounded objective, relevant context, constraints, and concrete
  acceptance evidence. A child completion is evidence, not automatic acceptance of a parent todo.
- Before claiming completion, inspect relevant child results and delivery state, verify the
  integrated outcome, then mark the corresponding todos complete.
"""

_SAYACODE_TODO_DESCRIPTION = f"""{WRITE_TODOS_TOOL_DESCRIPTION}

Update this plan when execution evidence or asynchronous child results invalidate future work.
"""


def summary_trigger_for(profile: Profile, model: Any) -> int | None:
    """按上下文窗口、输出预算和安全余量计算自动摘要阈值。"""
    if profile.summary_trigger_ratio is None and profile.summary_trigger_tokens is None:
        return None
    context_window = profile.context_length
    configured_input = max(1, context_window - profile.max_output_tokens)
    model_profile = getattr(model, "profile", None)
    provider_input = None
    if isinstance(model_profile, Mapping) and isinstance(
        model_profile.get("max_input_tokens"), int
    ):
        provider_input = int(model_profile["max_input_tokens"])
    input_limit = min(configured_input, provider_input) if provider_input else configured_input
    margin = min(
        max(2_048, int(context_window * 0.05)),
        max(1, int(input_limit * 0.10)),
    )
    reserved_limit = max(1, input_limit - margin)
    candidates = [reserved_limit]
    if profile.summary_trigger_ratio is not None:
        candidates.append(max(1, int(context_window * profile.summary_trigger_ratio)))
    if profile.summary_trigger_tokens is not None:
        candidates.append(profile.summary_trigger_tokens)
    return min(candidates)


class TruncationContinuationMiddleware(AgentMiddleware):
    """截断时自动续写，厂商未报截断时不动作。

    只看最后一条模型消息的结束原因。
    有工具调用时不续写，避免打断工具链。"""

    @hook_config(can_jump_to=["model"])
    async def aafter_model(
        self, state: Mapping[str, Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        """判断是否截断，决定是否跳回模型继续生成。

        参数是当前图状态和运行时，返回续写消息加跳转指令，无截断时返回空。
        只有结束原因为长度受限时才续写，调用约束是必须在限次中间件之前装配。"""
        # 先确认最后一条是无工具调用的模型消息。
        # 再读厂商结束原因，非长度截断直接放行。
        messages = state.get("messages", [])
        if not messages or not isinstance(messages[-1], AIMessage):
            return None
        answer = messages[-1]
        if answer.tool_calls:
            return None
        metadata = answer.response_metadata
        reason = metadata.get("finish_reason") or metadata.get("stop_reason")
        if str(reason).lower() not in {"length", "max_tokens", "max_output_tokens"}:
            return None
        return {
            "messages": [
                HumanMessage(
                    content="Continue your previous answer from the exact stopping point. "
                    "Do not repeat prior text.",
                    additional_kwargs={"sayacode_continuation": True},
                )
            ],
            "jump_to": "model",
        }


def build_graph(
    checkpointer: AsyncSqliteSaver,
    store: AsyncSqliteStore,
    model: Any,
    profile: Profile,
    tools: Sequence[Any],
    *,
    context: AgentContext,
    system_prompt: str = "",
    additional_tools: Sequence[Any] = (),
    interrupt_on: Mapping[str, Any] | None = None,
    tool_error_handler: Callable[[Exception, Any], str | None] | None = None,
    extra_middleware: Sequence[Any] = (),
) -> AgentHandle:
    """用固定顺序装配官方智能体图并返回句柄。

    参数是持久化组件加模型加画像加工具集，返回带画像和工作区的图句柄。
    调用约束是同一张图同时注册常驻和动态工具，中断策略由调用方传入。
    坑点是顺序不可乱，重试在前，限次居中，摘要和整理在后，审批永远靠后。

    组装分四段，先加失败兜底，再加模型调用预算和截断续写。
    然后加记忆整理和待办文件检索与工具选择，最后加审批和外部扩展。
    统一编译后返回句柄，运行时不再改结构。"""
    # 第一段放重试和错误转述，保证失败先有兜底。
    # 只读工具可重试，写工具不自动重试，避免重复副作用。
    middleware: list[Any] = []
    if profile.model_retries:
        middleware.append(
            ModelRetryMiddleware(max_retries=profile.model_retries, on_failure="error")
        )
    if profile.tool_retries:
        middleware.append(
            ToolRetryMiddleware(
                max_retries=profile.tool_retries,
                tools=cast(
                    list[BaseTool | str],
                    sorted(QUERY_TOOLS),
                ),
            )
        )
    middleware.append(
        ToolErrorMiddleware(
            tool_error_handler
            or (lambda error, _request: f"Tool failed: {type(error).__name__}: {str(error)[:1000]}")
        )
    )
    # 截断续写紧贴模型调用链，继续生成不依赖产品层计数器。
    middleware.append(TruncationContinuationMiddleware())
    summary_trigger = summary_trigger_for(profile, model)
    if summary_trigger is not None:
        # 第三段放记忆整理，动态阈值为窗口的八成且为输出和估算误差留空间。
        middleware.append(
            SummarizationMiddleware(
                model=model,
                trigger=("tokens", summary_trigger),
                keep=("messages", profile.summary_keep_messages),
            )
        )
    if profile.context_edit_trigger is not None:
        middleware.append(
            ContextEditingMiddleware(
                edits=[ClearToolUsesEdit(trigger=profile.context_edit_trigger)]
            )
        )
    middleware.append(
        TodoListMiddleware(
            system_prompt=_SAYACODE_PLANNING_PROMPT,
            tool_description=_SAYACODE_TODO_DESCRIPTION,
        )
    )
    # 待办默认常开，文件检索和工具选择按画像开关。
    # 选择器失败时放行全部工具，避免无工具可用。
    if profile.file_search:
        middleware.append(FilesystemFileSearchMiddleware(root_path=str(context.workspace)))
    if profile.tool_selector_max_tools is not None:
        skill_names = {"list_skills", "load_skill", "read_skill_resource", "search_memory"}
        middleware.append(
            LLMToolSelectorMiddleware(
                model=model,
                max_tools=profile.tool_selector_max_tools,
                always_include=sorted(
                    skill_names
                    & {getattr(item, "name", "") for item in [*tools, *additional_tools]}
                ),
                max_retries=0,
                on_parsing_failure="all",
            )
        )
    if profile.native_tool_search_tools:
        searchable_tools: list[str | BaseTool] = [name for name in profile.native_tool_search_tools]
        middleware.append(
            ProviderToolSearchMiddleware(
                searchable_tools=searchable_tools,
            )
        )
    if interrupt_on:
        # 第四段放人工审批，必须在链尾才能看到真实工具名。
        # 外部扩展排在审批之后，调用方自担顺序风险。
        middleware.append(HumanInTheLoopMiddleware(interrupt_on=dict(interrupt_on)))
    middleware.extend(extra_middleware)
    # 单工厂统一编译，上下文结构固定为运行上下文。
    graph = create_agent(
        model=model,
        tools=[*tools, *additional_tools],
        system_prompt=system_prompt or None,
        middleware=middleware,
        context_schema=AgentContext,
        checkpointer=checkpointer,
        store=store,
    )
    return AgentHandle(graph=graph, profile=profile, model=model, workspace=context.workspace)
