"""跨平台子进程树创建与清理。视窗用作业对象管一家人，Unix 用进程组管一家人，停掉就要停全。"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import uuid
from typing import Any


class _WindowsFallbackTree:
    """外层 Job 拒绝嵌套时，跟踪子孙 PID 并在结束时收尾。"""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        import psutil

        self.process = process
        self._psutil = psutil
        self._known: dict[int, Any] = {}
        self._closed = False
        self._capture()
        self._tracker = asyncio.get_running_loop().create_task(self._track())

    def _capture(self) -> None:
        """持续保留曾经的子孙句柄，父进程先退出也能找到它们。"""
        roots = []
        try:
            roots.append(self._psutil.Process(self.process.pid))
        except self._psutil.NoSuchProcess:
            pass
        roots.extend(self._known.values())
        for root in roots:
            try:
                for child in root.children(recursive=True):
                    self._known[child.pid] = child
            except (self._psutil.NoSuchProcess, self._psutil.AccessDenied):
                continue

    async def _track(self) -> None:
        try:
            while not self._closed:
                self._capture()
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass

    def _kill_known(self) -> None:
        self._capture()
        for child in reversed(list(self._known.values())):
            try:
                child.kill()
            except self._psutil.NoSuchProcess:
                pass

    async def stop(self) -> None:
        self._closed = True
        self._tracker.cancel()
        await asyncio.gather(self._tracker, return_exceptions=True)
        self._kill_known()
        if self.process.returncode is None:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
        await self.process.wait()

    def close(self) -> None:
        """同步关闭与 Windows Job 句柄同义：已退出的父进程不能留下子孙。"""
        self._closed = True
        self._tracker.cancel()
        self._kill_known()
        if self.process.returncode is None:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass


def _access_denied(error: BaseException) -> bool:
    code = getattr(error, "winerror", None)
    if code is None and error.args and isinstance(error.args[0], int):
        code = error.args[0]
    return code == 5


def process_creation_options() -> dict[str, Any]:
    """给出建子进程时的平台选项。传入无，返回选项字典。视窗藏黑窗口，Unix 独立成组，调用方直接解包传给建进程函数。"""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def attach_process_tree(process: asyncio.subprocess.Process) -> Any:
    """优先使用 Windows Job；外层 Job 拒绝嵌套时跟踪进程子树。"""
    if sys.platform != "win32":
        return None
    import win32api
    import win32con
    import win32job

    job = win32job.CreateJobObject(None, f"Local\\SAYACODE-{uuid.uuid4().hex}")
    try:
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        handle = win32api.OpenProcess(
            win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid
        )
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        return job
    except BaseException as error:
        job.Close()
        if isinstance(error, Exception) and _access_denied(error):
            try:
                return _WindowsFallbackTree(process)
            except ImportError:
                # 旧安装缺少可选的 Windows 依赖时仍能执行普通命令；停止时用 taskkill。
                return None
        raise


def close_process_tree(job: Any) -> None:
    """关掉作业句柄，只收管理权不杀进程。传入作业句柄或空，返回无。空直接过，进程还在跑要停请走停进程函数。"""
    if job is not None:
        if isinstance(job, _WindowsFallbackTree):
            job.close()
        else:
            job.Close()


async def stop_process_tree(process: asyncio.subprocess.Process, job: Any = None) -> None:
    """原进程已退出也要停掉派生子进程。传入进程和作业句柄，返回无。视窗优先用作业全停，Unix 按进程组杀，进程已不在就当成功。"""
    if sys.platform == "win32":
        if isinstance(job, _WindowsFallbackTree):
            await job.stop()
        elif job is not None:
            import win32job

            win32job.TerminateJobObject(job, 1)
        else:
            # 最后兜底仍尝试按 PID 清树；父进程已退出时只能尽力而为。
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    **process_creation_options(),
                )
                await asyncio.wait_for(killer.wait(), timeout=10)
            except (OSError, TimeoutError):
                if process.returncode is None:
                    process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()
