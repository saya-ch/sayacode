"""
Worker 生命周期管理 — 参考 Claude Code task system.

管理子 Agent 进程的完整生命周期：
- spawn: 启动新 Worker（subprocess）
- kill: 终止指定 Worker
- cleanup_all: SIGINT 级联清理所有 Worker
- 状态追踪和查询
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class WorkerStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


@dataclass
class WorkerState:
    """单个 Worker 的状态快照。"""
    worker_id: str
    status: WorkerStatus = WorkerStatus.PENDING
    pid: int | None = None
    start_time: float = 0.0
    end_time: float | None = None
    exit_code: int | None = None
    config: dict[str, Any] = field(default_factory=dict)
    worktree: str | None = None


class WorkerManager:
    """子 Agent 进程管理器。

    用法:
        wm = WorkerManager(Path("/tmp/sayacode/workers"))
        worker_id = wm.spawn({"agent_type": "code-reviewer", "workspace": "/project"})
        state = wm.get_state(worker_id)
        wm.kill(worker_id)
        wm.cleanup_all()
    """

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._workers: dict[str, WorkerState] = {}
        self._processes: dict[str, subprocess.Popen] = {}

    def spawn(self, agent_config: dict[str, Any]) -> str:
        """启动一个子 Agent Worker 进程。返回 worker_id。"""
        from uuid import uuid4
        worker_id = f"w{str(uuid4())[:8]}"

        state = WorkerState(
            worker_id=worker_id,
            status=WorkerStatus.PENDING,
            start_time=time.time(),
            config=agent_config,
        )

        # 构建子进程命令
        cmd = [
            sys.executable, "-m", "lib",
            "--workspace", str(agent_config.get("workspace", ".")),
            "--mode", agent_config.get("mode", "build"),
        ]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(agent_config.get("workspace", ".")),
            )
            state.pid = proc.pid
            state.status = WorkerStatus.RUNNING
            self._processes[worker_id] = proc
        except Exception as e:
            state.status = WorkerStatus.FAILED
            state.exit_code = -1

        self._workers[worker_id] = state
        return worker_id

    def kill(self, worker_id: str, timeout: float = 5.0) -> bool:
        """终止指定 Worker。Windows 用 taskkill, Unix 用 SIGTERM→SIGKILL。"""
        state = self._workers.get(worker_id)
        proc = self._processes.get(worker_id)

        if not state or not proc:
            return False

        if state.status != WorkerStatus.RUNNING:
            return False

        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=timeout,
                )
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)

            state.status = WorkerStatus.KILLED
            state.end_time = time.time()
            return True
        except Exception:
            return False

    def is_active(self, worker_id: str) -> bool:
        """检查 Worker 是否仍在运行。"""
        state = self._workers.get(worker_id)
        if not state:
            return False
        proc = self._processes.get(worker_id)
        if not proc:
            return False
        return proc.poll() is None and state.status == WorkerStatus.RUNNING

    def cleanup_all(self, timeout: float = 10.0) -> int:
        """SIGINT 级联清理：终止所有 Worker，清理工作树。返回清理的 Worker 数。"""
        count = 0
        for worker_id in list(self._workers.keys()):
            if self.is_active(worker_id):
                if self.kill(worker_id, timeout=2):
                    count += 1

        # 清理工作树目录
        for state in self._workers.values():
            if state.worktree and Path(state.worktree).exists():
                try:
                    import shutil
                    shutil.rmtree(state.worktree, ignore_errors=True)
                except Exception:
                    pass

        return count

    def get_state(self, worker_id: str) -> WorkerState | None:
        """获取 Worker 状态。"""
        state = self._workers.get(worker_id)
        if not state:
            return None

        # 刷新运行中进程的状态
        proc = self._processes.get(worker_id)
        if proc and state.status == WorkerStatus.RUNNING:
            returncode = proc.poll()
            if returncode is not None:
                state.status = WorkerStatus.COMPLETED if returncode == 0 else WorkerStatus.FAILED
                state.exit_code = returncode
                state.end_time = time.time()

        return state

    def list_workers(self) -> list[WorkerState]:
        """列出所有 Worker。"""
        return list(self._workers.values())

    def active_count(self) -> int:
        """活跃 Worker 数量。"""
        return sum(1 for w in self._workers.values() if w.status == WorkerStatus.RUNNING)


__all__ = ["WorkerManager", "WorkerState", "WorkerStatus"]
