"""后台委托注册表：派单即返，汇聚点收结果。

同步委托（spawn 等结果）在 turn 内阻塞；本模块把执行搬进守护线程池，
派单工具调用即返回句柄，模型先做别的任务，稍后经 poll 工具汇聚。

线程安全：句柄表按锁读写；工作线程透传派单时的 Context（权限运行时等），
并强制并行标记——后台线程弹不了确认窗，ask 一律 fail-closed。
结果默认保留最近 50 条，溢出 pruning 最旧已终结项。
"""

from __future__ import annotations

import contextvars
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Semaphore
from typing import Any, Callable, Dict, List, Optional

JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
TERMINAL_JOB_STATUS = (JOB_DONE, JOB_FAILED, JOB_CANCELLED)

class _Cancelled(Exception):
    """协作式取消信号（内部用，不向外抛原文）。"""


MAX_POOL_WORKERS = 10
# 后台委托并发上限，防打满模型限流。
MAX_DELEGATE_CONCURRENCY = 10
# 未终结句柄堆积上限，超限时 submit 直接 fail-closed。
MAX_PENDING_JOBS = 32
# 已终结结果保留条数，溢出时按最旧优先裁剪。
KEEP_FINISHED = 50

# 并行委托上限：子 Agent 各自占一个模型调用与一份 worktree，无上限的并发会
# 打满限流并让结果汇聚失控。同步与异步两条路径共用同一信号量。
_DELEGATE_SEMAPHORE = Semaphore(MAX_DELEGATE_CONCURRENCY)


@contextmanager
def delegate_slot():
    """占一个委托执行位（超出上限的排队等待）。"""
    with _DELEGATE_SEMAPHORE:
        yield


def _prepare_worker_context() -> Any:
    """工作线程入口前置：并行标记 + 专属中止控制器，返回该控制器。"""
    from .permissions import _PARALLEL_BATCH
    from ..tools.context import ToolAbortController, set_abort_controller

    _PARALLEL_BATCH.set(True)
    controller = ToolAbortController()
    set_abort_controller(controller)
    return controller


@dataclass
class DelegateJob:
    """一个后台委托的快照。"""

    handle: str
    task: str
    agent_type: str
    status: str = JOB_PENDING
    result: str = ""
    error: str = ""
    turns: int = 1
    notified: bool = False
    cancel_requested: bool = False
    created_at: float = field(default_factory=time.monotonic)
    finished_at: Optional[float] = None
    resume_token: Any = field(default=None, repr=False)
    resume_fn: Any = field(default=None, repr=False)
    run_context: Any = field(default=None, repr=False)
    done_event: Any = field(default_factory=threading.Event, repr=False)
    abort_controller: Any = field(default=None, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        """转可序列化字典。"""
        return {
            "handle": self.handle,
            "task": self.task,
            "agent_type": self.agent_type,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "turns": self.turns,
        }


class AsyncDelegateRegistry:
    """后台委托的派单与汇聚点（进程级单例亦可自建）。"""

    def __init__(self, max_workers: int = MAX_POOL_WORKERS) -> None:
        self._max_workers = max(1, int(max_workers or 1))
        self._lock = threading.Lock()
        self._jobs: Dict[str, DelegateJob] = {}

    def _start_daemon(self, target: Any, *args: Any) -> None:
        """起守护线程跑后台活：进程退出不被后台委托 hang 住。"""
        thread = threading.Thread(target=target, args=args, daemon=True, name="saya-delegate")
        thread.start()

    def submit(
        self,
        spawn_fn: Callable[[str, str], Any],
        task: str,
        agent_type: str,
        resume_fn: Any = None,
    ) -> str:
        """提交后台委托，立即返回句柄。

        ``spawn_fn`` 可返回纯文本（无追问），或 ``(文本, 恢复令牌)`` 元组；
        后者配合 ``resume_fn(令牌, 追问)`` 启用追问。
        """
        handle = "d" + uuid.uuid4().hex[:11]
        job = DelegateJob(handle=handle, task=task, agent_type=agent_type, resume_fn=resume_fn)
        context = contextvars.copy_context()
        job.run_context = context
        with self._lock:
            live = sum(1 for j in self._jobs.values() if j.status not in TERMINAL_JOB_STATUS)
            if live >= MAX_PENDING_JOBS:
                raise RuntimeError("后台委托过多，稍后再派")
            self._jobs[handle] = job
            self._start_daemon(self._run, context, spawn_fn, handle)
            self._prune_locked()
        return handle

    def cancel(self, handle: str) -> DelegateJob:
        """取消委托：未开跑的直接撤，已开跑的发 abort 快速收尾。

        Python 杀不掉运行中的线程，取消是协作式的：工作线程入口与
        收尾处检查标记，子 Agent 工具经中止控制器快速失败。
        """
        with self._lock:
            job = self._jobs.get(handle)
            if job is None:
                raise KeyError("未知委托句柄: " + str(handle))
            if job.status in TERMINAL_JOB_STATUS:
                return job
            job.cancel_requested = True
            controller = job.abort_controller
        if controller is not None:
            try:
                controller.abort("cancelled")
            except Exception:
                pass
        return job

    def resume(self, handle: str, follow_up: str) -> DelegateJob:
        """追问已完成的委托：复用同一句柄再跑一轮，后台执行。"""
        follow_up = str(follow_up or "").strip()
        if not follow_up:
            raise ValueError("追问内容不能为空")
        with self._lock:
            job = self._jobs.get(handle)
            if job is None:
                raise KeyError("未知委托句柄: " + str(handle))
            if job.status not in TERMINAL_JOB_STATUS:
                raise RuntimeError("委托尚未终结，不能追问")
            if job.resume_fn is None or job.resume_token is None:
                raise RuntimeError("该委托不支持追问")
            job.status = JOB_RUNNING
            job.notified = False
            job.error = ""
            job.cancel_requested = False
            context = job.run_context or contextvars.copy_context()
            job.done_event.clear()
            self._start_daemon(self._run_resume, context, handle, follow_up)
            return job

    def _run_resume(self, context: Any, handle: str, follow_up: str) -> str:
        """追问工作体：同一 thread 续跑，结果追加轮次。"""
        def _body() -> str:
            controller = _prepare_worker_context()
            with self._lock:
                job = self._jobs.get(handle)
                if job is None:
                    raise KeyError("未知委托句柄: " + str(handle))
                if job.cancel_requested:
                    job.status = JOB_CANCELLED
                    job.error = "已取消"
                    job.finished_at = time.monotonic()
                    job.done_event.set()
                    return ""
                job.abort_controller = controller
                resume_fn, token = job.resume_fn, job.resume_token
            try:
                with delegate_slot():
                    if self._is_cancelled(handle):
                        raise _Cancelled()
                    result = resume_fn(token, follow_up)
            except Exception as exc:
                if isinstance(exc, _Cancelled) or self._is_cancelled(handle):
                    self._finish(handle, JOB_CANCELLED)
                else:
                    self._finish(handle, JOB_FAILED, error=str(exc)[:2000])
                return ""
            with self._lock:
                job = self._jobs.get(handle)
                if job is not None:
                    job.turns += 1
            self._finish(handle, JOB_DONE, result=str(result or ""))
            return str(result or "")

        try:
            return context.run(_body)
        finally:
            with self._lock:
                job = self._jobs.get(handle)
                if job is not None:
                    job.done_event.set()

    def peek_notifications(self) -> List[DelegateJob]:
        """看自上次以来新终结的委托（不标记，交互循环打印用）。"""
        with self._lock:
            return [j for j in self._jobs.values() if j.status in TERMINAL_JOB_STATUS and not j.notified]

    def pending_notifications(self) -> List[DelegateJob]:
        """取自上次以来新终结的委托（取即标已通知，模型工具用）。"""
        with self._lock:
            fresh = [j for j in self._jobs.values() if j.status in TERMINAL_JOB_STATUS and not j.notified]
            for job in fresh:
                job.notified = True
            return fresh

    def _is_cancelled(self, handle: str) -> bool:
        """该句柄是否被要求取消。"""
        with self._lock:
            job = self._jobs.get(handle)
            return bool(job is not None and job.cancel_requested)

    def _finish(self, handle: str, status: str, result: str = "", error: str = "") -> None:
        """落终结态并唤醒等待者（取消优先于成功/失败）。"""
        with self._lock:
            job = self._jobs.get(handle)
            if job is None:
                return
            if job.cancel_requested:
                status, result, error = JOB_CANCELLED, "", "已取消"
            job.status = status
            job.result = result
            job.error = error
            job.notified = False
            job.finished_at = time.monotonic()
            job.done_event.set()

    def _run(self, context: Any, spawn_fn: Callable[[str, str], str], handle: str) -> str:
        """工作线程体：透传派单上下文、强制并行标记、专属中止控制器。"""
        def _body() -> str:
            controller = _prepare_worker_context()
            with self._lock:
                job = self._jobs.get(handle)
                if job is None:
                    raise KeyError("未知委托句柄: " + str(handle))
                if job.cancel_requested:
                    job.status = JOB_CANCELLED
                    job.error = "已取消"
                    job.finished_at = time.monotonic()
                    job.done_event.set()
                    return ""
                job.status = JOB_RUNNING
                job.abort_controller = controller
            try:
                with delegate_slot():
                    if self._is_cancelled(handle):
                        raise _Cancelled()
                    raw = spawn_fn(self._jobs[handle].task, self._jobs[handle].agent_type)
                if isinstance(raw, tuple) and len(raw) == 2:
                    result, token = raw
                    with self._lock:
                        job = self._jobs.get(handle)
                        if job is not None:
                            job.resume_token = token
                    result = str(result or "")
                else:
                    result = str(raw or "")
            except Exception as exc:
                # 状态已落盘 + 事件已唤醒，不再向外抛：守护线程无处可接，
                # 抛了只会变成未处理线程异常噪音，poll 看状态即可。
                if isinstance(exc, _Cancelled) or self._is_cancelled(handle):
                    self._finish(handle, JOB_CANCELLED)
                else:
                    self._finish(handle, JOB_FAILED, error=str(exc)[:2000])
                return ""
            self._finish(handle, JOB_DONE, result=result)
            return result

        try:
            return context.run(_body)
        finally:
            with self._lock:
                job = self._jobs.get(handle)
                if job is not None:
                    job.done_event.set()

    def poll(self, handle: str, wait_seconds: float = 0) -> DelegateJob:
        """查句柄状态；wait_seconds>0 时最多阻塞等这么久。"""
        with self._lock:
            job = self._jobs.get(handle)
            if job is None:
                raise KeyError("未知委托句柄: " + str(handle))
            event = job.done_event
        if wait_seconds > 0 and job.status not in TERMINAL_JOB_STATUS:
            event.wait(timeout=wait_seconds)
        with self._lock:
            return self._jobs[handle]

    def list_jobs(self) -> List[DelegateJob]:
        """列出全部未 pruning 句柄快照。"""
        with self._lock:
            return list(self._jobs.values())

    def _prune_locked(self) -> None:
        """保留最近已终结项，溢出删最旧。调用方需持锁。"""
        finished = [h for h, j in self._jobs.items() if j.status in TERMINAL_JOB_STATUS]
        overflow = len(finished) - KEEP_FINISHED
        if overflow <= 0:
            return
        for handle in list(self._jobs)[:]:
            if overflow <= 0:
                break
            if self._jobs[handle].status in TERMINAL_JOB_STATUS:
                del self._jobs[handle]
                overflow -= 1

    def shutdown(self) -> None:
        """预留关闭入口：工作线程均为守护线程，随进程退出，不阻塞。"""


_REGISTRY: Optional[AsyncDelegateRegistry] = None
_REGISTRY_LOCK = threading.Lock()


def get_delegate_registry() -> AsyncDelegateRegistry:
    """取进程级后台委托注册表（惰性单例）。"""
    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = AsyncDelegateRegistry()
        return _REGISTRY


__all__ = [
    "AsyncDelegateRegistry",
    "DelegateJob",
    "MAX_DELEGATE_CONCURRENCY",
    "delegate_slot",
    "JOB_DONE",
    "JOB_FAILED",
    "JOB_PENDING",
    "JOB_RUNNING",
    "KEEP_FINISHED",
    "MAX_POOL_WORKERS",
    "TERMINAL_JOB_STATUS",
    "get_delegate_registry",
]
