"""Git 标准命令行的只读查询工具。"""

from __future__ import annotations

import os
from typing import Any, Literal

from langchain.tools import ToolRuntime, tool

from .files import _path
from .shell import _run_process


def _validate_git_argument(value: str, label: str, *, required: bool = False) -> None:
    if required and not value.strip():
        raise ValueError(f"{label} must not be empty")
    if value.startswith("-") or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError(f"{label} cannot contain control characters or begin with '-'")


@tool
async def git(
    action: Literal["status", "diff", "log", "branch", "remote", "show"],
    runtime: ToolRuntime[Any],
    cwd: str = ".",
    ref: str = "",
    paths: list[str] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """执行只读 Git 查询；Git 变更由 Shell 工具处理。"""
    _validate_git_argument(ref, "Git reference")
    directory = _path(runtime, cwd)
    safe_paths = []
    for value in paths or []:
        target = _path(runtime, value)
        if not target.is_relative_to(directory):
            raise ValueError("Git paths must be within cwd")
        safe_paths.append(str(target.relative_to(directory)))
    args = ["git", "--no-pager"]
    args += [
        "--no-optional-locks",
        "-c",
        "core.fsmonitor=false",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "log.showSignature=false",
        action,
    ]
    if action in {"diff", "show", "log"}:
        args += ["--no-ext-diff", "--no-textconv"]
    if action == "status":
        args += ["--short", "--branch"]
    elif action == "log":
        args += [f"-{max(1, min(limit, 200))}", "--oneline"]
    elif action == "branch":
        args += ["--list", "--all"]
    elif action == "remote":
        args += ["-v"]
    elif action in {"show", "diff"} and ref:
        args.append(ref)
    if safe_paths:
        if action not in {"diff", "show", "log"}:
            raise ValueError("paths are supported for diff, show, and log")
        args += ["--", *safe_paths]
    return await _run_process(args, runtime, cwd, 120)
