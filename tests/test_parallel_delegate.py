"""并行委托测试：真并发、上限熔断、ask fail-closed。"""

import threading
import time

from lib.core.permissions import (
    PermissionRuntime,
    SessionPermissionState,
    enforce_tool_permission,
    parallel_batch_session,
    permission_runtime_session,
)
from lib.core.tool_meta import ToolMeta, register_tool_meta
from lib.tools.batch_executor import ToolBatchExecutor, ToolCallRequest
from lib.tools.delegate_tools import MAX_DELEGATE_CONCURRENCY, create_delegate_tool


def _register_safe(name):
    register_tool_meta(ToolMeta.safe_default(name, is_concurrency_safe=True, tool_group="test"))


def test_parallel_delegates_overlap_in_time(tmp_path):
    events = []
    lock = threading.Lock()

    def spawn(task, kind):
        with lock:
            events.append(("in", task))
        time.sleep(0.3)
        with lock:
            events.append(("out", task))
        return "done:" + task

    tool = create_delegate_tool(spawn)
    _register_safe("delegate_to_subagent")
    calls = [
        ToolCallRequest(tool_name="delegate_to_subagent", arguments={"task": t}, tool_call_id=str(i))
        for i, t in enumerate(["a", "b"])
    ]
    executor = ToolBatchExecutor(tool_map={"delegate_to_subagent": lambda **kw: tool.invoke(kw)})
    started = time.monotonic()
    result = executor.execute_batch(calls)
    elapsed = time.monotonic() - started
    assert all(not r.is_error for r in result.results)
    assert elapsed < 0.55, "两次 0.3s 委托应重叠执行，而非串行 0.6s"
    assert sorted(r.result for r in result.results) == ["done:a", "done:b"]


def test_delegate_concurrency_capped():
    live = 0
    peak = 0
    lock = threading.Lock()

    def spawn(task, kind):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.2)
        with lock:
            live -= 1
        return "ok"

    tool = create_delegate_tool(spawn)
    _register_safe("delegate_to_subagent")
    calls = [
        ToolCallRequest(tool_name="delegate_to_subagent", arguments={"task": str(i)}, tool_call_id=str(i))
        for i in range(5)
    ]
    executor = ToolBatchExecutor(tool_map={"delegate_to_subagent": lambda **kw: tool.invoke(kw)})
    result = executor.execute_batch(calls)
    assert all(not r.is_error for r in result.results)
    assert peak <= MAX_DELEGATE_CONCURRENCY
    assert peak >= 2, "应真正并发，而非逐个串行"
    assert MAX_DELEGATE_CONCURRENCY == 10


def test_ask_is_fail_closed_inside_parallel_batch(monkeypatch):
    runtime = PermissionRuntime(session=SessionPermissionState())
    runtime.update_session_rules({"unknown_tool_xyz": "ask"})
    prompted = []
    runtime.set_confirm_callback(lambda request: prompted.append(request) or True)
    with permission_runtime_session(runtime):
        assert enforce_tool_permission("unknown_tool_xyz", {}) is None
        assert prompted, "串行 ask 应弹窗（回调批准即放行）"
        prompted.clear()
        with parallel_batch_session():
            denied = enforce_tool_permission("unknown_tool_xyz", {})
        assert denied is not None and "并行" in denied
        assert not prompted, "并行批内不得弹窗"


def test_allow_still_passes_inside_parallel_batch():
    runtime = PermissionRuntime(session=SessionPermissionState())
    runtime.update_session_rules({"read_file": "allow"})
    with permission_runtime_session(runtime):
        with parallel_batch_session():
            assert enforce_tool_permission("read_file", {"path": "a.txt"}) is None
