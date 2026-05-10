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
from typing import Any, Callable, Dict, List, Optional

from ..core.tool_meta import get_tool_meta


MAX_TOOL_CONCURRENCY = 8


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
        if meta and meta.is_concurrency_safe:
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

        safe, unsafe = _partition_tool_calls(requests)
        batch_result = BatchResult()

        # 并发执行安全组
        if safe:
            safe_results = self._execute_concurrent(safe)
            batch_result.results.extend(safe_results)

            # 检查是否有中止信号
            for r in safe_results:
                if r.is_error:
                    meta = get_tool_meta(r.tool_name)
                    if meta and meta.can_abort_siblings:
                        batch_result.abort_reason = f"sibling_error: {r.tool_name}"
                        return batch_result

        # 串行执行非安全组，每个执行前检查中止信号
        for req in unsafe:
            if batch_result.has_aborted:
                batch_result.results.append(ToolCallResult(
                    tool_name=req.tool_name,
                    tool_call_id=req.tool_call_id,
                    result=None,
                    error=f"已中止（{batch_result.abort_reason}）",
                ))
                continue

            result = self._execute_one(req)
            batch_result.results.append(result)

            if result.is_error:
                meta = get_tool_meta(req.tool_name)
                if meta and meta.can_abort_siblings:
                    batch_result.abort_reason = f"sibling_error: {req.tool_name}"
                    # 不再执行后续工具

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
        results: List[ToolCallResult] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(self._max_concurrency, len(requests))
        ) as executor:
            futures = {
                executor.submit(self._execute_one, req): req
                for req in requests
            }
            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                    results.append(result)
                except Exception as exc:
                    req = futures[future]
                    results.append(ToolCallResult(
                        tool_name=req.tool_name,
                        tool_call_id=req.tool_call_id,
                        result=None,
                        error=str(exc),
                    ))
        # 按原始顺序排列
        order = {req.tool_call_id: i for i, req in enumerate(requests)}
        results.sort(key=lambda r: order.get(r.tool_call_id, 999))
        return results


def partition_by_concurrency(
    tool_names: List[str],
) -> tuple[List[str], List[str]]:
    """快速分区：返回 (并发安全工具名列表, 非并发安全工具名列表)。"""
    safe: List[str] = []
    unsafe: List[str] = []
    for name in tool_names:
        meta = get_tool_meta(name)
        if meta and meta.is_concurrency_safe:
            safe.append(name)
        else:
            unsafe.append(name)
    return safe, unsafe


__all__ = [
    "ToolBatchExecutor",
    "ToolCallRequest",
    "ToolCallResult",
    "BatchResult",
    "partition_by_concurrency",
    "MAX_TOOL_CONCURRENCY",
]
