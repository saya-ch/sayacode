"""P1: 工具元数据测试."""

import pytest
from lib.core.tool_meta import (
    ToolMeta, register_tool_meta, get_tool_meta, get_all_tool_metas,
)


class TestToolMeta:
    def test_fail_closed_defaults(self):
        """所有默认值应该是 fail-closed（保守、安全）。"""
        meta = ToolMeta(name="test_tool")
        assert meta.is_enabled is True
        assert meta.is_concurrency_safe is False   # 默认不可并发
        assert meta.is_read_only is False           # 默认会写
        assert meta.is_destructive is False
        assert meta.requires_confirmation is False
        assert meta.interrupt_behavior == "cancel"

    def test_is_mutation_tool(self):
        rw = ToolMeta(name="rw_tool", is_read_only=False)
        assert rw.is_mutation_tool
        ro = ToolMeta(name="ro_tool", is_read_only=True)
        assert not ro.is_mutation_tool

    def test_can_abort_siblings(self):
        shell = ToolMeta(name="shell", tool_group="shell")
        assert shell.can_abort_siblings
        git = ToolMeta(name="git", tool_group="git")
        assert git.can_abort_siblings
        file_op = ToolMeta(name="read", tool_group="file")
        assert not file_op.can_abort_siblings
        other = ToolMeta(name="other", tool_group="other")
        assert not other.can_abort_siblings

    def test_safe_default_factory(self):
        meta = ToolMeta.safe_default("my_tool", is_read_only=True, tool_group="file")
        assert meta.name == "my_tool"
        assert meta.is_read_only is True
        assert meta.tool_group == "file"
        # 未指定的保持默认
        assert meta.is_concurrency_safe is False

    def test_override_defaults(self):
        meta = ToolMeta(name="concurrent_reader", is_concurrency_safe=True, is_read_only=True)
        assert meta.is_concurrency_safe is True
        assert meta.is_read_only is True


class TestToolMetaRegistry:
    def setup_method(self):
        # 每个测试独立，不依赖全局注册表
        self.meta = ToolMeta(name="test_registry_tool", tool_group="file")

    def test_register_and_get(self):
        register_tool_meta(self.meta)
        retrieved = get_tool_meta("test_registry_tool")
        assert retrieved is not None
        assert retrieved.name == "test_registry_tool"
        assert retrieved.tool_group == "file"

    def test_get_nonexistent(self):
        assert get_tool_meta("nonexistent_tool") is None

    def test_get_all(self):
        register_tool_meta(ToolMeta(name="tool_a"))
        register_tool_meta(ToolMeta(name="tool_b"))
        all_metas = get_all_tool_metas()
        assert len(all_metas) >= 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
