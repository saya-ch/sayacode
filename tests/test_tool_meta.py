"""P1: 工具元数据测试."""

import logging

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
        assert meta.destructive_hint is False
        assert meta.confirmation_hint is False
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


class TestConcurrencyPredicate:
    """函数式并发判断及其异常回退路径（此前零覆盖）。

    注意：除 test_failing_predicate_fallback_is_logged 外，本类的测试都是
    **覆盖率测试**，不是本轮改动的回归防护 —— check_concurrency_safe 的回退
    语义在 HEAD 上已存在（with_predicates → with_concurrency_predicate 只是改名）。
    """

    def test_predicate_overrides_static_value(self):
        """覆盖率测试。"""
        meta = ToolMeta.with_concurrency_predicate("t", lambda _: True)
        assert meta.check_concurrency_safe({"command": "ls"}) is True

    def test_missing_input_uses_static_value(self):
        """覆盖率测试：没有输入时不做函数式判断，直接返回静态值。"""
        meta = ToolMeta.with_concurrency_predicate("t", lambda _: True)
        assert meta.check_concurrency_safe() is False
        assert meta.check_concurrency_safe(None) is False

    def test_failing_predicate_falls_back_to_static_value(self):
        """覆盖率测试：谓词抛异常时必须回退到静态值，默认值是 Fail-Closed。"""

        def boom(_):
            raise RuntimeError("predicate broken")

        meta = ToolMeta.with_concurrency_predicate("t", boom)
        assert meta.check_concurrency_safe({"a": 1}) is False

    def test_failing_predicate_fallback_is_logged(self, caplog):
        """回退必须留下**可见**的日志，否则判断逻辑损坏后完全无声。

        本库不配置 logging，未配置 handler 时只有 WARNING 及以上会经
        logging.lastResort 落到 stderr；DEBUG 记录会被直接丢弃。因此这里把
        logger 级别卡在 WARNING：若回退退回 logger.debug，本测试捕获不到记录。
        """
        import lib.core.tool_meta as tool_meta

        def boom(_):
            raise RuntimeError("predicate broken")

        meta = ToolMeta.with_concurrency_predicate("t", boom)
        with caplog.at_level(logging.WARNING, logger=tool_meta.__name__):
            meta.check_concurrency_safe({"a": 1})

        records = [r for r in caplog.records if "并发判断谓词执行失败" in r.getMessage()]
        assert records, "回退必须记录日志（且级别不高于 WARNING，否则生产环境不可见）"
        assert records[0].levelno >= logging.WARNING
        assert records[0].name == tool_meta.__name__

    def test_failing_predicate_keeps_explicit_static_value(self):
        """覆盖率测试：回退目标是元数据自己的静态值，而不是硬编码的 False。"""

        def boom(_):
            raise RuntimeError("predicate broken")

        meta = ToolMeta.with_concurrency_predicate("t", boom, is_concurrency_safe=True)
        assert meta.check_concurrency_safe({"a": 1}) is True


class TestRemovedLegacyApi:
    """F9：第二轮刻意删除了这些 0 引用的旧名；本测试防止它们被悄悄恢复。

    它们与现在的 destructive_hint / confirmation_hint 表达同一件事，若两套名字
    并存，又会有人把描述性字段误读成安全护栏（A10 的历史事故）。
    """

    def test_legacy_gate_names_are_gone(self):
        for name in (
            "is_destructive",
            "requires_confirmation",
            "check_read_only",
            "check_destructive",
            "with_predicates",
        ):
            assert not hasattr(ToolMeta, name), f"已删除的旧 API 不应复活: {name}"

    def test_concurrency_predicate_factory_is_the_only_entry_point(self):
        assert hasattr(ToolMeta, "with_concurrency_predicate")
        assert hasattr(ToolMeta, "check_concurrency_safe")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
