"""Shared subprocess lifecycle helpers used by shell and Git tools."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import Any, Dict


def build_process_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "PIP_NO_INPUT": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "POWERSHELL_TELEMETRY_OPTOUT": "1",
    })
    return env


def popen_platform_kwargs() -> Dict[str, Any]:
    if sys.platform.startswith("win"):
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def terminate_process_tree(process: subprocess.Popen[str], grace_seconds: int = 2) -> None:
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
            # 静默忽略：taskkill 失败，回退到 process.kill()
            try:
                process.kill()
            except Exception:
                # 静默忽略：进程清理非关键路径
                pass
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        # 静默忽略：SIGTERM 失败，回退到 process.terminate()
        try:
            process.terminate()
        except Exception:
            # 静默忽略：进程清理非关键路径
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
        # 静默忽略：SIGKILL 失败，回退到 process.kill()
        try:
            process.kill()
        except Exception:
            # 静默忽略：进程清理非关键路径
            pass


__all__ = ["build_process_env", "popen_platform_kwargs", "terminate_process_tree"]
