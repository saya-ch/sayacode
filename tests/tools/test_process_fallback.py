"""Windows 外层 Job 拒绝嵌套时的进程树收尾。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from sayacode.process import (
    attach_process_tree,
    close_process_tree,
    process_creation_options,
    stop_process_tree,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 使用 Job fallback")


def _deny_nested_job(monkeypatch: pytest.MonkeyPatch) -> None:
    import pywintypes
    import win32job

    def denied(*_args: object) -> None:
        raise pywintypes.error(5, "AssignProcessToJobObject", "拒绝访问")

    monkeypatch.setattr(win32job, "AssignProcessToJobObject", denied)


async def _parent_with_child(marker: Path) -> asyncio.subprocess.Process:
    child = (
        "import time; from pathlib import Path; "
        f"time.sleep(1.2); Path({str(marker)!r}).write_text('escaped')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{child!r}], "
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        "time.sleep(.25)"
    )
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        parent,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **process_creation_options(),
    )


async def test_access_denied_fallback_stops_child_after_parent_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deny_nested_job(monkeypatch)
    marker = tmp_path / "escaped.txt"
    process = await _parent_with_child(marker)
    tree = attach_process_tree(process)
    assert tree is not None and hasattr(tree, "_known")
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
        assert tree._known, "fallback 未捕获任何子进程"
        await asyncio.wait_for(stop_process_tree(process, tree), timeout=3)
        await asyncio.sleep(1.3)
        assert not marker.exists()
    finally:
        close_process_tree(tree)


async def test_access_denied_fallback_closes_child_after_normal_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _deny_nested_job(monkeypatch)
    marker = tmp_path / "escaped.txt"
    process = await _parent_with_child(marker)
    tree = attach_process_tree(process)
    assert tree is not None and hasattr(tree, "_known")
    await asyncio.wait_for(process.communicate(), timeout=3)
    assert tree._known, "fallback 未捕获任何子进程"
    close_process_tree(tree)
    await asyncio.sleep(1.3)
    assert not marker.exists()
