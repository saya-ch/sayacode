"""只负责官方智能体图和中间件组装。"""

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
    ModelCallLimitMiddleware,
    ModelRetryMiddleware,
    ProviderToolSearchMiddleware,
    SummarizationMiddleware,
    TodoListMiddleware,
    ToolCallLimitMiddleware,
    ToolErrorMiddleware,
    ToolRetryMiddleware,
)
from langchain.agents.middleware.types import ModelRequest, hook_config
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.runtime import Runtime
from langgraph.store.sqlite.aio import AsyncSqliteStore

from ..config import Profile
from ..trust import READ_TOOLS
from .context import AgentContext, AgentHandle


class TruncationContinuationMiddleware(AgentMiddleware):
    """只有厂商明确报告截断时才续写。"""

    @hook_config(can_jump_to=["model"])
    async def aafter_model(
        self, state: Mapping[str, Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
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


class TaskNotificationMiddleware(AgentMiddleware):
    """给单次运行附加应用事件。不发用户消息。"""

    async def awrap_model_call(self, request: ModelRequest, handler: Any) -> Any:
        context = request.runtime.context if request.runtime is not None else None
        notice = getattr(context, "task_notification", None)
        if not notice:
            return await handler(request)
        system = request.system_message
        instructions = system.content if system is not None else ""
        return await handler(
            request.override(system_message=SystemMessage(content=f"{instructions}\n\n{notice}"))
        )


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
    """用所选中间件编译官方智能体图。

    动态工具和常驻工具注册在同一张图上。审批和工具中间件看到的是真实名称。调用方通过中断配置传入自身策略。
    """
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
                    sorted((READ_TOOLS - {"write_todos"}) | {"web_search"}),
                ),
            )
        )
    middleware.append(
        ToolErrorMiddleware(
            tool_error_handler
            or (lambda error, _request: f"Tool failed: {type(error).__name__}: {str(error)[:1000]}")
        )
    )
    if profile.max_model_calls is not None:
        middleware.append(TruncationContinuationMiddleware())
        middleware.append(
            ModelCallLimitMiddleware(run_limit=profile.max_model_calls, exit_behavior="error")
        )
    if profile.max_tool_calls is not None:
        middleware.append(ToolCallLimitMiddleware(run_limit=profile.max_tool_calls))
    if profile.summary_trigger_tokens is not None:
        model_profile = getattr(model, "profile", None)
        max_input_tokens = profile.context_length
        if isinstance(model_profile, Mapping) and isinstance(
            model_profile.get("max_input_tokens"), int
        ):
            max_input_tokens = min(max_input_tokens, model_profile["max_input_tokens"])
        summary_trigger = min(profile.summary_trigger_tokens, max(1, int(max_input_tokens * 0.75)))
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
    middleware.append(TodoListMiddleware())
    if profile.file_search:
        middleware.append(FilesystemFileSearchMiddleware(root_path=str(context.workspace)))
    if profile.tool_selector_max_tools is not None:
        middleware.append(
            LLMToolSelectorMiddleware(
                model=model,
                max_tools=profile.tool_selector_max_tools,
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
        middleware.append(HumanInTheLoopMiddleware(interrupt_on=dict(interrupt_on)))
    middleware.extend(extra_middleware)
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
