"""
并发工具批处理执行器 — 参考 Claude Code toolOrchestration.ts.

将工具调用按 is_concurrency_safe 分区：
- 并发安全组 → 并行执行（受 MAX_CONCURRENCY 限制）
- 非并发安全组 → 串行执行
- Context modifier 排队，批次完成后统一应用
"""

from __future__ import annotations

import concurrent.futures
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
    context_modifier: Optional[Callable] = None

    @property
    def is_error(self) -> bool:
        return self.error is not None


@dataclass
class BatchResult:
    """批次执行结果。"""
    results: List[ToolCallResult] = field(default_factory=list)
    context_modifiers: List[Callable] = field(default_factory=list)
    abort_reason: Optional[str] = None

    @property
    def has_aborted(self) -> bool:
        return self.abort_reason is not None


def _partition_tool_calls(
    requests: List[ToolCallRequest],
) -> tuple[List[ToolCallRequest], List[ToolCallRequest]]:
    """将工具调用按并发安全性分区。"""
    safe: List[ToolCallRequest] = []
    unsafe: List[ToolCallRequest] = []
    for req in requests:
        meta = get_tool_meta(req.tool_name)
        if meta and meta.check_concurrency_safe(req.arguments):
            safe.append(req)
        else:
            unsafe.append(req)
    return safe, unsafe


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

        # 在变更/不安全操作前后保持调用顺序。只有彼此相邻的并发安全调用才会被分组，
        # 因此 [write, read] 绝不会仅仅因为 read 可以安全并行
        # 就变成 [read, write]。
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

            meta = get_tool_meta(req.tool_name)
            if meta and meta.check_concurrency_safe(req.arguments):
                group: List[ToolCallRequest] = []
                while index < len(requests):
                    candidate = requests[index]
                    candidate_meta = get_tool_meta(candidate.tool_name)
                    if not (
                        candidate_meta
                        and candidate_meta.check_concurrency_safe(candidate.arguments)
                    ):
                        break
                    group.append(candidate)
                    index += 1

                group_results = self._execute_concurrent(group)
                batch_result.results.extend(group_results)
                for item in group_results:
                    if item.context_modifier:
                        batch_result.context_modifiers.append(item.context_modifier)
                    item_meta = get_tool_meta(item.tool_name)
                    if item.is_error and item_meta and item_meta.can_abort_siblings:
                        batch_result.abort_reason = f"sibling_error: {item.tool_name}"
                        break
                continue

            result = self._execute_one(req)
            batch_result.results.append(result)
            index += 1

            if result.is_error:
                if meta and meta.can_abort_siblings:
                    batch_result.abort_reason = f"sibling_error: {req.tool_name}"

            if result.context_modifier:
                batch_result.context_modifiers.append(result.context_modifier)

        return batch_result

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

        # 检查中止信号
        if self._abort_signal is not None:
            if hasattr(self._abort_signal, "is_aborted") and self._abort_signal.is_aborted:
                return ToolCallResult(
                    tool_name=req.tool_name,
                    tool_call_id=req.tool_call_id,
                    result=None,
                    error=f"操作已中止（{getattr(self._abort_signal, 'reason', 'unknown')}）",
                )
            elif hasattr(self._abort_signal, "aborted") and self._abort_signal.aborted:
                return ToolCallResult(
                    tool_name=req.tool_name,
                    tool_call_id=req.tool_call_id,
                    result=None,
                    error="操作已中止",
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

        # 使用 ThreadPoolExecutor 进行并发（兼容同步工具函数）
        indexed_results: List[tuple[int, ToolCallResult]] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self._max_concurrency, len(requests))
        ) as executor:
            futures = {
                executor.submit(self._execute_one, req): (index, req)
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
        # 按原始位置排列；调用方不需要提供唯一的 tool_call_id。
        indexed_results.sort(key=lambda item: item[0])
        return [result for _, result in indexed_results]


def partition_by_concurrency(
    tool_names: List[str],
) -> tuple[List[str], List[str]]:
    """快速分区：返回 (并发安全工具名列表, 非并发安全工具名列表)。"""
    safe: List[str] = []
    unsafe: List[str] = []
    for name in tool_names:
        meta = get_tool_meta(name)
        if meta and meta.check_concurrency_safe({}):
            safe.append(name)
        else:
            unsafe.append(name)
    return safe, unsafe


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
                    "result": item.result,
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
    "partition_by_concurrency",
    "MAX_TOOL_CONCURRENCY",
    "MAX_BATCH_CALLS",
    "BatchToolCallInput",
    "BatchExecuteInput",
    "create_batch_execute_tool",
]
