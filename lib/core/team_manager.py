"""Closed-loop multi-Agent team coordination."""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from .agent_mailbox import AgentMailbox
from .team_config import TeamConfig, TeamMember
from .team_worktree import TeamWorktreeManager, WorktreeIsolationError
from .worker_manager import WorkerManager, WorkerState, WorkerStatus


_TERMINAL_STATUSES = {
    WorkerStatus.COMPLETED,
    WorkerStatus.FAILED,
    WorkerStatus.KILLED,
}


class TeamManager:
    """Launch workers, track durable state, and collect mailbox results."""

    def __init__(self, base_dir: Path, team_name: str = "default"):
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.team_name = team_name
        self.config_path = self.base_dir / "teams" / team_name / "config.json"
        self.workers = WorkerManager(self.config_path.parent / "workers")
        self.worktrees = TeamWorktreeManager(self.config_path.parent / "worktrees")

    def init_team(self, workspace: str = ".") -> TeamConfig:
        existing = TeamConfig.load(self.config_path)
        if existing:
            if workspace != "." and existing.workspace != workspace:
                existing.workspace = workspace
                existing.save(self.config_path)
            return existing
        config = TeamConfig(team_name=self.team_name, workspace=workspace)
        config.save(self.config_path)
        return config

    def spawn(self, agent_type: str, task: str, workspace: str = ".") -> str:
        """Queue a task in a mailbox and launch a headless child Agent."""
        task = str(task or "").strip()
        if not task:
            raise ValueError("子 Agent 任务不能为空")
        if len(task) > 50_000:
            raise ValueError("子 Agent 任务不能超过 50000 个字符")
        if self.workers.active_count() >= self.workers.max_workers:
            raise RuntimeError(f"最多同时运行 {self.workers.max_workers} 个子 Agent")

        config = self.init_team(workspace)
        worker_id = self.workers.new_worker_id()
        effective_workspace = str(Path(workspace).expanduser().resolve())
        worktree = None
        if _requires_worktree(agent_type):
            worktree = self.worktrees.prepare(worker_id, effective_workspace)
            effective_workspace = worktree.workspace
        self.get_mailbox(worker_id).write(
            {"type": "task", "task": task, "agent_type": agent_type},
            sender="leader",
        )
        mode = _mode_for_agent_type(agent_type)
        worker_id = self.workers.spawn(
            {
                "agent_type": agent_type,
                "workspace": effective_workspace,
                "mode": mode,
                "sayacode_home": str(self.base_dir),
                "source_workspace": str(Path(workspace).expanduser().resolve()),
                "worktree": worktree.worktree_root if worktree else "",
                "branch": worktree.branch if worktree else "",
                "source_commit": worktree.source_commit if worktree else "",
            },
            worker_id=worker_id,
        )
        state = self.workers.get_state(worker_id)
        member_status = state.status.value if state else "failed"
        config.add_member(TeamMember(
            agent_id=worker_id,
            agent_type=agent_type,
            status=member_status,
            worktree=worktree.worktree_root if worktree else "",
        ))
        config.save(self.config_path)
        return worker_id

    def get_mailbox(self, worker_id: str) -> AgentMailbox:
        return AgentMailbox(self.base_dir, worker_id)

    def list_workers(self) -> list[WorkerState]:
        return self.workers.list_workers()

    def get_worker_state(self, worker_id: str) -> WorkerState | None:
        return self.workers.get_state(worker_id)

    def get_result(self, worker_id: str, *, mark_read: bool = True) -> dict[str, Any] | None:
        """Read a worker result from the leader mailbox, with file fallback."""
        leader = self.get_mailbox("leader")
        matches = [
            message
            for message in leader.read_all(include_read=True)
            if message.sender == worker_id and message.content.get("type") == "result"
        ]
        if matches:
            message = max(matches, key=lambda item: item.timestamp)
            if mark_read and not message.is_read:
                leader.mark_read(message.message_id)
            return self._decorate_result(worker_id, dict(message.content))
        result = self.workers.get_result(worker_id)
        return self._decorate_result(worker_id, result) if result is not None else None

    def get_delivery(self, worker_id: str) -> dict[str, Any] | None:
        """Inspect a retained isolated worktree without mutating it."""
        state = self.workers.get_state(worker_id)
        if state is None or not state.worktree:
            return None
        return self.worktrees.inspect(
            state.worktree,
            source_commit=str(state.config.get("source_commit") or ""),
        )

    def wait(self, worker_id: str, timeout: float = 60.0) -> dict[str, Any] | None:
        """Wait for one worker to reach a terminal state."""
        deadline = time.monotonic() + max(0.0, min(float(timeout), 3600.0))
        while True:
            state = self.workers.get_state(worker_id)
            if state is None:
                return None
            if state.status in _TERMINAL_STATUSES:
                self._sync_config_statuses()
                return self.get_result(worker_id)
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.1)

    def get_status(self) -> str:
        config = self._sync_config_statuses()
        states = {state.worker_id: state for state in self.workers.list_workers()}
        active = sum(1 for member in config.members if member.status == "running")
        lines = [f"团队: {self.team_name}", f"成员: {len(config.members)} | 运行中: {active}"]
        for member in config.members:
            state = states.get(member.agent_id)
            pid_text = f" pid={state.pid}" if state and state.pid else ""
            result_ready = self.get_result(member.agent_id, mark_read=False) is not None
            result_text = " | 结果可用" if result_ready else ""
            worktree_text = ""
            if state and state.worktree:
                branch = str(state.config.get("branch") or "")
                worktree_text = f" | branch={branch} | worktree={state.worktree}"
            lines.append(
                f"  {member.agent_id}: {member.status} ({member.agent_type})"
                f"{pid_text}{result_text}{worktree_text}"
            )
        return "\n".join(lines)

    def cleanup(self) -> int:
        """Terminate owned active workers and synchronize durable status."""
        count = self.workers.cleanup_all()
        self._sync_config_statuses()
        return count

    def _sync_config_statuses(self) -> TeamConfig:
        config = self.init_team()
        states = {state.worker_id: state for state in self.workers.list_workers()}
        changed = False
        for member in config.members:
            state = states.get(member.agent_id)
            if state is not None and member.status != state.status.value:
                member.status = state.status.value
                changed = True
        if changed:
            config.save(self.config_path)
        return config

    def _decorate_result(self, worker_id: str, result: dict[str, Any]) -> dict[str, Any]:
        state = self.workers.get_state(worker_id)
        decorated = dict(result)
        if state is not None:
            decorated.setdefault("worktree", state.worktree or "")
            decorated.setdefault("branch", str(state.config.get("branch") or ""))
            decorated.setdefault(
                "source_workspace",
                str(state.config.get("source_workspace") or state.config.get("workspace") or ""),
            )
        return decorated


def _mode_for_agent_type(agent_type: str) -> str:
    normalized = str(agent_type or "").lower()
    if any(token in normalized for token in ("plan", "architect", "research")):
        return "plan"
    if any(token in normalized for token in ("review", "audit", "inspect")):
        return "review"
    return "build"


def _requires_worktree(agent_type: str) -> bool:
    normalized = str(agent_type or "").lower()
    return _mode_for_agent_type(normalized) == "build" and not normalized.startswith("shared-")


__all__ = ["TeamManager", "WorktreeIsolationError"]
