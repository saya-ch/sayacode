"""TeamManager — 多 Agent 协作入口，整合邮箱/Worker/配置。"""

from __future__ import annotations

from pathlib import Path

from .agent_mailbox import AgentMailbox
from .team_config import TeamConfig, TeamMember
from .worker_manager import WorkerManager, WorkerState


class TeamManager:
    """多 Agent 团队管理器。"""

    def __init__(self, base_dir: Path, team_name: str = "default"):
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.team_name = team_name
        self.workers = WorkerManager(self.base_dir)
        self.config_path = self.base_dir / "teams" / team_name / "config.json"

    def init_team(self, workspace: str = ".") -> TeamConfig:
        """初始化或加载团队配置。"""
        existing = TeamConfig.load(self.config_path)
        if existing:
            return existing
        config = TeamConfig(team_name=self.team_name, workspace=workspace)
        config.save(self.config_path)
        return config

    def spawn(self, agent_type: str, task: str, workspace: str = ".") -> str:
        """启动子 Agent。返回 worker_id。"""
        config = self.init_team(workspace)
        worker_id = self.workers.spawn({
            "agent_type": agent_type,
            "workspace": workspace,
            "mode": "build",
        })
        config.add_member(TeamMember(agent_id=worker_id, agent_type=agent_type, status="active"))
        config.save(self.config_path)

        # 通过邮箱发送任务
        mailbox = self.get_mailbox(worker_id)
        mailbox.write({"type": "task", "task": task, "agent_type": agent_type})
        return worker_id

    def get_mailbox(self, worker_id: str) -> AgentMailbox:
        return AgentMailbox(self.base_dir, worker_id)

    def list_workers(self) -> list[WorkerState]:
        return self.workers.list_workers()

    def get_status(self) -> str:
        """返回人类可读的团队状态。"""
        config = self.init_team()
        active = len(config.active_members)
        workers = self.workers.list_workers()
        lines = [f"团队: {self.team_name}", f"成员: {len(config.members)} | 活跃: {active}"]
        for w in workers:
            mb = self.get_mailbox(w.worker_id)
            lines.append(f"  {w.worker_id}: {w.status.value} (消息: {mb.unread_count}未读/{mb.message_count}总计)")
        return "\n".join(lines)

    def cleanup(self) -> int:
        """终止所有 Worker，保存团队状态。"""
        count = self.workers.cleanup_all()
        config = self.init_team()
        for m in config.members:
            if m.status == "active":
                m.status = "completed"
        config.save(self.config_path)
        return count


__all__ = ["TeamManager"]
