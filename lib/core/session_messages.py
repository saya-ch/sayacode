"""消息存储模块：消息体、派生视图与会话管理器组装。

会话唯一真相源为消息列表，记忆视图只读派生。
压缩与落盘能力来自混入类，本文件负责组装。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import json

from .session_compact import SessionCompactMixin, _budgets_for_limit
from .session_state import _SessionState
from .session_store import SESSION_SCHEMA_VERSION, SessionStoreMixin
from ..i18n import tr


def normalize_truncate_target(target) -> int:
    """归一化截断目标为非负整数，非法输入返回零。"""
    try:
        return max(0, int(target or 0))
    except (TypeError, ValueError):
        return 0


@dataclass
class Message:
    """单条消息数据结构，角色限用户或助手。"""
    role: str
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "metadata": self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        """从字典创建"""
        return cls(
            role=data["role"],
            content=data["content"],
            timestamp=data.get("timestamp", ""),
            metadata=data.get("metadata", {})
        )


def load_legacy_memory_json(json_str: str) -> List[Dict[str, Any]]:
    """只读兼容已落盘的旧记忆格式，失败返回空列表不抛异常。"""
    try:
        data = json.loads(json_str)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    items = data.get("interactions", [])
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for entry in items:
        if isinstance(entry, dict):
            out.append(entry)
    return out


class SessionDerivedMemoryView:
    """会话派生的只读记忆视图，不另存第二份历史。"""

    def __init__(self, session: "SessionManager") -> None:
        """绑定会话实例，不拷贝消息，实时派生。"""
        self._session = session

    def _pairs(self) -> List[Tuple[str, str]]:
        """由会话派生交互轮，复用会话唯一真相源。"""
        session = self._session
        if session is None or not hasattr(session, "derived_interaction_pairs"):
            return []
        try:
            return list(session.derived_interaction_pairs())
        except Exception:
            return []

    def summarize(self) -> str:
        """生成记忆摘要，只读派生，不触碰会话。"""
        pairs = self._pairs()
        if not pairs:
            return "空记忆 - 暂无对话历史"
        lines = ["## 记忆摘要", "", f"**总交互数**: {len(pairs)}", ""]
        last_user, last_ai = pairs[-1]
        lines.append("**最近活动**:")
        lines.append(f"  用户: {last_user[:50]}{'...' if len(last_user) > 50 else ''}")
        lines.append(f"  助手: {last_ai[:50]}{'...' if len(last_ai) > 50 else ''}")
        return "\n".join(lines)

    def get_recent_context(self, n: int = 10) -> str:
        """获取最近若干轮交互摘要，只读派生。"""
        pairs = self._pairs()
        if not pairs:
            return "暂无对话历史"
        recent = pairs[-n:] if len(pairs) >= n else pairs
        lines = [f"## 最近 {len(recent)} 轮对话\n"]
        for i, (user_text, ai_text) in enumerate(recent, 1):
            lines.append(f"### 第 {i} 轮")
            lines.append(f"**用户**: {user_text[:100]}{'...' if len(user_text) > 100 else ''}")
            lines.append(f"**助手**: {ai_text[:100]}{'...' if len(ai_text) > 100 else ''}")
            lines.append("")
        return "\n".join(lines)

    def get_modified_files(self) -> List[str]:
        """派生视图不跟踪文件修改，恒返回空列表。"""
        return []

    def get_stats(self) -> Dict[str, Any]:
        """返回派生统计，字段名与旧格式对齐，只读。"""
        pairs = self._pairs()
        session_id = getattr(self._session, "session_id", "")
        return {
            "session_id": session_id,
            "total_interactions": len(pairs),
            "total_file_modifications": 0,
            "total_tool_uses": 0,
            "unique_tools_used": 0,
            "unique_files_modified": 0,
        }

    def clear(self) -> None:
        """视图无状态可清，保持空操作。"""
        return None

    def __len__(self) -> int:
        """返回派生交互轮数。"""
        return len(self._pairs())

    def __repr__(self) -> str:
        session_id = getattr(self._session, "session_id", "")
        return f"SessionDerivedMemoryView(session={session_id}, interactions={len(self)})"


class SessionMessageMixin(_SessionState):
    """消息管理能力混入，持有消息列表的增删查改，宿主状态见 _SessionState。"""

    def add_message(
        self,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Message:
        """添加消息并更新计数，超安全上限触发压缩。"""
        message = Message(
            role=role,
            content=content,
            metadata=metadata or {}
        )
        self.messages.append(message)
        self.last_updated = datetime.now(timezone.utc)

        # 更新运行中计数
        self._running_tokens += self._count_message_tokens(message)

        # 安全上限兜底，防止极端消息撑爆
        if len(self.messages) > self.max_messages * 2:
            self._auto_compact()

        return message

    def add_user_message(self, content: str) -> Message:
        """添加用户消息"""
        return self.add_message("user", content)

    def add_assistant_message(self, content: str, metadata: Optional[Dict[str, Any]] = None) -> Message:
        """添加助手消息"""
        return self.add_message("assistant", content, metadata)

    def truncate_to_user_turns(self, target: int) -> int:
        """按用户轮截断并重算计数，返回丢弃消息数。"""
        want = normalize_truncate_target(target)
        original = len(self.messages)
        if want <= 0:
            if not self.messages:
                return 0
            self.messages = []
            self._rebuild_token_count()
            return original
        user_indices = [i for i, m in enumerate(self.messages) if m.role == "user"]
        if want >= len(user_indices):
            return 0
        cutoff = user_indices[want]
        self.messages = self.messages[:cutoff]
        self._rebuild_token_count()
        return original - len(self.messages)

    def get_messages(
        self,
        include_system: bool = True,
        max_turns: Optional[int] = None,
        include_compaction_summaries: bool = False
    ) -> List[Dict[str, Any]]:
        """获取模型输入用消息列表，调用前建议先触发压缩。"""
        messages: List[Dict[str, Any]] = []

        for msg in self.messages:
            if not include_system and msg.role == "system":
                is_compaction_artifact = bool(msg.metadata.get("compressed"))
                if not (include_compaction_summaries and is_compaction_artifact):
                    continue
            item: Dict[str, Any] = {
                "role": msg.role,
                "content": msg.content
            }
            if msg.metadata:
                item["metadata"] = dict(msg.metadata)
            messages.append(item)

        if max_turns:
            system_messages = [m for m in messages if m["role"] == "system"]
            non_system = [m for m in messages if m["role"] != "system"]
            recent = non_system[-max_turns * 2:]
            messages = system_messages + recent

        return messages

    def get_history(self) -> List[Tuple[str, str]]:
        """获取对话历史简化格式。"""
        return [(msg.role, msg.content) for msg in self.messages]

    def derived_interaction_pairs(self) -> List[Tuple[str, str]]:
        """由消息派生问答轮视图，系统与压缩产物不计入。"""
        pairs: List[Tuple[str, str]] = []
        pending_user: Optional[str] = None
        for msg in self.messages:
            if msg.metadata.get("compressed") or msg.role == "system":
                continue
            if msg.role == "user":
                if pending_user is not None:
                    pairs.append((pending_user, ""))
                pending_user = msg.content
            elif msg.role == "assistant":
                pairs.append((pending_user or "", msg.content))
                pending_user = None
        if pending_user is not None:
            pairs.append((pending_user, ""))
        return pairs

    @staticmethod
    def messages_from_interaction_dicts(items: List[Dict[str, Any]]) -> List[Message]:
        """由记忆形态交互字典派生消息列表。"""
        out: List[Message] = []
        for item in items:
            user_text = item.get("user_input", "") or item.get("user", "")
            ai_text = item.get("ai_response", "") or item.get("assistant", "")
            if user_text:
                out.append(Message(role="user", content=user_text))
            if ai_text:
                out.append(Message(role="assistant", content=ai_text))
        return out

    def clear(self):
        """清空会话历史与计数。"""
        self.messages = []
        self.summary = None
        self._running_tokens = 0
        self._compact_count = 0
        self._last_compact_time = None
        self._last_archive_path = None
        self.last_updated = datetime.now(timezone.utc)

    def is_empty(self) -> bool:
        """检查会话是否为空。"""
        return len(self.messages) == 0

    def get_message_count(self) -> int:
        """获取消息数量。"""
        return len(self.messages)


class SessionManager(SessionMessageMixin, SessionCompactMixin, SessionStoreMixin):
    """会话管理器，商业级上下文压缩，消息追加写入，压缩只增标记。"""

    def __init__(
        self,
        max_messages: int = 100,
        enable_summary: bool = True,
        session_id: Optional[str] = None,
        model_context_limit: int = 0,
        compact_strategy: str = "semantic",
        archive_dir: Optional[str] = None,
    ):
        """初始化会话管理器，设置预算计数与存档目录。"""
        self.session_id = session_id or self._generate_session_id()
        self.max_messages = max_messages
        self.enable_summary = enable_summary

        # 预算配置
        self.model_context_limit = model_context_limit
        self._compact_strategy = compact_strategy
        self._compact_fn: Optional[Callable] = None

        # 未知窗口不启用预算压缩，不伪造默认值
        self.context_budget, self.output_reserve = _budgets_for_limit(model_context_limit)

        # 运行中计数
        self._running_tokens: int = 0
        self._last_compact_time: Optional[str] = None
        self._compact_count: int = 0

        # 存档目录可选，用于压缩时保存原始消息回溯
        self.archive_dir: Optional[Path] = Path(archive_dir) if archive_dir else None
        if self.archive_dir:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
        self._last_archive_path: Optional[str] = None

        # 消息历史
        self.messages: List[Message] = []

        # 元数据
        self.created_at = datetime.now(timezone.utc)
        self.last_updated = datetime.now(timezone.utc)

        # 摘要信息
        self.summary: Optional[str] = None

    @classmethod
    def load(cls, file_path: str) -> Optional["SessionManager"]:
        """从文件加载会话，缺失坏格式与版本不符返回空。"""
        try:
            path = Path(file_path)
            if not path.exists():
                return None

            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if data.get("schema_version") != SESSION_SCHEMA_VERSION:
                return None

            raw_archive = data.get("_archive_dir")
            archive_dir = None
            if raw_archive:
                try:
                    arch_path = Path(str(raw_archive)).expanduser().resolve()
                    base = path.expanduser().resolve().parent
                    arch_path.relative_to(base)
                    archive_dir = str(arch_path)
                except (OSError, ValueError):
                    archive_dir = None

            session = cls(
                max_messages=data.get("max_messages", 100),
                enable_summary=data.get("enable_summary", True),
                session_id=data.get("session_id"),
                model_context_limit=data.get("_model_context_limit", 0),
                compact_strategy=data.get("_compact_strategy", "semantic"),
                archive_dir=archive_dir,
            )

            session._apply_state_dict(data)

            # 加载后重建 token 计数
            session._rebuild_token_count()

            return session
        except Exception as e:
            print(tr("core.session_load_failed", error=str(e)))
            return None

    @staticmethod
    def _generate_session_id() -> str:
        from uuid import uuid4
        return str(uuid4())[:8]

    def __repr__(self) -> str:
        limit_display = self.model_context_limit if self.model_context_limit > 0 else "unknown"
        return (
            f"SessionManager("
            f"id={self.session_id}, "
            f"messages={len(self.messages)}, "
            f"tokens={self._running_tokens}/{limit_display} "
            f"({self.usage_ratio:.0%}), "
            f"compacts={self._compact_count}"
            f")"
        )
