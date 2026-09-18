"""运行追踪：把一次用户请求内的审计事件串成可回溯的调用树。

trace_id 是「一次用户请求」的标识，随 ContextVar 传播。同一 turn 内的
审计事件、Hook 触发、工具调用与（继承上下文的）后台委托共享同一个
trace_id，因此 ``AuditLogService.read_by_trace(trace_id)`` 拿到的有序事件
序列**就是**这次请求的调用树。

span 记录耗时：进入/退出成对写入 ``span`` 类型审计事件（带 duration_ms、
span_id、parent_span），用来回答「这一轮到底慢在哪一步」。

无 trace 上下文时（例如单测直接调工具）一切照常：trace_id 为空，
审计退回「每条事件各自一个 id」的旧行为。
"""

from __future__ import annotations

import functools
import inspect
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Dict, Iterator, Optional, Tuple
from uuid import uuid4

from langchain_core.callbacks import BaseCallbackHandler


TRACE_PREFIX = "tr-"
SPAN_PREFIX = "sp-"

# 当前 trace 与当前 span 栈（栈用来算 parent_span 与嵌套深度）。
_TRACE_ID: ContextVar[str] = ContextVar("sayacode_trace_id", default="")
_SPAN_STACK: ContextVar[Tuple[Tuple[str, str], ...]] = ContextVar(
    "sayacode_span_stack", default=()
)


def new_trace_id() -> str:
    """生成一个新的 trace_id（可读前缀 + 12 位十六进制）。"""
    return TRACE_PREFIX + uuid4().hex[:12]


def new_span_id() -> str:
    """生成一个新的 span_id。"""
    return SPAN_PREFIX + uuid4().hex[:8]


def current_trace_id() -> str:
    """当前 trace_id；不在任何 trace 内时返回空串。"""
    return _TRACE_ID.get()


def current_span() -> Tuple[str, str]:
    """当前 span（span_id, name）；不在任何 span 内时返回 ("", "")。"""
    stack = _SPAN_STACK.get()
    return stack[-1] if stack else ("", "")


def span_depth() -> int:
    """当前 span 嵌套深度（0 表示不在 span 内）。"""
    return len(_SPAN_STACK.get())


@contextmanager
def trace_session(trace_id: Optional[str] = None) -> Iterator[str]:
    """进入一次请求的追踪区。

    * 已在某个 trace 内且未显式指定 id → 复用外层 id（子流程归属于同一请求）；
    * 否则用给定的 id，或新生成一个。

    退出时恢复外层状态，因此嵌套调用是安全的。
    """
    existing = current_trace_id()
    effective = trace_id or existing or new_trace_id()
    token = _TRACE_ID.set(effective)
    try:
        yield effective
    finally:
        _TRACE_ID.reset(token)


@contextmanager
def span(name: str, *, audit: bool = True, **details: Any) -> Iterator[str]:
    """记录一个带耗时的执行区间，退出时写一条 ``span`` 审计事件。

    ``name`` 建议用「动作:对象」形状（如 ``tool:read_file``、``turn``），
    便于在调用树里一眼定位。``audit=False`` 时只计时不上报（高频内部循环用）。
    """
    import time

    from .audit import append_audit_event

    span_id = new_span_id()
    parent_id, _parent_name = current_span()
    stack = _SPAN_STACK.get()
    token = _SPAN_STACK.set(stack + ((span_id, name),))
    started = time.perf_counter()
    status = "ok"
    error = ""
    try:
        yield span_id
    except BaseException as exc:  # 失败也要留下耗时与原因
        status = "error"
        error = str(exc)[:500]
        raise
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
        _SPAN_STACK.reset(token)
        if audit:
            payload: dict[str, Any] = {
                "span": name,
                "span_id": span_id,
                "duration_ms": duration_ms,
                "status": status,
            }
            if parent_id:
                payload["parent_span"] = parent_id
            if error:
                payload["error"] = error
            payload.update(details)
            append_audit_event("span", name, details=payload, trace_id=current_trace_id() or None)


def traced(name: str, *, audit: bool = True) -> Callable:
    """装饰器：把函数体包进 trace_session + span。

    生成器函数（如 ``stream_run``）会被特殊处理 —— 用 ``yield from`` 让
    追踪区间覆盖**整个迭代过程**，而不是只覆盖生成器的创建。
    嵌套调用复用外层 trace_id，因此子流程自动归属同一次请求。
    """

    def decorator(func: Callable) -> Callable:
        """包被装饰函数，进追踪区后再执行原函数。"""
        if inspect.isgeneratorfunction(func):

            @functools.wraps(func)
            def gen_wrapper(*args: Any, **kwargs: Any):
                # 生成器用 yield from 包住全程，覆盖整个迭代区间。
                with trace_session(), span(name, audit=audit):
                    yield from func(*args, **kwargs)

            return gen_wrapper

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any):
            # 同步函数直接包一层追踪区执行。
            with trace_session(), span(name, audit=audit):
                return func(*args, **kwargs)

        return wrapper

    return decorator


class ModelCallTraceHandler(BaseCallbackHandler):
    """把每次模型调用记成审计事件：耗时与 token 用量。

    本版本的 chat model 触发 ``on_chat_model_start``（不是 ``on_llm_start``），
    结束统一走 ``on_llm_end``。工具调用已由中间件层审计，这里不重复记，
    只补上「模型这一步花了多久、用掉多少 token」这块空白。
    """

    def __init__(self, model_name: str = "", audit: bool = True) -> None:
        super().__init__()
        self._model_name = str(model_name or "model")
        self._audit_enabled = audit
        self._started: Dict[str, float] = {}
        self._batches: Dict[str, int] = {}

    def on_chat_model_start(
        self, serialized: Any, messages: Any, *, run_id: Any = None, **kwargs: Any
    ) -> None:
        """记录起始时刻与送进去的消息条数。"""
        key = str(run_id)
        self._started[key] = time.perf_counter()
        try:
            self._batches[key] = sum(len(batch) for batch in messages)
        except TypeError:
            self._batches[key] = 0

    def on_llm_end(self, response: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        """成功返回：写耗时与用量。"""
        key = str(run_id)
        details = self._usage_of(response)
        details["messages"] = self._batches.pop(key, 0)
        started = self._started.pop(key, None)
        if started is not None:
            details["duration_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        self._emit(details)

    def on_llm_error(self, error: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        """模型调用失败：同样留痕，否则失败的一步在调用树里会消失。"""
        key = str(run_id)
        self._batches.pop(key, None)
        started = self._started.pop(key, None)
        details: Dict[str, Any] = {
            "error": str(error),
            "exception_type": type(error).__name__,
        }
        if started is not None:
            details["duration_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        self._emit(details, allowed=False)

    @staticmethod
    def _usage_of(response: Any) -> Dict[str, Any]:
        """从 LLMResult 里取 token 用量；取不到就返回空。"""
        generations = getattr(response, "generations", None) or []
        for group in generations:
            for generation in group:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None)
                if isinstance(usage, dict):
                    return {
                        "input_tokens": usage.get("input_tokens"),
                        "output_tokens": usage.get("output_tokens"),
                        "total_tokens": usage.get("total_tokens"),
                    }
        llm_output = getattr(response, "llm_output", None)
        if isinstance(llm_output, dict):
            token_usage = llm_output.get("token_usage") or llm_output.get("usage")
            if isinstance(token_usage, dict):
                return dict(token_usage)
        return {}

    def _emit(self, details: Dict[str, Any], allowed: Optional[bool] = True) -> None:
        """写审计；审计 I/O 失败绝不能影响模型调用本身。"""
        if not self._audit_enabled:
            return
        from .audit import append_audit_event

        payload = {key: value for key, value in details.items() if value is not None}
        try:
            append_audit_event("llm", self._model_name, allowed=allowed, details=payload)
        except Exception:
            pass


__all__ = [
    "SPAN_PREFIX",
    "TRACE_PREFIX",
    "ModelCallTraceHandler",
    "current_span",
    "current_trace_id",
    "new_span_id",
    "new_trace_id",
    "span",
    "span_depth",
    "trace_session",

    "traced",
]
