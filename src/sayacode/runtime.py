"""异步智能体运行时。状态归图框架所有。

本模块只持有框架资源和少量应用元数据。不实现循环调度和对话历史。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping, Sequence, cast

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
from langchain.agents.middleware.types import hook_config
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphDrained
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import RunControl, Runtime
from langgraph.store.sqlite.aio import AsyncSqliteStore
from langgraph.types import Command

from .config import Profile
from .policy import READ_TOOLS

_KEYLESS_API_KEY = "sayacode-keyless-endpoint"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class AgentContext:
    """单次运行的依赖。由工具运行时注入。"""

    workspace: Path
    trust_level: str
    policy: Any
    output_dir: Path
    session_id: str
    task_id: str | None = None
    profile_name: str | None = None
    is_background: bool = False
    output_limit_bytes: int = 64 * 1024
    task_notification: str | None = None


@dataclass(frozen=True, slots=True)
class AgentHandle:
    """编译好的图。连同模型和摘要配置一起持有。"""

    graph: Any
    profile: Profile
    model: Any
    workspace: Path


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


class _ManagedEventStream:
    """跟随单路原生事件流。同步线程状态。"""

    def __init__(
        self,
        runtime: AgentRuntime,
        handle: AgentHandle,
        thread_id: str,
        raw: Any,
    ) -> None:
        self._runtime = runtime
        self._handle = handle
        self._thread_id = thread_id
        self._raw = raw

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    def __aiter__(self) -> AsyncIterator[Any]:
        return cast(AsyncIterator[Any], self._raw.__aiter__())

    async def __aenter__(self) -> _ManagedEventStream:
        await self._raw.__aenter__()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> Any:
        try:
            result = await self._raw.__aexit__(exc_type, exc, traceback)
        except BaseException as close_error:
            if isinstance(close_error, GraphDrained):
                status = "stopped"
            elif isinstance(close_error, asyncio.CancelledError):
                status = "interrupted"
            else:
                status = "error"
            await self._runtime.set_thread_status(self._thread_id, status)
            raise
        if exc_type is not None:
            if issubclass(exc_type, GraphDrained):
                status = "stopped"
            elif issubclass(exc_type, asyncio.CancelledError):
                status = "interrupted"
            else:
                status = "error"
        else:
            snapshot = await self._handle.graph.aget_state(
                self._runtime.thread_config(self._thread_id)
            )
            status = "interrupted" if snapshot.interrupts else "completed"
        await self._runtime.set_thread_status(self._thread_id, status)
        return result


class AgentRuntime:
    """持有本地运行所需的检查点和存储。各持一份。"""

    def __init__(
        self,
        root: Path,
        stack: AsyncExitStack,
        checkpointer: AsyncSqliteSaver,
        store: AsyncSqliteStore,
    ) -> None:
        self.root = root
        self.checkpointer = checkpointer
        self.store = store
        self._stack = stack
        self._closed = False

    @classmethod
    async def open(cls, config_root: str | Path) -> AgentRuntime:
        """在配置根目录下打开框架持久化文件。"""
        root = Path(config_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        stack = AsyncExitStack()
        try:
            saver = await stack.enter_async_context(
                AsyncSqliteSaver.from_conn_string(str(root / "checkpoints.sqlite3"))
            )
            await saver.setup()
            store = await stack.enter_async_context(
                AsyncSqliteStore.from_conn_string(str(root / "store.sqlite3"))
            )
            await store.setup()
            return cls(root, stack, saver, store)
        except BaseException:
            await stack.aclose()
            raise

    async def __aenter__(self) -> AgentRuntime:
        self._require_open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self._stack.aclose()

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("agent runtime is closed")

    @staticmethod
    def thread_config(thread_id: str) -> dict[str, Any]:
        if not thread_id:
            raise ValueError("thread_id is required")
        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _model_for(profile: Profile, override: Any = None) -> Any:
        if override is not None:
            return override
        options: dict[str, Any] = {"base_url": profile.base_url}
        if profile.protocol != "ollama_native_chat":
            # 否则会从环境读取厂商密钥。
            # 可能误发到用户自配的地址。
            options["api_key"] = profile.api_key or _KEYLESS_API_KEY
        if profile.protocol == "openai_chat_completions":
            adapter = "openai"
            options["use_responses_api"] = False
            options["max_tokens"] = profile.max_output_tokens
        elif profile.protocol == "openai_responses":
            adapter = "openai"
            options["use_responses_api"] = True
            options["max_tokens"] = profile.max_output_tokens
        elif profile.protocol == "anthropic_messages":
            adapter = "anthropic"
            options["max_tokens"] = profile.max_output_tokens
        elif profile.protocol == "gemini_generate_content":
            adapter = "google_genai"
            options["max_output_tokens"] = profile.max_output_tokens
            options["vertexai"] = False
        elif profile.protocol == "ollama_native_chat":
            adapter = "ollama"
            options["num_predict"] = profile.max_output_tokens
            options["num_ctx"] = profile.context_length
            options["client_kwargs"] = {
                "headers": {"Authorization": f"Bearer {profile.api_key or _KEYLESS_API_KEY}"}
            }
        else:
            raise ValueError(f"unsupported model protocol: {profile.protocol}")
        return init_chat_model(
            profile.model_id,
            model_provider=adapter,
            **options,
        )

    def build_agent(
        self,
        profile: Profile,
        tools: Sequence[Any],
        *,
        context: AgentContext,
        system_prompt: str = "",
        model_override: Any = None,
        additional_tools: Sequence[Any] = (),
        interrupt_on: Mapping[str, Any] | None = None,
        tool_error_handler: Callable[[Exception, Any], str | None] | None = None,
        extra_middleware: Sequence[Any] = (),
    ) -> AgentHandle:
        """用所选中间件编译官方智能体图。

        动态工具和常驻工具注册在同一张图上。审批和工具中间件看到的是真实名称。调用方通过中断配置传入自身策略。
        """
        self._require_open()
        model = self._model_for(profile, model_override)
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
                or (
                    lambda error, _request: (
                        f"Tool failed: {type(error).__name__}: {str(error)[:1000]}"
                    )
                )
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
            summary_trigger = min(
                profile.summary_trigger_tokens, max(1, int(max_input_tokens * 0.75))
            )
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
            searchable_tools: list[str | BaseTool] = [
                name for name in profile.native_tool_search_tools
            ]
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
            checkpointer=self.checkpointer,
            store=self.store,
        )
        return AgentHandle(graph=graph, profile=profile, model=model, workspace=context.workspace)

    async def put_thread(
        self,
        thread_id: str,
        context: AgentContext,
        *,
        status: str = "idle",
        title: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把图线程记入存储。供任务列表查询。"""
        self._require_open()
        prior = await self.get_thread(thread_id)
        item: dict[str, Any] = {
            **(prior or {}),
            "thread_id": thread_id,
            "workspace": str(context.workspace),
            "session_id": context.session_id,
            "task_id": context.task_id,
            "profile_name": context.profile_name,
            "trust_level": context.trust_level,
            "is_background": context.is_background,
            "status": status,
            "created_at": (prior or {}).get("created_at", _now()),
            "updated_at": _now(),
        }
        if title is not None:
            item["title"] = title
        if metadata is not None:
            item["metadata"] = dict(metadata)
        await self.store.aput(("threads",), thread_id, item, index=False)
        return item

    async def get_thread(self, thread_id: str) -> dict[str, Any] | None:
        self._require_open()
        item = await self.store.aget(("threads",), thread_id)
        return dict(item.value) if item is not None else None

    async def list_threads(
        self,
        *,
        workspace: Path | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self._require_open()
        if limit <= 0:
            return []
        # 存储按精确字段过滤。工作区路径在写入索引时已规范化。
        filters = {"workspace": str(workspace.resolve())} if workspace else None
        items = await self.store.asearch(("threads",), filter=filters, limit=limit)
        return sorted(
            (dict(item.value) for item in items),
            key=lambda v: v.get("updated_at", ""),
            reverse=True,
        )

    async def set_thread_status(self, thread_id: str, status: str) -> dict[str, Any]:
        self._require_open()
        prior = await self.get_thread(thread_id)
        if prior is None:
            raise KeyError(thread_id)
        item = {**prior, "status": status, "updated_at": _now()}
        await self.store.aput(("threads",), thread_id, item, index=False)
        return item

    async def invoke(
        self,
        handle: AgentHandle,
        context: AgentContext,
        message: str | None = None,
        *,
        thread_id: str | None = None,
        resume: Any = None,
        control: RunControl | None = None,
        durability: str = "sync",
        callbacks: Sequence[Any] = (),
        internal_trigger: bool = False,
    ) -> Any:
        """跑一轮对话。或处理内部触发和中断恢复。"""
        self._require_open()
        tid = thread_id or context.task_id or context.session_id
        payload = self._payload(message, resume, internal_trigger=internal_trigger)
        await self.put_thread(tid, context, status="running")
        try:
            config = self.thread_config(tid)
            if callbacks:
                config["callbacks"] = list(callbacks)
            result = await handle.graph.ainvoke(
                payload,
                config,
                context=context,
                control=control,
                durability=durability,
                version="v2",
            )
        except GraphDrained:
            await self.set_thread_status(tid, "stopped")
            raise
        except asyncio.CancelledError:
            await self.set_thread_status(tid, "interrupted")
            raise
        except Exception:
            await self.set_thread_status(tid, "error")
            raise
        await self.set_thread_status(tid, "interrupted" if result.interrupts else "completed")
        return result

    async def open_event_stream_v3(
        self,
        handle: AgentHandle,
        context: AgentContext,
        message: str | None = None,
        *,
        thread_id: str | None = None,
        resume: Any = None,
        control: RunControl | None = None,
        callbacks: Sequence[Any] = (),
    ) -> _ManagedEventStream:
        """返回原生事件流。支持官方投影选择。

        该实验接口保持原样。调用方直接选择所需投影。用作异步上下文管理器。退出时结算线程状态。
        """
        self._require_open()
        tid = thread_id or context.task_id or context.session_id
        await self.put_thread(tid, context, status="running")
        try:
            config = self.thread_config(tid)
            if callbacks:
                config["callbacks"] = list(callbacks)
            raw = await handle.graph.astream_events(
                self._payload(message, resume),
                config,
                context=context,
                control=control,
                version="v3",
            )
        except GraphDrained:
            await self.set_thread_status(tid, "stopped")
            raise
        except asyncio.CancelledError:
            await self.set_thread_status(tid, "interrupted")
            raise
        except Exception:
            await self.set_thread_status(tid, "error")
            raise
        return _ManagedEventStream(self, handle, tid, raw)

    @staticmethod
    def _payload(message: str | None, resume: Any, *, internal_trigger: bool = False) -> Any:
        if internal_trigger:
            if message is not None or resume is not None:
                raise ValueError("internal trigger cannot include a message or resume value")
            # 发起新的官方图运行。不伪造用户消息。
            # 调用方经由运行上下文传递事件。
            return {"messages": []}
        if resume is not None:
            if message is not None:
                raise ValueError("message and resume cannot be supplied together")
            return Command(resume=resume)
        if message is None:
            return None
        return {"messages": [{"role": "user", "content": message}]}

    async def resume(
        self,
        handle: AgentHandle,
        context: AgentContext,
        decision: Any,
        *,
        thread_id: str | None = None,
        control: RunControl | None = None,
        callbacks: Sequence[Any] = (),
    ) -> Any:
        """用调用方决定恢复人工确认中断。"""
        return await self.invoke(
            handle,
            context,
            thread_id=thread_id,
            resume=decision,
            control=control,
            callbacks=callbacks,
        )

    async def continue_run(
        self,
        handle: AgentHandle,
        context: AgentContext,
        *,
        thread_id: str | None = None,
        control: RunControl | None = None,
        callbacks: Sequence[Any] = (),
    ) -> Any:
        """从保存的超步边界继续被排空的图。"""
        return await self.invoke(
            handle,
            context,
            thread_id=thread_id,
            control=control,
            callbacks=callbacks,
        )

    async def get_state(self, handle: AgentHandle, thread_id: str) -> Any:
        self._require_open()
        return await handle.graph.aget_state(self.thread_config(thread_id))

    async def get_history(
        self,
        handle: AgentHandle,
        thread_id: str,
        *,
        limit: int = 100,
    ) -> list[Any]:
        self._require_open()
        return [
            snapshot
            async for snapshot in handle.graph.aget_state_history(
                self.thread_config(thread_id),
                limit=limit,
            )
        ]

    async def rewind(
        self,
        handle: AgentHandle,
        thread_id: str,
        checkpoint_id: str,
    ) -> dict[str, Any]:
        """在检查点处分叉存档状态。不执行工具。

        历史检查点会保留。返回配置指向新的分支头。后续调用由调用方显式决定。
        """
        self._require_open()
        selected = None
        async for snapshot in handle.graph.aget_state_history(self.thread_config(thread_id)):
            if snapshot.config.get("configurable", {}).get("checkpoint_id") == checkpoint_id:
                selected = snapshot
                break
        if selected is None:
            raise KeyError(f"checkpoint not found: {checkpoint_id}")
        values = dict(selected.values)
        if "messages" in values:
            values["messages"] = [
                RemoveMessage(id=REMOVE_ALL_MESSAGES),
                *values["messages"],
            ]
        fork = await handle.graph.aupdate_state(selected.config, values)
        await self.set_thread_status(thread_id, "rewound")
        return cast(dict[str, Any], fork)

    async def compact(
        self,
        handle: AgentHandle,
        context: AgentContext,
        *,
        thread_id: str | None = None,
        focus: str | None = None,
    ) -> bool:
        """强制走官方摘要。把更新写回图状态。"""
        self._require_open()
        tid = thread_id or context.task_id or context.session_id
        config = self.thread_config(tid)
        indexed = await self.get_thread(tid)
        if indexed is not None and indexed.get("status") == "running":
            raise RuntimeError("cannot compact while the graph is running")
        snapshot = await handle.graph.aget_state(config)
        if snapshot.next or snapshot.interrupts:
            raise RuntimeError("cannot compact while the graph has pending work or an interrupt")
        messages = list(snapshot.values.get("messages", ())) if snapshot.values else []
        if len(messages) < 2:
            return False
        keep = max(1, min(handle.profile.summary_keep_messages, len(messages) // 2))
        options: dict[str, Any] = {}
        if focus:
            from langchain.agents.middleware.summarization import DEFAULT_SUMMARY_PROMPT

            escaped_focus = focus.replace("{", "{{").replace("}", "}}")
            options["summary_prompt"] = (
                f"<user_focus>Preserve details relevant to: {escaped_focus}</user_focus>\n"
                + DEFAULT_SUMMARY_PROMPT
            )
        middleware = SummarizationMiddleware(
            model=handle.model,
            trigger=("messages", 1),
            keep=("messages", keep),
            **options,
        )
        update = await middleware.abefore_model(
            snapshot.values,
            Runtime[None](store=self.store),
        )
        if not update:
            return False
        await handle.graph.aupdate_state(config, update)
        return True


__all__ = ["AgentContext", "AgentHandle", "AgentRuntime", "RunControl"]
