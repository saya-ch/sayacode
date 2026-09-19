"""会话共享状态声明：三个混入共用同一份宿主状态。

消息、压缩、落盘三个混入的方法互相读写对方关心的属性，
但混入之间没有继承关系，mypy 看不到宿主 SessionManager 的构造赋值。
本基类把共享状态一次性声明清楚，各混入只管继承，不许重复声明。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional

if TYPE_CHECKING:
    from .session_messages import Message


class _SessionState:
    """宿主状态契约：属性由 SessionManager 构造赋值，行为由各混入提供。"""

    messages: List[Message]
    session_id: str
    max_messages: int
    enable_summary: bool
    model_context_limit: int
    _compact_strategy: str
    _compact_fn: Optional[Callable]
    context_budget: int
    output_reserve: int
    _running_tokens: int
    _last_compact_time: Optional[str]
    _compact_count: int
    archive_dir: Optional[Path]
    _last_archive_path: Optional[str]
    created_at: datetime
    last_updated: datetime
    summary: Optional[str]

    def __init__(
        self,
        max_messages: int = 100,
        enable_summary: bool = True,
        session_id: Optional[str] = None,
        model_context_limit: int = 0,
        compact_strategy: str = "semantic",
        archive_dir: Optional[str] = None,
    ) -> None:
        """构造签名契约：落盘恢复经类方法构造，签名必须一致。"""
        raise NotImplementedError

    @property
    def usage_ratio(self) -> float:
        """上下文使用比例，零到一之间。"""
        raise NotImplementedError

    def _rebuild_token_count(self) -> None:
        """从当前消息列表重算运行中计数。"""
        raise NotImplementedError

    def _count_message_tokens(self, message: Message) -> int:
        """估算单条消息 token 数。"""
        raise NotImplementedError

    def _auto_compact(
        self,
        focus: Optional[str] = None,
        gentle: bool = False,
        urgent: bool = False,
    ) -> None:
        """三层压缩核心。"""
        raise NotImplementedError

    def _archive_history(self) -> Optional[str]:
        """压缩前存档完整历史，未配置返回空。"""
        raise NotImplementedError


__all__ = ["_SessionState"]
