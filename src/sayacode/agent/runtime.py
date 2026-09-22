"""异步智能体运行时。状态归图框架所有。

本模块只持有框架资源和少量应用元数据。不实现循环调度和对话历史。
两种执行模式共用同一张图，一次拿结果和流式取增量按需二选一。"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping, Sequence, cast

from langchain.agents.middleware import (
    SummarizationMiddleware,
)
from langchain_core.messages import RemoveMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import GraphDrained
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import RunControl, Runtime
from langgraph.store.sqlite.aio import AsyncSqliteStore
from langgraph.types import Command

from ..config import Profile
from . import models
from .context import AgentContext, AgentHandle
from .graph import build_graph


def _now() -> str:
    """取当前协调世界时的文本形式，用于线程索引的时间戳。"""
    return datetime.now(timezone.utc).isoformat()


class _ManagedEventStream:
    """跟随单路原生事件流。退出时按结果同步线程状态。"""

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
        """关闭流并结算线程状态，正常结束看是否有中断。

        分三步，先关底层流，再判退出原因，最后写回线程状态。
        排空记为停止，取消记为中断，异常记为失败，无异常看快照有无中断。
        坑点是结算失败会覆盖原异常，调用方应把流当上下文管理器使用。"""
        # 先让底层流收尾，关闭期异常同样要落状态。
        # 再区分正常退出和异常退出，正常退出以图快照为准。
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
    """持有本地运行所需的检查点和存储。各持一份。

    打开后复用到底，关闭后不可再用。
    建图只拼装不执行，执行走一次拿结果或流式两种模式。"""

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
        """在配置根目录下打开框架持久化文件。

        参数是配置根目录，返回已建好检查点和存储的运行时。
        调用约束是目录不存在会自动创建，失败时已开资源会回滚关闭。
        坑点是同一目录不宜多实例并发写，调用方应复用单例。"""
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
        """关闭底层资源，重复调用安全无副作用。

        无参数无返回，关闭后所有方法拒绝服务。
        坑点是关闭后不可重开，需要重开应新建实例。"""
        if not self._closed:
            self._closed = True
            await self._stack.aclose()

    def _require_open(self) -> None:
        """运行前检查是否已关闭，关闭后直接抛错。"""
        if self._closed:
            raise RuntimeError("agent runtime is closed")

    @staticmethod
    def thread_config(thread_id: str) -> dict[str, Any]:
        """按线程标识拼装图调用所需的配置字典。

        参数是非空线程标识，返回带线程标识的配置。
        坑点是空标识直接抛错，调用方需先落到任务或会话标识。"""
        if not thread_id:
            raise ValueError("thread_id is required")
        return {"configurable": {"thread_id": thread_id}}

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
        """使用官方组件构建图，不实现独立执行循环。

        参数是画像加工具集加运行上下文，返回可执行的图句柄。
        调用约束是运行时必须已打开，换画像换工具要重建句柄。
        坑点是模型覆盖只用于测试注入，生产应走画像选型。"""
        self._require_open()
        model = models.model_for(profile, model_override)
        return build_graph(
            self.checkpointer,
            self.store,
            model,
            profile,
            tools,
            context=context,
            system_prompt=system_prompt,
            additional_tools=additional_tools,
            interrupt_on=interrupt_on,
            tool_error_handler=tool_error_handler,
            extra_middleware=extra_middleware,
        )

    async def put_thread(
        self,
        thread_id: str,
        context: AgentContext,
        *,
        status: str = "idle",
        title: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """把图线程记入存储。供任务列表查询。

        参数是线程标识加运行上下文加状态标题等可选信息，返回写入后的记录。
        调用约束是重复写入会合并旧记录，创建时间以首次为准。
        坑点是工作区以字符串存放，查询过滤依赖该规范形式。"""
        self._require_open()
        prior = await self.get_thread(thread_id)
        item: dict[str, Any] = {
            **(prior or {}),
            "thread_id": thread_id,
            "workspace": str(context.workspace),
            "session_id": context.session_id,
            "task_id": context.task_id,
            "agent_role": context.agent_role,
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
        """按标识读取线程记录，缺失时返回空。

        参数是线程标识，返回记录字典或空。
        坑点是返回值为拷贝，改动不自动回写。"""
        self._require_open()
        item = await self.store.aget(("threads",), thread_id)
        return dict(item.value) if item is not None else None

    async def list_threads(
        self,
        *,
        workspace: Path | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """按更新时间倒序列出线程，可按工作区过滤。

        参数是可选工作区和条数上限，返回最新在前的记录表。
        调用约束是条数非法时直接返回空，过滤依赖写入时的规范路径。
        坑点是大库只取前若干条，调用方不要假定全量。"""
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
        """更新线程状态并刷新时间戳，记录缺失时抛错。

        参数是线程标识和新状态，返回更新后的记录。
        坑点是不做状态机校验，调用方保证状态含义一致。"""
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
        """跑一轮对话，或处理内部触发和中断恢复。

        参数是图句柄加上下文加三选一载荷，返回图的最终状态。
        调用约束是消息恢复触发三者互斥，线程标识缺省时按任务再按会话顺延。
        坑点是排空取消异常会先落状态再抛出，调用方仍需处理异常。
        这是一次拿结果模式，与流式模式共用同一载荷拼装。"""
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
        internal_trigger: bool = False,
        control: RunControl | None = None,
        callbacks: Sequence[Any] = (),
    ) -> _ManagedEventStream:
        """返回原生事件流，支持官方投影选择。

        参数与一次拿结果模式基本一致，返回受管的流对象。
        调用约束是必须当异步上下文管理器使用，退出时自动结算线程状态。
        坑点是创建期失败同样先落状态，流内消费失败由受管流的退出逻辑结算。
        这是流式取增量模式，适合边收边渲染，载荷拼装与另一模式一致。"""
        self._require_open()
        tid = thread_id or context.task_id or context.session_id
        await self.put_thread(tid, context, status="running")
        try:
            config = self.thread_config(tid)
            if callbacks:
                config["callbacks"] = list(callbacks)
            raw = await handle.graph.astream_events(
                self._payload(message, resume, internal_trigger=internal_trigger),
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
        """拼装图输入，消息恢复触发三者只能取其一。"""
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
        """用调用方决定恢复人工确认中断。

        参数是图句柄加上下文加审批决定，返回恢复执行后的最终状态。
        调用约束是只能在有中断的线程上调用，无中断时等价于空跑一轮。
        坑点是决定与新消息互斥，审批通过与否都由调用方编码在决定中。"""
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
        """从保存的超步边界继续被排空的图。

        参数是图句柄加上下文加可选线程标识，返回继续执行后的最终状态。
        调用约束是不带新消息不带恢复值，只续跑未完成的超步。
        坑点是非排空线程调用会空转，调用方先确认线程处于停止态。"""
        return await self.invoke(
            handle,
            context,
            thread_id=thread_id,
            control=control,
            callbacks=callbacks,
        )

    async def get_state(self, handle: AgentHandle, thread_id: str) -> Any:
        """读取线程当前快照，含消息和中断信息。

        参数是图句柄和线程标识，返回官方快照对象。
        坑点是快照只读，改动不回写，回退请走分叉接口。"""
        self._require_open()
        return await handle.graph.aget_state(self.thread_config(thread_id))

    async def get_history(
        self,
        handle: AgentHandle,
        thread_id: str,
        *,
        limit: int = 100,
    ) -> list[Any]:
        """按新到旧列出状态历史，用于回看和选点分叉。

        参数是图句柄加线程标识加条数上限，返回快照列表。
        坑点是条数只截断返回，不影响已存检查点。"""
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
        """在检查点处分叉存档状态，不执行工具。

        参数是图句柄加线程标识加检查点标识，返回指向新分支头的配置。
        调用约束是检查点不存在时抛错，历史分支全部保留。
        坑点是分叉后先清空再重放消息，调用方需显式发起下一轮。
        分两步，先按标识找到历史快照，再写回分叉并标记为已回退。"""
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
        """强制走官方摘要，把压缩结果写回图状态。

        参数是图句柄加上下文加可选焦点，返回是否真正写入。
        调用约束是运行中或有待办中断时拒绝压缩，消息不足两条直接返回否。
        坑点是留存条数取画像与半数消息较小值，焦点只影响摘要措辞。
        分三步，先判空闲，再算留存并拼提示，最后调摘要中间件写回。"""
        self._require_open()
        tid = thread_id or context.task_id or context.session_id
        config = self.thread_config(tid)
        # 先确认空闲，无待办无中断才允许压缩。
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
