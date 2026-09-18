"""异步委托测试：派单即返、汇聚、上下文透传。"""

import threading
import time

from lib.core.delegate_pool import AsyncDelegateRegistry, get_delegate_registry
from lib.tools.delegate_tools import create_async_delegate_tools


def _tools(spawn, registry=None):
    tools = {t.name: t for t in create_async_delegate_tools(spawn, registry)}
    assert set(tools) == {"delegate_async", "delegate_poll"}
    return tools


def test_dispatch_returns_immediately_while_work_runs():
    started = threading.Event()
    release = threading.Event()

    def spawn(task, kind):
        started.set()
        assert release.wait(timeout=5)
        return "后台结果"

    registry = AsyncDelegateRegistry(max_workers=2)
    tools = _tools(spawn, registry)
    begin = time.monotonic()
    out = tools["delegate_async"].invoke({"task": "慢活"})
    elapsed = time.monotonic() - begin
    assert elapsed < 1.0, "派单必须立即返回，不能等子 Agent 跑完"
    handle = out.split("已派单：")[1].split("。")[0].strip()
    assert started.wait(timeout=5)
    assert "未完成" in tools["delegate_poll"].invoke({"handle_id": handle})
    release.set()
    done = tools["delegate_poll"].invoke({"handle_id": handle, "wait_seconds": 5})
    assert "后台结果" in done


def test_poll_unknown_handle_and_validation():
    registry = AsyncDelegateRegistry(max_workers=1)
    tools = _tools(lambda task, kind: "ok", registry)
    assert "不能为空" in tools["delegate_async"].invoke({"task": "  "})
    assert "未知句柄" in tools["delegate_poll"].invoke({"handle_id": "d_nope"})
    assert "不能为空" in tools["delegate_poll"].invoke({"handle_id": ""})


def test_background_worker_forces_parallel_flag():
    from lib.core.permissions import is_parallel_batch

    seen = {}

    def spawn(task, kind):
        seen["flag"] = is_parallel_batch()
        return "ok"

    registry = AsyncDelegateRegistry(max_workers=1)
    tools = _tools(spawn, registry)
    out = tools["delegate_async"].invoke({"task": "查标记"})
    handle = out.split("已派单：")[1].split("。")[0].strip()
    assert "已完成" in tools["delegate_poll"].invoke({"handle_id": handle, "wait_seconds": 5})
    assert seen.get("flag") is True, "后台线程必须强制并行标记（ask fail-closed）"


def test_failed_spawn_surfaces_in_poll():
    def boom(task, kind):
        raise RuntimeError("炸了")

    registry = AsyncDelegateRegistry(max_workers=1)
    tools = _tools(boom, registry)
    out = tools["delegate_async"].invoke({"task": "会炸的活"})
    handle = out.split("已派单：")[1].split("。")[0].strip()
    assert "失败" in tools["delegate_poll"].invoke({"handle_id": handle, "wait_seconds": 5})


def test_singleton_registry_shared():
    assert get_delegate_registry() is get_delegate_registry()
