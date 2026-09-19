"""落盘模块：存档、快照保存加载与导出统计。

保存失败返回假不抛异常，加载失败返回空。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import json

from ..i18n import tr
from .private_io import write_private_json
from .session_state import _SessionState


SESSION_SCHEMA_VERSION = 2


class SessionStoreMixin(_SessionState):
    """落盘能力混入，宿主状态见 _SessionState。"""

    def _archive_history(self) -> Optional[str]:
        """压缩前保存完整历史供回溯，未配置目录返回空。"""
        if not self.archive_dir:
            return None

        try:
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"compact_{timestamp}_{self._compact_count}.json"
            path = self.archive_dir / filename

            data = {
                "schema_version": SESSION_SCHEMA_VERSION,
                "session_id": self.session_id,
                "compact_count": self._compact_count,
                "archived_at": datetime.now(timezone.utc).isoformat(),
                "messages": [msg.to_dict() for msg in self.messages],
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            self._last_archive_path = str(path)
            return str(path)
        except Exception as e:
            print(tr("core.archive_save_failed", error=str(e)))
            return None

    def _messages_payload(self) -> List[Dict[str, Any]]:
        """消息列表落盘形态，保存与导出共用同一格式。"""
        return [msg.to_dict() for msg in self.messages]

    def to_state_dict(self) -> Dict[str, Any]:
        """会话全量落盘字典，单字典一次落盘。"""
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "last_updated": self.last_updated.isoformat(),
            "max_messages": self.max_messages,
            "enable_summary": self.enable_summary,
            "summary": self.summary,
            "messages": self._messages_payload(),
            "_compact_count": self._compact_count,
            "_last_compact_time": self._last_compact_time,
            "_last_archive_path": self._last_archive_path,
            "_model_context_limit": self.model_context_limit,
            "_compact_strategy": self._compact_strategy,
            "_archive_dir": str(self.archive_dir) if self.archive_dir else None,
        }

    def _apply_state_dict(self, data: Dict[str, Any]) -> None:
        """由落盘字典恢复消息与压缩元数据。"""
        from .session_messages import Message

        self.messages = [
            Message.from_dict(msg) for msg in data.get("messages", [])
        ]
        self.summary = data.get("summary")
        self.created_at = datetime.fromisoformat(data.get("created_at", datetime.now(timezone.utc).isoformat()))
        self.last_updated = datetime.fromisoformat(data.get("last_updated", datetime.now(timezone.utc).isoformat()))
        self._compact_count = data.get("_compact_count", 0)
        self._last_compact_time = data.get("_last_compact_time")
        self._last_archive_path = data.get("_last_archive_path")

    def save(self, file_path: str) -> bool:
        """保存会话到文件，失败返回假。"""
        try:
            write_private_json(file_path, self.to_state_dict())
            return True
        except Exception as e:
            print(tr("core.session_save_failed", error=str(e)))
            return False

    def get_context_summary(self) -> str:
        """获取上下文摘要。"""
        if self.summary:
            return self.summary
        if not self.messages:
            return "空对话"
        user_count = sum(1 for m in self.messages if m.role == "user")
        limit_display = self.model_context_limit if self.model_context_limit > 0 else "unknown"
        return (
            f"对话轮数: {user_count} 轮, "
            f"消息: {len(self.messages)} 条, "
            f"Token: {self._running_tokens}/{limit_display} "
            f"({self.usage_ratio:.0%})"
        )

    def export_conversation(self, format: str = "text") -> str:
        """导出会话内容，支持文本、表格与结构化格式。"""
        if format == "json":
            return json.dumps(
                self._messages_payload(),
                indent=2,
                ensure_ascii=False
            )
        elif format == "markdown":
            lines = [f"# 会话 {self.session_id}\n"]
            lines.append(f"**创建时间**: {self.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n")
            lines.append(f"**最后更新**: {self.last_updated.strftime('%Y-%m-%d %H:%M:%S')}\n")
            if self._compact_count > 0:
                limit_display = self.model_context_limit if self.model_context_limit > 0 else "unknown"
                lines.append(f"**压缩次数**: {self._compact_count}\n")
                lines.append(f"**Token 使用**: {self._running_tokens}/{limit_display} ({self.usage_ratio:.0%})\n\n")
            for msg in self.messages:
                role_label = "用户" if msg.role == "user" else "助手"
                lines.append(f"### {role_label}\n")
                lines.append(f"{msg.content}\n\n")
            return "".join(lines)
        else:
            lines = [f"=== 会话 {self.session_id} ===\n"]
            for msg in self.messages:
                role_label = "用户" if msg.role == "user" else "助手"
                lines.append(f"{role_label}: {msg.content}\n\n")
            return "".join(lines)
