"""
并发工具批处理执行器 — 参考 Claude Code toolOrchestration.ts.

将工具调用按 is_concurrency_safe 分区：
- 并发安全组 → 并行执行（受 MAX_CONCURRENCY 限制）
- 非并发安全组 → 串行执行
"""

from __future__ import annotations

import concurrent.futures
import contextvars
from dataclasses import dataclass, field
import json
from typing import Any, Callable, Dict, List, Optional

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from ..core.tool_meta import get_tool_meta


MAX_TOOL_CONCURRENCY = 8
MAX_BATCH_CALLS = 8


class BatchToolCallInput(BaseModel):
    """批次中的一次独立工具调用。"""

    tool_name: str = Field(description="要调用的工具名称")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="传给工具的参数对象")
    tool_call_id: str = Field(default="", description="可选的调用标识")


class BatchExecuteInput(BaseModel):
    """面向受控批处理、暴露给模型的输入 schema。"""

    calls: List[BatchToolCallInput] = Field(
        min_length=1,
        max_length=MAX_BATCH_CALLS,
        description="彼此独立的工具调用；最多 8 个",
    )


@dataclass
class ToolCallRequest:
    """单个工具调用请求。"""
    tool_name: str
    arguments: Dict[str, Any]
    tool_call_id: str = ""


@dataclass
class ToolCallResult:
    """单个工具调用结果。"""
    tool_name: str
    tool_call_id: str
    result: Any
    error: Optional[str] = None

    @property
    def is_error(self) -> bool:
        """判断本次调用是否失败。"""
        return self.error is not None


@dataclass
class BatchResult:
    """批次执行结果。"""
    results: List[ToolCallResult] = field(default_factory=list)
    abort_reason: Optional[str] = None

    @property
    def has_aborted(self) -> bool:
        """判断批次是否已触发同级中止。"""
        return self.abort_reason is not None


def _is_concurrent_safe(tool_name: str, arguments: Any) -> bool:
    """单点并发判断：空名或无元数据一律按不安全处理（Fail-Closed）。"""
    if not tool_name:
        return False
    meta = get_tool_meta(tool_name)
    if meta is None:
        return False
    try:
        args = arguments if isinstance(arguments, dict) else {}
        return bool(meta.check_concurrency_safe(args))
    except Exception:
        return False


def _sibling_abort(tool_name: str, is_error: bool) -> Optional[str]:
    """同级中止判定：失败且属 shell/git 组时返回中止原因，否则返回空。

    与中间件同级中止同语义（见 SayaHookMiddleware._maybe_sibling_abort）：
    只杀死同批次剩余调用，不结束整轮。
    """
    if not is_error or not tool_name:
        return None
    meta = get_tool_meta(tool_name)
    if not (meta and meta.can_abort_siblings):
        return None
    return f"sibling_error: {tool_name}"


class ToolBatchExecutor:
    """并发工具批处理执行器。

    将工具分为并发安全组和串行组：
    - 并发安全组内的工具并行执行
    - 串行组内的工具按顺序执行
    - 如果某个 shell/git 工具失败，触发同级中止
    """

    def __init__(
        self,
        tool_map: Dict[str, Callable],
        abort_signal: Optional[Any] = None,
        max_concurrency: int = MAX_TOOL_CONCURRENCY,
    ):
        """初始化执行器并绑定工具映射与并发上限。

        abort_signal 由构造注入；_execute_one 预检是唯一生效点（batch 工具路径
        传 None，依赖 Hook 层 abort_controller，两者一致不冲突）。
        """
        self._tool_map = tool_map
        self._abort_signal = abort_signal
        self._max_concurrency = max_concurrency

    def execute_batch(
        self,
        requests: List[ToolCallRequest],
    ) -> BatchResult:
        """执行一批工具调用。"""
        if not requests:
            return BatchResult()

        batch_result = BatchResult()
        index = 0

        # 保持调用顺序，仅分组相邻的并发安全调用。
        # 避免 [write， read] 因 read 可并行而被重排为 [read， write]。
        while index < len(requests):
            req = requests[index]
            if batch_result.has_aborted:
                batch_result.results.append(ToolCallResult(
                    tool_name=req.tool_name,
                    tool_call_id=req.tool_call_id,
                    result=None,
                    error=f"已中止（{batch_result.abort_reason}）",
                ))
                index += 1
                continue

            if _is_concurrent_safe(req.tool_name, req.arguments):
                group: List[ToolCallRequest] = []
                while index < len(requests):
                    candidate = requests[index]
                    if not _is_concurrent_safe(candidate.tool_name, candidate.arguments):
                        break
                    group.append(candidate)
                    index += 1

                group_results = self._execute_concurrent(group)
                batch_result.results.extend(group_results)
                # 并发分支 sibling-abort：串行分支是主要生效点；并发组多为只读，
                # 仅只读 git/shell 失败时此处生效，保留以保证一致。
                for item in group_results:
                    if (reason := _sibling_abort(item.tool_name, item.is_error)) is not None:
                        batch_result.abort_reason = reason
                        break
                continue

            result = self._execute_one(req)
            batch_result.results.append(result)
            index += 1

            if (reason := _sibling_abort(req.tool_name, result.is_error)) is not None:
                batch_result.abort_reason = reason

        return batch_result

    def _abort_reason(self) -> Optional[str]:
        """取当前中止原因：注入信号优先，其次复用 tools.context 同级控制器。

        batch 工具路径传 None 时回落到 ContextVar 控制器，与 Hook 层/中间件
        看到的是同一个 ``ToolAbortController``，语义一致不冲突。
        """
        signal = self._abort_signal
        if signal is not None:
            # 兼容两种历史形状：ToolAbortController（is_aborted/reason）与
            # threading.Event 风格（aborted）。前者优先。
            if hasattr(signal, "is_aborted"):
                try:
                    if signal.is_aborted:
                        return str(getattr(signal, "reason", "unknown") or "unknown")
                except Exception:
                    return "unknown"
            elif hasattr(signal, "aborted"):
                try:
                    if signal.aborted:
                        return "unknown"
                except Exception:
                    return "unknown"
            # 显式注入信号未中止时不再看全局控制器，避免跨批次串扰。
            return None
        try:
            from .context import get_abort_controller

            ctrl = get_abort_controller()
            if ctrl.is_aborted:
                return str(ctrl.reason or "unknown")
        except Exception:
            pass
        return None

    def _execute_one(self, req: ToolCallRequest) -> ToolCallResult:
        """执行单个工具调用。"""
        tool_fn = self._tool_map.get(req.tool_name)
        if tool_fn is None:
            return ToolCallResult(
                tool_name=req.tool_name,
                tool_call_id=req.tool_call_id,
                result=None,
                error=f"未知工具: {req.tool_name}",
            )

        # 检查中止信号，已中止则直接返回。
        aborted = self._abort_reason()
        if aborted is not None:
            if aborted == "unknown":
                error = "操作已中止"
            else:
                error = f"操作已中止（{aborted}）"
            return ToolCallResult(
                tool_name=req.tool_name,
                tool_call_id=req.tool_call_id,
                result=None,
                error=error,
            )

        try:
            result = tool_fn(**req.arguments)
            return ToolCallResult(
                tool_name=req.tool_name,
                tool_call_id=req.tool_call_id,
                result=result,
            )
        except Exception as exc:
            return ToolCallResult(
                tool_name=req.tool_name,
                tool_call_id=req.tool_call_id,
                result=None,
                error=str(exc),
            )

    def _execute_concurrent(
        self,
        requests: List[ToolCallRequest],
    ) -> List[ToolCallResult]:
        """并行执行多个并发安全工具。"""
        if len(requests) == 1:
            return [self._execute_one(requests[0])]

        # 用 ThreadPoolExecutor 并发执行，兼容同步工具函数。
        # 透传 contextvars：子线程默认看不到主线程的 trace_id。
        # 每次提交复制一份：同一个 Context 对象不能被两个线程同时进入。
        indexed_results: List[tuple[int, ToolCallResult]] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self._max_concurrency, len(requests))
        ) as executor:
            futures = {
                executor.submit(contextvars.copy_context().run, self._execute_one, req): (index, req)
                for index, req in enumerate(requests)
            }
            for future in concurrent.futures.as_completed(futures):
                index, req = futures[future]
                try:
                    result = future.result()
                    indexed_results.append((index, result))
                except Exception as exc:
                    indexed_results.append((index, ToolCallResult(
                        tool_name=req.tool_name,
                        tool_call_id=req.tool_call_id,
                        result=None,
                        error=str(exc),
                    )))
        # 恢复原始顺序，调用方无需提供唯一 tool_call_id。
        indexed_results.sort(key=lambda item: item[0])
        return [result for _, result in indexed_results]


# 复用 shell 10k 截断策略，避免单条结果撑爆上下文。
_BATCH_MAX_RESULT_CHARS = 10000


def _truncate_batch_result(value: Any) -> Any:
    """截断超长 batch 结果（仅字符串，与 shell 落盘策略对齐但不落盘）。"""
    if isinstance(value, str) and len(value) > _BATCH_MAX_RESULT_CHARS:
        omitted = len(value) - _BATCH_MAX_RESULT_CHARS
        return value[:_BATCH_MAX_RESULT_CHARS] + f"\n... [result已截断，超出 {omitted} 字符]"
    return value


def create_batch_execute_tool(tools: List[BaseTool]) -> StructuredTool:
    """将 ``ToolBatchExecutor`` 暴露为一个 LangChain 工具。

    tool map 通过 ``invoke`` 调用已绑定运行时的工具，因此它们的校验、权限检查、
    Hook、审计记录与工作区上下文仍然具有权威性。
    """
    tool_map: Dict[str, Callable[..., Any]] = {}
    for tool in tools:
        name = str(getattr(tool, "name", ""))
        if not name or name == "batch_execute":
            continue

        def invoke_bound_tool(_tool: BaseTool = tool, **kwargs: Any) -> Any:
            return _tool.invoke(kwargs)

        tool_map[name] = invoke_bound_tool

    executor = ToolBatchExecutor(tool_map)

    def batch_execute(calls: List[Any]) -> str:
        if len(calls) > MAX_BATCH_CALLS:
            raise ValueError(f"A batch may contain at most {MAX_BATCH_CALLS} calls")

        requests: List[ToolCallRequest] = []
        for index, raw_call in enumerate(calls):
            if hasattr(raw_call, "model_dump"):
                item = raw_call.model_dump()
            else:
                item = dict(raw_call)
            requests.append(ToolCallRequest(
                tool_name=str(item.get("tool_name") or ""),
                arguments=dict(item.get("arguments") or {}),
                tool_call_id=str(item.get("tool_call_id") or f"call_{index + 1}"),
            ))

        result = executor.execute_batch(requests)
        payload = {
            "aborted": result.has_aborted,
            "abort_reason": result.abort_reason,
            "results": [
                {
                    "tool_name": item.tool_name,
                    "tool_call_id": item.tool_call_id,
                    "ok": not item.is_error,
                    "result": _truncate_batch_result(item.result),
                    "error": item.error,
                }
                for item in result.results
            ],
        }
        return json.dumps(payload, ensure_ascii=False, default=str)

    return StructuredTool.from_function(
        func=batch_execute,
        name="batch_execute",
        description=(
            "批量执行彼此独立的工具调用。相邻且标记为并发安全的调用会并行执行，"
            "写入、Shell、Git 等调用保持顺序执行；底层权限与安全策略仍然生效。"
        ),
        args_schema=BatchExecuteInput,
    )


__all__ = [
    "ToolBatchExecutor",
    "ToolCallRequest",
    "ToolCallResult",
    "BatchResult",
    "MAX_TOOL_CONCURRENCY",
    "MAX_BATCH_CALLS",
    "BatchToolCallInput",
    "BatchExecuteInput",
    "create_batch_execute_tool",
]
