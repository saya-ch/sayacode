"""中断恢复模块：并行批、回退模式与一次性批准。

本模块只放无依赖的判定小件，运行时类按需调用。
失败一律收紧，绝不放宽。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from .permission_policy import DANGEROUS_TOOLS, PermissionDecision


_PARALLEL_BATCH: ContextVar[bool] = ContextVar("sayacode_parallel_batch", default=False)


def is_parallel_batch() -> bool:
    """是否处于并行批内，后台线程弹不了确认窗，询问一律拒绝。"""
    return bool(_PARALLEL_BATCH.get())


@contextmanager
def parallel_batch_session() -> Iterator[None]:
    """进入并行批，询问不弹窗直接拒绝。"""
    token = _PARALLEL_BATCH.set(True)
    try:
        yield
    finally:
        _PARALLEL_BATCH.reset(token)


def apply_fallback_decision(
    decision: PermissionDecision,
    tool_name: str,
    is_in_fallback: bool,
) -> PermissionDecision:
    """回退模式把放行升级为询问，拒绝保持不变，只收紧不放宽。"""
    if not is_in_fallback or decision.action != "allow":
        return decision
    return PermissionDecision(
        allowed=False,
        action="ask",
        reason=f"{tool_name}: ask (连续拒绝回退模式)",
        source=decision.source,
    )


def build_parallel_batch_deny(tool_name: str, source: str) -> PermissionDecision:
    """并行批内询问直接拒绝，避免后台线程弹窗卡死。"""
    return PermissionDecision(
        allowed=False,
        action="deny",
        reason=f"Permission denied for tool '{tool_name}' by {source} policy (并行批内禁止弹窗，ask fail-closed)。",
        source=source,
    )


def grant_fingerprint(tool_name: str, arguments: Any = None) -> str:
    """一次性批准的身份：工具名加参数规范 JSON，精确到本次调用。

    同名不同参不算同一次批准，批的是哪次调用就只放行那次。
    """
    import json

    try:
        args_text = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        args_text = str(arguments or "")
    return str(tool_name) + "\n" + args_text


def consume_one_shot_grant(grants: set, tool_name: str, arguments: Any = None) -> bool:
    """消耗一次性批准，名加参数都命中才返回真，用后即焚。"""
    key = grant_fingerprint(tool_name, arguments)
    if key not in grants:
        return False
    grants.discard(key)
    return True


def build_one_shot_grant(tool_name: str) -> PermissionDecision:
    """构造一次性批准的放行决策。"""
    return PermissionDecision(
        allowed=True,
        action="allow",
        reason=f"{tool_name}: allow (one-shot interrupt grant)",
        source="interrupt",
    )


def apply_dangerous_floor(
    tool_name: str,
    decision: PermissionDecision,
    record_stripped,
) -> PermissionDecision:
    """危险工具地板，任何来源的放行一律降级为拒绝。"""
    if decision.action != "allow" or str(tool_name) not in DANGEROUS_TOOLS:
        return decision
    record_stripped(decision.source or "session", str(tool_name))
    return PermissionDecision(
        allowed=False,
        action="deny",
        reason=f"{tool_name}: deny (危险工具不允许自动放行)",
        source=decision.source,
    )
