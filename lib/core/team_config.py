"""
Team 配置持久化 — 参考 Claude Code TeamFile.

记录多 Agent 协作团队的配置状态到 JSON 文件。
跨会话持久化，支持崩溃后恢复。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class TeamMember:
    """团队中的单个成员。"""
    agent_id: str
    agent_type: str
    session_id: str = ""
    status: str = "active"  # "active" | "idle" | "completed" | "failed"
    worktree: str = ""
    pane_id: str = ""


@dataclass
class TeamConfig:
    """团队配置，序列化到 ~/.sayacode/teams/{name}/config.json。"""
    team_name: str
    members: list[TeamMember] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    session_id: str = ""
    workspace: str = ""

    def __post_init__(self):
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now

    def save(self, path: Path) -> bool:
        """保存到 JSON 文件。"""
        try:
            self.updated_at = datetime.now(timezone.utc).isoformat()
            path = Path(path).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.to_json(), encoding="utf-8")
            return True
        except Exception:
            return False

    @classmethod
    def load(cls, path: Path) -> TeamConfig | None:
        """从 JSON 文件加载。"""
        try:
            path = Path(path).expanduser()
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def to_dict(self) -> dict[str, Any]:
        """转为字典。"""
        return {
            "team_name": self.team_name,
            "members": [
                {
                    "agent_id": m.agent_id,
                    "agent_type": m.agent_type,
                    "session_id": m.session_id,
                    "status": m.status,
                    "worktree": m.worktree,
                    "pane_id": m.pane_id,
                }
                for m in self.members
            ],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "session_id": self.session_id,
            "workspace": self.workspace,
        }

    def to_json(self) -> str:
        """转为 JSON 字符串。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TeamConfig":
        """从字典创建。"""
        members = [
            TeamMember(
                agent_id=m["agent_id"],
                agent_type=m["agent_type"],
                session_id=m.get("session_id", ""),
                status=m.get("status", "active"),
                worktree=m.get("worktree", ""),
                pane_id=m.get("pane_id", ""),
            )
            for m in data.get("members", [])
        ]
        return cls(
            team_name=data["team_name"],
            members=members,
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            session_id=data.get("session_id", ""),
            workspace=data.get("workspace", ""),
        )

    def add_member(self, member: TeamMember) -> None:
        """添加团队成员。"""
        # 替换已有相同 agent_id 的成员
        for i, m in enumerate(self.members):
            if m.agent_id == member.agent_id:
                self.members[i] = member
                return
        self.members.append(member)

    def remove_member(self, agent_id: str) -> bool:
        """移除团队成员。"""
        before = len(self.members)
        self.members = [m for m in self.members if m.agent_id != agent_id]
        return len(self.members) < before

    def get_member(self, agent_id: str) -> TeamMember | None:
        """按 agent_id 查找成员。"""
        for m in self.members:
            if m.agent_id == agent_id:
                return m
        return None

    @property
    def active_members(self) -> list[TeamMember]:
        """获取活跃成员列表。"""
        return [m for m in self.members if m.status == "active"]

    @property
    def member_count(self) -> int:
        return len(self.members)


__all__ = ["TeamConfig", "TeamMember"]
