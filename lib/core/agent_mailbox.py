"""
文件系统消息邮箱 — 参考 Claude Code teammate mailbox.

Agent 间通信使用文件系统作为消息总线：
- 每个 Agent 拥有独立的 mailbox 目录
- 消息以 JSON 文件存储，文件名为 UUID
- Leader 写入，Worker 轮询读取
- 支持已读标记（通过后缀重命名）
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4


@dataclass
class MailboxMessage:
    """邮箱中的一条消息。"""
    message_id: str
    sender: str
    content: dict[str, Any]
    timestamp: float
    is_read: bool = False


class AgentMailbox:
    """文件系统邮箱。

    用法:
        mailbox = AgentMailbox(Path("/tmp/mailboxes"), "agent_alpha")
        mailbox.write({"type": "task", "payload": "..."})
        for msg in mailbox.read_all():
            if not msg.is_read:
                # 处理消息
                mailbox.mark_read(msg.message_id)
    """

    def __init__(self, base_dir: Path, agent_name: str):
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.agent_name = agent_name
        self.mailbox_dir = self.base_dir / "mailbox" / agent_name
        self.mailbox_dir.mkdir(parents=True, exist_ok=True)
        self._last_poll_index: int = 0

    def write(self, content: dict[str, Any], sender: str = "system") -> Path:
        """写入一条新消息。返回消息文件路径。"""
        msg_id = str(uuid4())[:12]
        msg = {
            "message_id": msg_id,
            "sender": sender,
            "content": content,
            "timestamp": time.time(),
        }
        path = self.mailbox_dir / f"{msg_id}.json"
        path.write_text(json.dumps(msg, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def read_all(self, include_read: bool = False) -> list[MailboxMessage]:
        """读取所有消息。默认跳过已读消息。"""
        messages: list[MailboxMessage] = []
        patterns = ["*.json"]
        if include_read:
            patterns.append("*.read")
        paths: list[Path] = []
        for pat in patterns:
            paths.extend(self.mailbox_dir.glob(pat))
        for path in sorted(paths):
            is_read = path.suffix == ".read"
            if is_read and not include_read:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                messages.append(MailboxMessage(
                    message_id=data["message_id"],
                    sender=data.get("sender", "unknown"),
                    content=data.get("content", {}),
                    timestamp=data.get("timestamp", 0.0),
                    is_read=is_read,
                ))
            except (json.JSONDecodeError, KeyError):
                continue
        return messages

    def read_unread(self) -> list[MailboxMessage]:
        """只读取未读消息。"""
        return self.read_all(include_read=False)

    def _all_paths(self) -> list[Path]:
        """返回邮箱中所有消息路径。"""
        return sorted(list(self.mailbox_dir.glob("*.json")) + list(self.mailbox_dir.glob("*.read")))

    def mark_read(self, message_id: str) -> bool:
        """将消息标记为已读（通过后缀重命名）。"""
        json_path = self.mailbox_dir / f"{message_id}.json"
        if not json_path.exists():
            return False
        read_path = json_path.with_suffix(".read")
        json_path.rename(read_path)
        return True

    def poll(self, timeout: float = 1.0, interval: float = 0.1) -> MailboxMessage | None:
        """轮询等待新消息，直到超时。返回第一条新消息或 None。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            unread = self.read_unread()
            if unread:
                return unread[0]
            time.sleep(interval)
        return None

    def clear(self) -> int:
        """清空邮箱，返回删除的消息数量。"""
        count = 0
        for path in self.mailbox_dir.glob("*"):
            path.unlink()
            count += 1
        return count

    @property
    def message_count(self) -> int:
        """邮箱中的总消息数。"""
        return len(self._all_paths())

    @property
    def unread_count(self) -> int:
        """未读消息数。"""
        return len(list(self.mailbox_dir.glob("*.json")))


__all__ = ["AgentMailbox", "MailboxMessage"]
