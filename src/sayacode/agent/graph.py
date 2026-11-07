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
    HumanInTheLoopMiddleware,
    LLMToolSelectorMiddleware,
    ModelRetryMiddleware,
    ProviderToolSearchMiddleware,
    TodoListMiddleware,
    ToolErrorMiddleware,
    ToolRetryMiddleware,
)
from langchain.agents.middleware.todo import (
    WRITE_TODOS_SYSTEM_PROMPT,
    WRITE_TODOS_TOOL_DESCRIPTION,
)
from langchain.agents.middleware.types import hook_config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.runtime import Runtime
from langgraph.store.sqlite.aio import AsyncSqliteStore

from ..approvals import QUERY_TOOLS
from ..config import Profile
from ..tools.search import WorkspaceFileSearchMiddleware
from .context import AgentContext, AgentHandle
from .summary import ResilientSummarizationMiddleware

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


_LENGTH_FINISH_REASONS = frozenset(
    {"length", "max_tokens", "max_output_tokens", "max_completion_tokens"}
)
_CONTINUATION_KEY = "sayacode_continuation"
_NUDGE_KEY = "sayacode_continue_nudge"
_CONTINUATION_PROMPT = (
    "Continue your previous answer from the exact stopping point. Do not repeat prior text."
)
_NUDGE_PROMPT = (
    "If any part of the task is still incomplete, continue and verify it; "
    "otherwise give your final answer."
)


def _finish_reason(metadata: Mapping[str, Any]) -> str:
    """把厂商结束原因收敛成统一小写值，缺失时返回空串。"""
    for key in ("finish_reason", "stop_reason"):
        value = metadata.get(key)
        if value is not None:
            return str(value).strip().lower()
    return ""


def _injected_count(messages: Sequence[Any], marker: str) -> int:
    """统计最近一次普通用户消息之后注入的指定标记消息数。"""
    count = 0
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            if message.additional_kwargs.get(marker):
                count += 1
            else:
                break
    return count


class TruncationContinuationMiddleware(AgentMiddleware):
    """截断时自动续写，厂商未报截断时按输出用量兜底。

    只看最后一条模型消息的结束原因和用量。
    有工具调用时不续写，避免打断工具链。
    真实试验中厂商把截断错报成 `tool_use`，因此输出用量达到上限且无工具调用
    同样判定为截断。"""

    def __init__(
        self,
        *,
        max_output_tokens: int | None = None,
        max_continuations: int = 3,
    ) -> None:
        super().__init__()
        self.max_output_tokens = max_output_tokens
        self.max_continuations = max(1, max_continuations)

    @hook_config(can_jump_to=["model"])
    async def aafter_model(
        self, state: Mapping[str, Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        """判断是否截断，决定是否跳回模型继续生成。

        参数是当前图状态和运行时，返回续写消息加跳转指令，无截断时返回空。
        只在最后一条无工具调用的模型消息被判定截断且未达续写上限时动作。"""
        messages = state.get("messages", [])
        if not messages or not isinstance(messages[-1], AIMessage):
            return None
        answer = messages[-1]
        if answer.tool_calls:
            return None
        if not self._truncated(answer):
            return None
        if _injected_count(messages, _CONTINUATION_KEY) >= self.max_continuations:
            return None
        return {
            "messages": [
                HumanMessage(
                    content=_CONTINUATION_PROMPT,
                    additional_kwargs={_CONTINUATION_KEY: True},
                )
            ],
            "jump_to": "model",
        }

    def _truncated(self, answer: AIMessage) -> bool:
        """判断最后一条模型消息是否被输出上限截断。

        参数是模型消息，返回是否截断。
        厂商结束原因命中长度类值即截断；否则看输出用量是否达到画像上限，
        覆盖厂商把截断错报成 `tool_use` 或 `end_turn` 的情况。"""
        if _finish_reason(answer.response_metadata) in _LENGTH_FINISH_REASONS:
            return True
        if not self.max_output_tokens:
            return False
        usage: Mapping[str, Any] = answer.usage_metadata or {}
        output_tokens = usage.get("output_tokens")
        return isinstance(output_tokens, int) and output_tokens >= self.max_output_tokens


class CompletionGuardMiddleware(AgentMiddleware):
    """纯文本收尾时补一次继续执行提示，默认关闭。

    只处理「本轮已用过工具、却只回一段文本就结束」的情况。
    截断响应交给截断续写中间件，不重复注入。
    这是行为改变，由画像显式开启；关闭时完全不动作。"""

    def __init__(
        self,
        *,
        max_output_tokens: int | None = None,
        max_nudges: int = 1,
    ) -> None:
        super().__init__()
        self.max_output_tokens = max_output_tokens
        self.max_nudges = max(0, max_nudges)

    @hook_config(can_jump_to=["model"])
    async def aafter_model(
        self, state: Mapping[str, Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        """判断是否提前收尾，决定是否补一次继续执行提示。

        参数是当前图状态和运行时，返回提示消息加跳转指令，否则返回空。
        只在未提示过、本轮确有工具活动、且非截断时动作一次。"""
        if self.max_nudges == 0:
            return None
        messages = state.get("messages", [])
        if not messages or not isinstance(messages[-1], AIMessage):
            return None
        answer = messages[-1]
        if answer.tool_calls or self._truncated(answer):
            return None
        if _injected_count(messages, _NUDGE_KEY) >= self.max_nudges:
            return None
        if not self._has_tool_activity(messages):
            return None
        if self._has_write_activity(messages):
            # 本轮已经发生过写操作（write_file/search_replace/delete_file），
            # 说明模型确实动过手，此时纯文本收尾更可能是「总结」而非「空转」，
            # 不再催促，避免把已完成的任务逼着继续跑（成本翻倍）。
            return None
        return {
            "messages": [
                HumanMessage(
                    content=_NUDGE_PROMPT,
                    additional_kwargs={_NUDGE_KEY: True},
                )
            ],
            "jump_to": "model",
        }

    def _truncated(self, answer: AIMessage) -> bool:
        """与截断中间件同口径判断截断，避免重复注入。"""
        if _finish_reason(answer.response_metadata) in _LENGTH_FINISH_REASONS:
            return True
        if not self.max_output_tokens:
            return False
        usage: Mapping[str, Any] = answer.usage_metadata or {}
        output_tokens = usage.get("output_tokens")
        return isinstance(output_tokens, int) and output_tokens >= self.max_output_tokens

    @staticmethod
    def _has_tool_activity(messages: Sequence[Any]) -> bool:
        """判断最近一轮用户请求之后是否出现过工具结果消息。"""
        for message in reversed(messages):
            if isinstance(message, ToolMessage):
                return True
            if isinstance(message, HumanMessage) and not (
                message.additional_kwargs.get(_CONTINUATION_KEY)
                or message.additional_kwargs.get(_NUDGE_KEY)
            ):
                break
        return False

    _WRITE_TOOLS = frozenset({"write_file", "search_replace", "delete_file"})
    @classmethod
    def _has_write_activity(cls, messages: Sequence[Any]) -> bool:
        """判断本轮（最近一次真人请求之后）是否出现过写操作工具调用。

        写操作 = write_file / search_replace / delete_file。只读工具（read_file、
        list_directory、grep、execute_command_tool 等）不算——只读之后纯文本收尾
        才更可能是「没真正动手」，值得催促。"""
        for message in reversed(messages):
            if isinstance(message, HumanMessage) and not (
                message.additional_kwargs.get(_CONTINUATION_KEY)
                or message.additional_kwargs.get(_NUDGE_KEY)
            ):
                break
            if isinstance(message, AIMessage):
                for call in message.tool_calls or []:
                    name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
                    if name in cls._WRITE_TOOLS:
                        return True
        return False


#: 只读类工具：只收集信息，不产生产物。
_EXPLORE_TOOLS = frozenset(
    {"read_file", "list_directory", "grep_search", "glob_search", "analyze_project"}
)

_EXPLORE_BUDGET_KEY = "sayacode_explore_budget"

_EXPLORE_BUDGET_PROMPT = (
    "你已经连续调用了 {count} 次只读工具（读文件 / 搜索 / 列目录），但还没有修改任何文件。\n"
    "在长上下文任务里这个模式高度预示失败：有用的信息已经收集到了，继续读只会把它们稀释。\n\n"
    "这一轮请直接动手：\n"
    "1. 如果已经定位到需要改的文件，现在就做出修改；\n"
    "2. 如果确实还缺信息，请**明确说出缺什么**，然后只做一次最有针对性的读取。\n"
    "不要再来一轮大范围搜索。"
)


class ExploreBudgetMiddleware(AgentMiddleware):
    """连续只读不写超过阈值时提醒模型落笔，默认关闭。

    动机（2026-11-07 实测）：长上下文 A/B 里 12 个 trial 中，
    **2 个从头到尾一次编辑都没有**（46 次 / 13 次全只读后静默结束，产 0 字节 patch），
    另外 3 个到第 29–42 个动作才首次落笔；而通过的 trial 通常在前 10 个动作内就开始改。
    失败组的模型调用中位数是通过组的 3.7 倍。

    这不是「不会做」（同样 6 题在短上下文下 12/12 全过），而是**缺少收敛机制**。
    这里是行为改变，由画像显式开启；关闭时完全不动作。"""

    def __init__(self, *, read_budget: int = 12, max_nudges: int = 2) -> None:
        super().__init__()
        self.read_budget = max(1, read_budget)
        self.max_nudges = max(0, max_nudges)

    @hook_config(can_jump_to=["model"])
    async def aafter_model(
        self, state: Mapping[str, Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        """连续只读达到预算且尚未写过文件时，补一次「该动手了」的提示。

        参数是当前图状态与运行时，返回提示消息加跳转指令，否则返回空。
        只在本轮真人请求之后、且没发生过写操作时动作，最多 nudge ``max_nudges`` 次。"""
        if self.max_nudges == 0:
            return None
        messages = state.get("messages", [])
        if not messages or not isinstance(messages[-1], AIMessage):
            return None
        count, wrote = self._scan_since_human(messages)
        if wrote or count < self.read_budget:
            return None
        if _injected_count(messages, _EXPLORE_BUDGET_KEY) >= self.max_nudges:
            return None
        return {
            "messages": [
                HumanMessage(
                    content=_EXPLORE_BUDGET_PROMPT.format(count=count),
                    additional_kwargs={_EXPLORE_BUDGET_KEY: True},
                )
            ],
            "jump_to": "model",
        }

    @classmethod
    def _scan_since_human(cls, messages: Sequence[Any]) -> tuple[int, bool]:
        """统计本轮只读调用数，并判断是否已经写过文件。"""
        count = 0
        wrote = False
        for message in reversed(messages):
            if isinstance(message, HumanMessage) and not (
                message.additional_kwargs.get(_CONTINUATION_KEY)
                or message.additional_kwargs.get(_NUDGE_KEY)
                or message.additional_kwargs.get(_EXPLORE_BUDGET_KEY)
            ):
                break
            if isinstance(message, AIMessage):
                for call in message.tool_calls or []:
                    name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
                    if name in CompletionGuardMiddleware._WRITE_TOOLS:
                        wrote = True
                    elif name in _EXPLORE_TOOLS:
                        count += 1
        return count, wrote


def build_graph(
    checkpointer: AsyncSqliteSaver,
    store: AsyncSqliteStore,
    model: Any,
    profile: Profile,
    tools: Sequence[Any],
    *,    context: AgentContext,
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
    middleware.append(
        TruncationContinuationMiddleware(max_output_tokens=profile.max_output_tokens)
    )
    if profile.completion_guard:
        # 提前收尾保护是行为改变，默认关闭，由画像显式开启。
        middleware.append(
            CompletionGuardMiddleware(max_output_tokens=profile.max_output_tokens)
        )
    if profile.explore_read_budget is not None:
        # 探索预算护栏：长上下文下「只读不写」是失败的强前兆，达到预算时提醒落笔。
        # 同样是行为改变，默认关闭（None），由画像显式开启。
        middleware.append(ExploreBudgetMiddleware(read_budget=profile.explore_read_budget))
    summary_trigger = summary_trigger_for(profile, model)
    if summary_trigger is not None:
        # 第三段放记忆整理，动态阈值为窗口的八成且为输出和估算误差留空间。
        middleware.append(
            ResilientSummarizationMiddleware(
                model=model,
                trigger=("tokens", summary_trigger),
                keep=("messages", profile.summary_keep_messages),
            )
        )
    if profile.context_edit_trigger is not None:
        middleware.append(
            ContextEditingMiddleware(
                edits=[
                    ClearToolUsesEdit(
                        trigger=profile.context_edit_trigger,
                        keep=profile.context_edit_keep,
                    )
                ]
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
        middleware.append(WorkspaceFileSearchMiddleware(root_path=str(context.workspace)))
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
