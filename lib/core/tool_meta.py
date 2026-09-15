"""
工具属性元数据 — 参考 Claude Code Tool type。

定义每个工具的 Fail-Closed 默认属性：
- 默认不可并发、默认非只读
- 并发判断支持函数式形式（check_concurrency_safe(input) 背后的谓词），按具体输入决定
- 支持 ToolSearch 延迟加载（search_hint / should_defer / always_load）

字段分为两类，勿混淆：

- **被生产代码消费**：name、description、is_concurrency_safe
  （batch_executor 据此决定并发）、can_abort_siblings（batch_executor）、
  tool_group、should_defer / always_load（tools/registry 延迟加载）、
  search_hint（ToolSearch 匹配与展示）。
- **仅声明、当前无任何消费方（惰性字段）**：is_read_only、is_mutation_tool、
  destructive_hint、confirmation_hint、is_enabled、interrupt_behavior、
  max_result_chars。真正的权限判定在 lib/core/permissions.py；把上述字段
  当作护栏读取属于误用（历史上 delete_file 的 confirmation_hint=True 就曾被
  误认为是一道确认门，实际门控来自权限策略的默认 ask）。

其中 destructive_hint / confirmation_hint 唯一的读取方是
tool_search._get_tool_detail()，而该函数目前没有任何调用方；实时 ToolSearch
输出（_search_tools → _format_search_results）不展示这两个字段，它们只对直接
调用该函数的库消费者可见。

历史 API 变更（全仓含 tests 引用数均为 0，已删除；勿据旧文档调用）：
is_destructive / requires_confirmation 两个 flag 改名为 destructive_hint /
confirmation_hint；check_read_only() / check_destructive() 连同只服务于它们的
_read_only_predicate / _destructive_predicate 一并移除（只读与破坏性没有函数式
判断，也没有消费方）；with_predicates() 收敛为只处理并发判断的
with_concurrency_predicate()。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


logger = logging.getLogger(__name__)

# 函数式判断类型：接受工具参数，返回布尔值
InputPredicate = Callable[[Dict[str, Any]], bool]


@dataclass
class ToolMeta:
    """工具属性元数据 — Fail-Closed 默认值。

    并发判断支持两种模式：
    1. 静态布尔值（默认）— 所有输入返回相同结果
    2. 函数式判断 — 按具体输入决定（如按命令内容判断是否可并发）

    当同时设置 bool 和 callable 时，callable 优先。
    """

    name: str
    description: str = ""

    # --- 启用（仅描述性，当前无消费方）---
    is_enabled: bool = True

    # --- 并发安全 ---
    is_concurrency_safe: bool = False     # 静态：默认不可并发
    _concurrency_predicate: Optional[InputPredicate] = field(default=None, repr=False)

    # --- 只读（仅描述性；is_mutation_tool 由它派生）---
    is_read_only: bool = False            # 静态：默认会写

    # --- 破坏性（仅声明：唯一读取方 tool_search._get_tool_detail() 无调用方）---
    destructive_hint: bool = False

    # --- 确认建议（仅声明，同 destructive_hint；不是确认门）---
    confirmation_hint: bool = False

    # --- 中断行为（仅描述性，当前无消费方）---
    interrupt_behavior: str = "cancel"    # "cancel" | "block"

    # --- 分组 ---
    tool_group: str = "other"             # "file"|"shell"|"git"|"project"|"mcp"|"other"

    # --- ToolSearch 支持 ---
    search_hint: str = ""                 # 关键字提示（3-10词，无句号）
    should_defer: bool = False            # 是否延迟加载（defer_loading: true）
    always_load: bool = False             # 是否始终包含在初始 prompt（忽略 should_defer）

    # --- 结果大小限制（仅描述性，当前无消费方）---
    max_result_chars: float = 50_000      # 结果超过此大小时写盘；float("inf") 表示无限制（read_file 即如此注册）

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

        优先使用函数式判断；谓词抛异常时回退到静态布尔值（Fail-Closed：默认不可并发）。
        回退意味着谓词本身已损坏，属于异常路径，因此按 WARNING 级别记录（含 traceback）：
        本库不配置 logging（库内调用 basicConfig/addHandler 会污染宿主应用），未配置
        handler 时只有 WARNING 及以上经 logging.lastResort 输出到 stderr，DEBUG/INFO
        会被静默丢弃。配置了 logging 的库消费者则按自己的 handler 处理该记录。
        """
        if self._concurrency_predicate and input_dict is not None:
            try:
                return self._concurrency_predicate(input_dict)
            except Exception:
                logger.warning(
                    "工具 %s 的并发判断谓词执行失败，回退到静态值 is_concurrency_safe=%s",
                    self.name,
                    self.is_concurrency_safe,
                    exc_info=True,
                )
        return self.is_concurrency_safe

    @classmethod
    def safe_default(cls, name: str, **overrides) -> "ToolMeta":
        """创建 Fail-Closed 默认值的元数据实例。"""
        return cls(name=name, **overrides)

    @classmethod
    def with_concurrency_predicate(
        cls,
        name: str,
        predicate: InputPredicate,
        **overrides,
    ) -> "ToolMeta":
        """创建「按输入判断并发安全性」的元数据实例。

        注意：当前没有任何内置工具使用函数式判断（全部使用静态布尔值），
        因此 check_concurrency_safe 在生产路径上总是返回静态值。
        本工厂供扩展与测试使用；谓词抛异常时回退到静态值（Fail-Closed）。
        """
        return cls(name=name, _concurrency_predicate=predicate, **overrides)


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
