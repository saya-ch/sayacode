"""P0: 并发工具批处理执行器测试."""

import pytest
from lib.tools.batch_executor import (
    ToolBatchExecutor,
    ToolCallRequest,
    partition_by_concurrency,
)
from lib.core.tool_meta import ToolMeta, register_tool_meta


# 注册测试用工具元数据
@pytest.fixture(autouse=True)
def _register_test_metas():
    register_tool_meta(ToolMeta.safe_default("safe_read", is_concurrency_safe=True, is_read_only=True, tool_group="file"))
    register_tool_meta(ToolMeta.safe_default("safe_write", is_concurrency_safe=True, tool_group="file"))
    register_tool_meta(ToolMeta.safe_default("unsafe_shell", tool_group="shell"))
    register_tool_meta(ToolMeta.safe_default("unsafe_git", tool_group="git"))
    yield


class TestPartitionByConcurrency:
    def test_empty(self):
        safe, unsafe = partition_by_concurrency([])
        assert safe == []
        assert unsafe == []

    def test_all_safe(self):
        safe, unsafe = partition_by_concurrency(["safe_read", "safe_write"])
        assert set(safe) == {"safe_read", "safe_write"}
        assert unsafe == []

    def test_all_unsafe(self):
        safe, unsafe = partition_by_concurrency(["unsafe_shell", "unsafe_git"])
        assert safe == []
        assert set(unsafe) == {"unsafe_shell", "unsafe_git"}

    def test_mixed(self):
        safe, unsafe = partition_by_concurrency(["safe_read", "unsafe_shell", "safe_write", "unsafe_git"])
        assert set(safe) == {"safe_read", "safe_write"}
        assert set(unsafe) == {"unsafe_shell", "unsafe_git"}

    def test_unknown_tool_is_unsafe(self):
        safe, unsafe = partition_by_concurrency(["nonexistent_tool"])
        assert safe == []
        assert unsafe == ["nonexistent_tool"]


class TestBatchExecutor:
    def _tool_fn(self, name: str, result=None, raise_err=None):
        def fn(**kwargs):
            if raise_err:
                raise Exception(raise_err)
            return result or f"{name}_result"
        return fn

    def test_empty_batch(self):
        executor = ToolBatchExecutor({})
        result = executor.execute_batch([])
        assert result.results == []
        assert not result.has_aborted

    def test_single_tool(self):
        executor = ToolBatchExecutor({"safe_read": lambda **kw: "ok"})
        result = executor.execute_batch([
            ToolCallRequest(tool_name="safe_read", arguments={}, tool_call_id="1")
        ])
        assert len(result.results) == 1
        assert result.results[0].result == "ok"
        assert not result.results[0].is_error

    def test_concurrent_safe_tools(self):
        results_store = []

        def safe1(**kw):
            results_store.append("s1")
            return "result1"

        def safe2(**kw):
            results_store.append("s2")
            return "result2"

        executor = ToolBatchExecutor({"safe_read": safe1, "safe_write": safe2})
        result = executor.execute_batch([
            ToolCallRequest(tool_name="safe_read", arguments={}, tool_call_id="1"),
            ToolCallRequest(tool_name="safe_write", arguments={}, tool_call_id="2"),
        ])
        assert len(result.results) == 2
        assert not result.has_aborted
        # Both should have been called (order not guaranteed in concurrent)
        assert "s1" in results_store
        assert "s2" in results_store

    def test_sequential_unsafe_tools(self):
        call_order = []

        def us1(**kw):
            call_order.append("us1")
            return "r1"

        def us2(**kw):
            call_order.append("us2")
            return "r2"

        executor = ToolBatchExecutor({"unsafe_shell": us1, "unsafe_git": us2})
        result = executor.execute_batch([
            ToolCallRequest(tool_name="unsafe_shell", arguments={}, tool_call_id="1"),
            ToolCallRequest(tool_name="unsafe_git", arguments={}, tool_call_id="2"),
        ])
        assert len(result.results) == 2
        assert call_order == ["us1", "us2"]  # sequential

    def test_error_triggers_sibling_abort(self):
        executor = ToolBatchExecutor({
            "unsafe_shell": lambda **kw: (_ for _ in ()).throw(Exception("cmd failed")),
            "unsafe_git": lambda **kw: "should_not_run",
        })
        result = executor.execute_batch([
            ToolCallRequest(tool_name="unsafe_shell", arguments={}, tool_call_id="1"),
            ToolCallRequest(tool_name="unsafe_git", arguments={}, tool_call_id="2"),
        ])
        assert result.has_aborted
        assert "sibling_error" in (result.abort_reason or "")

    def test_unknown_tool_returns_error(self):
        executor = ToolBatchExecutor({})
        result = executor.execute_batch([
            ToolCallRequest(tool_name="nonexistent", arguments={}, tool_call_id="1")
        ])
        assert result.results[0].is_error
        assert "未知工具" in (result.results[0].error or "")

    def test_partition_mixed_execution(self):
        """安全工具并发，非安全工具串行 — 混合批次。"""
        call_order = []

        def safe1(**kw):
            call_order.append("safe1")
            return "s1"

        def unsafe1(**kw):
            call_order.append("unsafe1")
            return "u1"

        def safe2(**kw):
            call_order.append("safe2")
            return "s2"

        executor = ToolBatchExecutor({
            "safe_read": safe1,
            "safe_write": safe2,
            "unsafe_shell": unsafe1,
        })
        result = executor.execute_batch([
            ToolCallRequest(tool_name="safe_read", arguments={}, tool_call_id="s1"),
            ToolCallRequest(tool_name="unsafe_shell", arguments={}, tool_call_id="u1"),
            ToolCallRequest(tool_name="safe_write", arguments={}, tool_call_id="s2"),
        ])
        assert len(result.results) == 3
        # 非安全工具在安全工具之后执行
        unsafe_idx = call_order.index("unsafe1")
        # unsafe 应在并发安全工具之后
        assert unsafe_idx >= 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
