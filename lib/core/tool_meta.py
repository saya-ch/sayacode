"""
工具属性元数据。

定义每个工具的 Fail-Closed 默认属性，参考 Claude Code ToolDef defaults。
所有默认值偏向安全和简单：默认不可并发、默认非只读、默认不需确认。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ToolMeta:
    """工具属性元数据 — Fail-Closed 默认值。

    所有属性默认保守值，工具显式声明后才获得更大权限。
    """
    name: str
    description: str = ""
    is_enabled: bool = True
    is_concurrency_safe: bool = False       # 默认不可并发执行
    is_read_only: bool = False               # 默认会写
    is_destructive: bool = False             # 默认非破坏性
    requires_confirmation: bool = False      # 默认不需额外确认
    interrupt_behavior: str = "cancel"       # "cancel" | "block"
    # 工具分组（"file" | "shell" | "git" | "project" | "mcp" | "other"）
    tool_group: str = "other"

    @property
    def is_mutation_tool(self) -> bool:
        """是否为变更类工具（写、删、执行命令、Git 变更）。"""
        return not self.is_read_only

    @property
    def can_abort_siblings(self) -> bool:
        """工具失败时是否应中止同级工具（Bash/Shell/Git 类）。"""
        return self.tool_group in ("shell", "git")

    @classmethod
    def safe_default(cls, name: str, **overrides) -> "ToolMeta":
        """创建 Fail-Closed 默认值的元数据实例。"""
        return cls(name=name, **overrides)


# ==============================================================================
# 内置工具的元数据注册表
# ==============================================================================

_BUILTIN_TOOL_METAS: dict[str, ToolMeta] = {}


def register_tool_meta(meta: ToolMeta) -> ToolMeta:
    """注册工具元数据。"""
    _BUILTIN_TOOL_METAS[meta.name] = meta
    return meta


def get_tool_meta(name: str) -> ToolMeta | None:
    """获取已注册的工具元数据。"""
    return _BUILTIN_TOOL_METAS.get(name)


def get_all_tool_metas() -> list[ToolMeta]:
    """获取所有已注册的工具元数据。"""
    return list(_BUILTIN_TOOL_METAS.values())


__all__ = [
    "ToolMeta",
    "register_tool_meta",
    "get_tool_meta",
    "get_all_tool_metas",
]
