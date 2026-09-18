"""Shell 与 Git 共用的 subprocess 生命周期辅助函数。

提供跨平台启动参数与进程树终止能力，核心为 popen_platform_kwargs 与 terminate_process_tree。
调用链为 shell_tools/git_tools 执行入口调用本模块完成拉起、超时回收与清理。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Any, Dict

from ..core.process_env import build_process_env


def popen_platform_kwargs() -> Dict[str, Any]:
    """返回跨平台 subprocess 启动参数。"""
    if sys.platform.startswith("win"):
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def terminate_process_tree(process: subprocess.Popen[str], grace_seconds: int = 2) -> None:
    """终止进程树并回收子进程。"""
    if process.poll() is not None:
        return

    if sys.platform.startswith("win"):
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception:
            # 忽略 taskkill 失败，回退到 process.kill()。
            try:
                process.kill()
            except Exception:
                # 忽略进程清理失败，继续退出清理流程。
                pass
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        # 忽略 SIGTERM 失败，回退到 process.terminate()。
        try:
            process.terminate()
        except Exception:
            # 忽略进程清理失败，继续退出清理流程。
            return

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return
        time.sleep(0.05)

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        # 忽略 SIGKILL 失败，回退到 process.kill()。
        try:
            process.kill()
        except Exception:
            # 忽略进程清理失败，继续退出清理流程。
            pass


__all__ = ["build_process_env", "popen_platform_kwargs", "terminate_process_tree"]
