"""
工具属性元数据 — 参考 Claude Code Tool type.

定义每个工具的 Fail-Closed 默认属性：
- 默认不可并发、默认非只读、默认不需确认
- 支持函数式判断（is_concurrency_safe(input)、is_read_only(input)）
- 支持 ToolSearch 延迟加载（search_hint / should_defer / always_load）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Union


# 函数式判断类型：接受工具参数，返回布尔值
InputPredicate = Callable[[Dict[str, Any]], bool]


@dataclass
class ToolMeta:
    """工具属性元数据 — Fail-Closed 默认值。

    支持两种判断模式：
    1. 静态布尔值（默认）— 所有输入返回相同结果
    2. 函数式判断 — 按具体输入决定（如 read_file("/etc/passwd") 可能有不同权限）

    当同时设置 bool 和 callable 时，callable 优先。
    """

    name: str
    description: str = ""

    # --- 启用 ---
    is_enabled: bool = True               # 工具是否启用

    # --- 并发安全 ---
    is_concurrency_safe: bool = False     # 静态：默认不可并发
    _concurrency_predicate: Optional[InputPredicate] = field(default=None, repr=False)

    # --- 只读 ---
    is_read_only: bool = False            # 静态：默认会写
    _read_only_predicate: Optional[InputPredicate] = field(default=None, repr=False)

    # --- 破坏性 ---
    is_destructive: bool = False          # 静态：默认非破坏性
    _destructive_predicate: Optional[InputPredicate] = field(default=None, repr=False)

    # --- 确认 ---
    requires_confirmation: bool = False   # 是否需要额外确认

    # --- 中断行为 ---
    interrupt_behavior: str = "cancel"    # "cancel" | "block"

    # --- 分组 ---
    tool_group: str = "other"             # "file"|"shell"|"git"|"project"|"mcp"|"other"

    # --- ToolSearch 支持 ---
    search_hint: str = ""                 # 关键字提示（3-10词，无句号）
    should_defer: bool = False            # 是否延迟加载（defer_loading: true）
    always_load: bool = False             # 是否始终包含在初始 prompt（忽略 should_defer）

    # --- 结果大小限制 ---
    max_result_chars: int = 50_000        # 结果超过此大小时写盘（0 表示无限制）

    @property
    def is_mutation_tool(self) -> bool:
        """是否为变更类工具（写、删、执行命令、Git 变更）。"""
        return not self.is_read_only

    @property
    def can_abort_siblings(self) -> bool:
        """工具失败时是否应中止同级工具（Bash/Shell/Git 类）。"""
        return self.tool_group in ("shell", "git")

    # --- 按输入判断的方法 ---

    def check_concurrency_safe(self, input_dict: Optional[Dict[str, Any]] = None) -> bool:
        """判断给定输入下工具是否可并发执行。
        优先使用函数式判断，回退到静态布尔值。
        """
        if self._concurrency_predicate and input_dict is not None:
            try:
                return self._concurrency_predicate(input_dict)
            except Exception:
                pass
        return self.is_concurrency_safe

    def check_read_only(self, input_dict: Optional[Dict[str, Any]] = None) -> bool:
        """判断给定输入下工具是否只读。
        优先使用函数式判断，回退到静态布尔值。
        """
        if self._read_only_predicate and input_dict is not None:
            try:
                return self._read_only_predicate(input_dict)
            except Exception:
                pass
        return self.is_read_only

    def check_destructive(self, input_dict: Optional[Dict[str, Any]] = None) -> bool:
        """判断给定输入下工具是否具有破坏性。"""
        if self._destructive_predicate and input_dict is not None:
            try:
                return self._destructive_predicate(input_dict)
            except Exception:
                pass
        return self.is_destructive

    @classmethod
    def safe_default(cls, name: str, **overrides) -> "ToolMeta":
        """创建 Fail-Closed 默认值的元数据实例。"""
        return cls(name=name, **overrides)

    @classmethod
    def with_predicates(
        cls,
        name: str,
        concurrency_predicate: Optional[InputPredicate] = None,
        read_only_predicate: Optional[InputPredicate] = None,
        destructive_predicate: Optional[InputPredicate] = None,
        **kwargs,
    ) -> "ToolMeta":
        """创建带函数式判断的元数据实例。"""
        return cls(
            name=name,
            _concurrency_predicate=concurrency_predicate,
            _read_only_predicate=read_only_predicate,
            _destructive_predicate=destructive_predicate,
            **kwargs,
        )


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


def get_deferred_tool_metas() -> list[ToolMeta]:
    """获取所有应延迟加载的工具元数据（should_defer=True 且 always_load=False）。"""
    return [
        m for m in _BUILTIN_TOOL_METAS.values()
        if m.should_defer and not m.always_load
    ]


def get_searchable_tool_metas() -> list[ToolMeta]:
    """获取所有可通过 ToolSearch 搜索的工具元数据。"""
    return [
        m for m in _BUILTIN_TOOL_METAS.values()
        if m.search_hint or m.should_defer
    ]


__all__ = [
    "ToolMeta",
    "InputPredicate",
    "register_tool_meta",
    "get_tool_meta",
    "get_all_tool_metas",
    "get_deferred_tool_metas",
    "get_searchable_tool_metas",
]
