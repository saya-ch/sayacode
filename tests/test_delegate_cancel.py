"""提前取消测试：未跑撤回、在跑中止、守护线程。"""

import threading
import time

from lib.core.delegate_pool import AsyncDelegateRegistry
from lib.tools.delegate_tools import create_cancel_tool


def _cancel_tool(registry):
    return create_cancel_tool(registry.cancel)


def test_cancel_running_job_aborts_and_marks_cancelled():
    registry = AsyncDelegateRegistry(max_workers=2)
    release = threading.Event()
    observed = {}

    def spawn(task, kind):
        from lib.tools.context import get_abort_controller

        observed["controller"] = get_abort_controller()
        release.wait()
        return "不该到这里"

    handle = registry.submit(spawn, "慢活", "builder")
    assert registry.poll(handle, wait_seconds=5).status == "running"
    tool = _cancel_tool(registry)
    out = tool.invoke({"handle_id": handle})
    assert "已取消" in out
    release.set()
    final = registry.poll(handle, wait_seconds=5)
    assert final.status == "cancelled"
    assert observed["controller"].is_aborted


def test_cancel_finished_is_noop_and_unknown_errors():
    registry = AsyncDelegateRegistry(max_workers=1)
    handle = registry.submit(lambda task, kind: "好", "快活", "builder")
    assert registry.poll(handle, wait_seconds=5).status == "done"
    tool = _cancel_tool(registry)
    assert "无需取消" in tool.invoke({"handle_id": handle})
    assert "未知" in tool.invoke({"handle_id": "d_nope"})
    assert "不能为空" in tool.invoke({"handle_id": ""})


def test_worker_threads_are_daemon():
    registry = AsyncDelegateRegistry(max_workers=1)
    gate = threading.Event()
    seen = {}

    def spawn(task, kind):
        seen["daemon"] = threading.current_thread().daemon
        gate.wait()
        return "ok"

    handle = registry.submit(spawn, "挂起活", "builder")
    assert registry.poll(handle, wait_seconds=5).status == "running"
    deadline = time.monotonic() + 5
    while "daemon" not in seen and time.monotonic() < deadline:
        time.sleep(0.05)
    assert seen.get("daemon") is True, "后台线程必须守护，进程退出不被 hang 住"
    registry.cancel(handle)
    gate.set()
    assert registry.poll(handle, wait_seconds=5).status == "cancelled"
