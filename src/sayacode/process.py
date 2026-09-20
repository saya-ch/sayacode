"""跨平台子进程树创建与清理。"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import uuid
from typing import Any


def process_creation_options() -> dict[str, Any]:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def attach_process_tree(process: asyncio.subprocess.Process) -> Any:
    """给视窗进程绑定内核作业寿命。Unix 沿用自身会话。"""
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
    except BaseException:
        job.Close()
        raise


def close_process_tree(job: Any) -> None:
    if job is not None:
        job.Close()


async def stop_process_tree(process: asyncio.subprocess.Process, job: Any = None) -> None:
    """原进程已退出也要停掉派生子进程。"""
    if sys.platform == "win32":
        if job is not None:
            import win32job

            win32job.TerminateJobObject(job, 1)
        elif process.returncode is None:
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
            await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()
