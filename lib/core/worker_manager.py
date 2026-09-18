"""非交互团队 worker 的持久化生命周期管理。

负责派生 headless 子进程并跟踪状态与回收结果。
核心类：WorkerManager、WorkerState、WorkerStatus。
调用链：TeamManager→WorkerManager.spawn→team_worker。"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from .private_io import ensure_private_dir, write_private_json


MAX_TEAM_WORKERS = 4
_WORKER_ID_RE = re.compile(r"^w[0-9a-f]{8}$")


class WorkerStatus(Enum):
    """worker 生命周期状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    KILLED = "killed"


@dataclass
class WorkerState:
    """单个子 Agent 进程的持久化状态。"""

    worker_id: str
    status: WorkerStatus = WorkerStatus.PENDING
    pid: int | None = None
    start_time: float = 0.0
    end_time: float | None = None
    exit_code: int | None = None
    config: dict[str, Any] = field(default_factory=dict)
    worktree: str | None = None
    stdout_path: str = ""
    stderr_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkerState":
        """从字典恢复 worker 状态。"""
        return cls(
            worker_id=str(payload["worker_id"]),
            status=WorkerStatus(str(payload.get("status", "pending"))),
            pid=payload.get("pid"),
            start_time=float(payload.get("start_time") or 0.0),
            end_time=payload.get("end_time"),
            exit_code=payload.get("exit_code"),
            config=dict(payload.get("config") or {}),
            worktree=payload.get("worktree"),
            stdout_path=str(payload.get("stdout_path") or ""),
            stderr_path=str(payload.get("stderr_path") or ""),
        )


class WorkerManager:
    """启动并监管受数量约束的非交互子 Agent 进程。"""

    def __init__(self, base_dir: Path, *, max_workers: int = MAX_TEAM_WORKERS):
        self.base_dir = Path(base_dir).expanduser().resolve()
        ensure_private_dir(self.base_dir)
        self.max_workers = max(1, int(max_workers))
        self._workers: dict[str, WorkerState] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._load_states()

    @staticmethod
    def new_worker_id() -> str:
        """生成新的 worker 标识。"""
        return f"w{str(uuid4()).replace('-', '')[:8]}"

    def spawn(
        self,
        agent_config: dict[str, Any],
        *,
        worker_id: str | None = None,
    ) -> str:
        """启动消费 mailbox 的 worker 进程并返回其 ID。"""
        if self.active_count() >= self.max_workers:
            raise RuntimeError(f"最多同时运行 {self.max_workers} 个子 Agent")

        worker_id = worker_id or self.new_worker_id()
        if not _WORKER_ID_RE.fullmatch(worker_id):
            raise ValueError("invalid worker_id")

        workspace = Path(agent_config.get("workspace", ".")).expanduser().resolve()
        if not workspace.is_dir():
            raise NotADirectoryError(f"workspace is not a directory: {workspace}")
        sayacode_home = str(agent_config.get("sayacode_home") or "").strip()
        if not sayacode_home:
            from .paths import SayacodePaths

            sayacode_home = str(SayacodePaths.resolve().home)

        stdout_path = self.base_dir / f"{worker_id}.stdout.json"
        stderr_path = self.base_dir / f"{worker_id}.stderr.log"
        state = WorkerState(
            worker_id=worker_id,
            status=WorkerStatus.PENDING,
            start_time=time.time(),
            config={
                "agent_type": str(agent_config.get("agent_type") or "builder"),
                "workspace": str(workspace),
                "mode": str(agent_config.get("mode") or "build"),
                "sayacode_home": sayacode_home,
                "source_workspace": str(agent_config.get("source_workspace") or workspace),
                "branch": str(agent_config.get("branch") or ""),
                "source_commit": str(agent_config.get("source_commit") or ""),
            },
            worktree=str(agent_config.get("worktree") or "") or None,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )
        self._workers[worker_id] = state
        self._persist_state(state)

        cmd = [
            sys.executable,
            "-m",
            "lib.core.team_worker",
            "--base-dir",
            state.config["sayacode_home"],
            "--worker-id",
            worker_id,
            "--workspace",
            str(workspace),
            "--mode",
            state.config["mode"],
        ]
        popen_kwargs: dict[str, Any] = {
            "cwd": str(workspace),
            "text": True,
        }
        try:
            from .process_env import build_process_env

            popen_kwargs["env"] = build_process_env()
        except Exception:
            pass
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            popen_kwargs["start_new_session"] = True

        try:
            with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
                "w", encoding="utf-8"
            ) as stderr_file:
                proc = subprocess.Popen(
                    cmd,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    **popen_kwargs,
                )
            state.pid = proc.pid
            state.status = WorkerStatus.RUNNING
            self._processes[worker_id] = proc
        except Exception:
            state.status = WorkerStatus.FAILED
            state.exit_code = -1
            state.end_time = time.time()
        self._persist_state(state)
        return worker_id

    def kill(self, worker_id: str, timeout: float = 5.0) -> bool:
        """终止由本 manager 实例持有的 worker。"""
        state = self._workers.get(worker_id)
        proc = self._processes.get(worker_id)
        if not state or not proc or state.status != WorkerStatus.RUNNING:
            return False

        try:
            if sys.platform == "win32":
                completed = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=timeout,
                )
                if completed.returncode != 0:
                    return False
            else:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=2)
            state.status = WorkerStatus.KILLED
            state.exit_code = proc.poll()
            state.end_time = time.time()
            self._persist_state(state)
            return True
        except Exception:
            return False

    def is_active(self, worker_id: str) -> bool:
        """返回 worker 是否仍在运行。"""
        state = self.get_state(worker_id)
        return bool(state and state.status == WorkerStatus.RUNNING)

    def cleanup_all(self, timeout: float = 10.0) -> int:
        """终止本进程持有的 worker；保留持久化结果。"""
        count = 0
        for worker_id in list(self._workers):
            if self.is_active(worker_id) and self.kill(worker_id, timeout=min(timeout, 5.0)):
                count += 1
        return count

    def get_state(self, worker_id: str) -> WorkerState | None:
        """返回 worker 状态并同步进程退出。"""
        state = self._workers.get(worker_id)
        if state is None:
            return None

        previous = (state.status, state.exit_code, state.end_time)
        proc = self._processes.get(worker_id)
        if proc is not None and state.status == WorkerStatus.RUNNING:
            returncode = proc.poll()
            if returncode is not None:
                state.status = WorkerStatus.COMPLETED if returncode == 0 else WorkerStatus.FAILED
                state.exit_code = returncode
                state.end_time = time.time()
        elif state.status == WorkerStatus.RUNNING:
            result = self.get_result(worker_id, refresh=False)
            if result is not None:
                state.status = WorkerStatus.COMPLETED if result.get("ok") else WorkerStatus.FAILED
                state.exit_code = 0 if result.get("ok") else 1
                state.end_time = state.end_time or time.time()
            elif state.pid and not _pid_is_running(state.pid):
                state.status = WorkerStatus.FAILED
                state.exit_code = -1
                state.end_time = time.time()

        if previous != (state.status, state.exit_code, state.end_time):
            self._persist_state(state)
        return state

    def get_result(
        self,
        worker_id: str,
        *,
        refresh: bool = True,
    ) -> dict[str, Any] | None:
        """读取 worker 结果，失败时用日志兜底。"""
        state = self._workers.get(worker_id)
        if state is None:
            return None
        if refresh:
            state = self.get_state(worker_id) or state
            if state.status in {WorkerStatus.PENDING, WorkerStatus.RUNNING}:
                return None

        stdout_path = self._safe_output_path(state.stdout_path)
        if stdout_path and stdout_path.is_file():
            try:
                text = stdout_path.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    payload = json.loads(text.splitlines()[-1])
                    if isinstance(payload, dict):
                        return payload
            except (OSError, json.JSONDecodeError):
                pass

        stderr = ""
        stderr_path = self._safe_output_path(state.stderr_path)
        if stderr_path and stderr_path.is_file():
            try:
                stderr = stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:]
            except OSError:
                pass
        if state.status in {WorkerStatus.FAILED, WorkerStatus.KILLED}:
            return {
                "ok": False,
                "worker_id": worker_id,
                "error": stderr.strip() or f"worker {state.status.value}",
            }
        return None

    def list_workers(self) -> list[WorkerState]:
        """列出全部 worker 状态。"""
        states = [self.get_state(worker_id) for worker_id in self._workers]
        return sorted(
            [state for state in states if state is not None],
            key=lambda state: state.start_time,
        )

    def active_count(self) -> int:
        """返回运行中 worker 数量。"""
        return sum(1 for state in self.list_workers() if state.status == WorkerStatus.RUNNING)

    def _state_path(self, worker_id: str) -> Path:
        return self.base_dir / f"{worker_id}.state.json"

    def _safe_output_path(self, raw_path: str) -> Path | None:
        if not raw_path:
            return None
        path = Path(raw_path).expanduser().resolve()
        try:
            path.relative_to(self.base_dir)
        except ValueError:
            return None
        return path

    def _persist_state(self, state: WorkerState) -> None:
        write_private_json(self._state_path(state.worker_id), state.to_dict())

    def _load_states(self) -> None:
        for path in self.base_dir.glob("w????????.state.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                state = WorkerState.from_dict(payload)
                if _WORKER_ID_RE.fullmatch(state.worker_id):
                    self._workers[state.worker_id] = state
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue


def _pid_is_running(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


__all__ = [
    "MAX_TEAM_WORKERS",
    "WorkerManager",
    "WorkerState",
    "WorkerStatus",
]
