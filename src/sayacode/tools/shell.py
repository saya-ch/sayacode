"""Shell 工具负责超时、输入输出和进程树取消。"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import uuid
from typing import Any

from langchain.tools import ToolRuntime, tool

from ..paths import context_value
from ..process import (
    attach_process_tree,
    close_process_tree,
    process_creation_options,
    stop_process_tree,
)
from .files import _output_dir, _path


async def _run_process(
    args: list[str], runtime: ToolRuntime, cwd: str, timeout: float, input_text: str | None = None
) -> dict[str, Any]:
    if not 0 < timeout <= 3600:
        raise ValueError("timeout must be between 0 and 3600 seconds")
    directory = _path(runtime, cwd)
    output_dir = _output_dir(runtime)
    identifier = uuid.uuid4().hex
    stdout_path, stderr_path = (
        output_dir / f"{identifier}.stdout.txt",
        output_dir / f"{identifier}.stderr.txt",
    )
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_PAGER": "cat", "PAGER": "cat"}
    timed_out = False
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=directory,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            **process_creation_options(),
        )
        job = None
        try:
            job = attach_process_tree(process)
            await asyncio.wait_for(
                process.communicate(input_text.encode("utf-8") if input_text is not None else None),
                timeout,
            )
        except TimeoutError:
            timed_out = True
            await stop_process_tree(process, job)
        except asyncio.CancelledError:
            await asyncio.shield(stop_process_tree(process, job))
            raise
        except BaseException:
            await stop_process_tree(process, job)
            raise
        finally:
            # 有限命令不能留下未跟踪的派生进程。
            if sys.platform == "win32":
                close_process_tree(job)
            else:
                await stop_process_tree(process)
    result: dict[str, Any] = {"exit_code": process.returncode, "timed_out": timed_out}
    preview_limit = int(context_value(runtime.context, "output_limit_bytes", 64 * 1024))
    if preview_limit <= 0:
        preview_limit = 64 * 1024
    for name, path in (("stdout", stdout_path), ("stderr", stderr_path)):
        with path.open("rb") as handle:
            result[name] = handle.read(preview_limit).decode("utf-8", "ignore")
        result[f"{name}_file"] = path.name
        result[f"{name}_bytes"] = path.stat().st_size
    return result


@tool
async def execute_command_tool(
    command: str,
    runtime: ToolRuntime[Any],
    cwd: str = ".",
    timeout: float = 120,
    input_text: str | None = None,
) -> dict[str, Any]:
    """运行非交互 PowerShell 或 sh 命令；完整输出保存在文件中。"""
    if sys.platform == "win32":
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            raise RuntimeError("PowerShell is not installed")
        args = [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    else:
        args = ["/bin/sh", "-c", command]
    return await _run_process(args, runtime, cwd, timeout, input_text)
