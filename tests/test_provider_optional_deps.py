"""provider 目录与可选依赖的一致性。

对应 CODE_REVIEW_FINDINGS.md 的已确认问题：
- ``provider_catalog_entry()`` 对未知 provider 静默回退 ollama（拼错名字不报错）
- ``langchain_ollama`` 顶层硬导入，缺少该包时整个 lib 包无法导入

重写后可选厂商集成统一在 ``lib.models.providers`` 里由 ``try/except`` 保护，
因此本文件同时守住「缺包时 import lib 仍然可用」这条不变量。
"""

import ast
from pathlib import Path

import pytest

from lib.models.provider_catalog import (
    PROVIDER_CATALOG,
    provider_catalog_entry,
    provider_defaults,
)


def test_known_provider_returns_catalog_entry():
    for key, entry in PROVIDER_CATALOG.items():
        assert provider_catalog_entry(key) is entry


def test_azure_alias_is_normalized():
    assert provider_catalog_entry("azure").value == "azure_openai"


def test_unknown_provider_raises_instead_of_silent_fallback():
    """拼错 provider 名必须报错，而不是静默跑在本地 ollama 上。"""
    with pytest.raises(ValueError) as excinfo:
        provider_catalog_entry("ollamma")

    message = str(excinfo.value)
    assert "ollamma" in message
    assert "ollama" in message  # 错误信息应列出受支持的 provider


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_empty_provider_still_falls_back_to_ollama(empty):
    """空值表示「未设置」，仍回退到 ollama 默认项。"""
    assert provider_catalog_entry(empty).value == "ollama"


def test_provider_defaults_propagates_unknown_error():
    with pytest.raises(ValueError):
        provider_defaults("not-a-provider")


def test_runtime_model_profiles_normalizes_empty_to_ollama():
    """runtime 层显式把空值归一为 ollama，因此不应触发 ValueError。"""
    from lib.runtime.model_profiles import provider_defaults as runtime_provider_defaults

    assert runtime_provider_defaults(None)["value"] == "ollama"
    assert runtime_provider_defaults("")["value"] == "ollama"


# ── 可选依赖保护 ─────────────────────────────────────────────────────────────

# 厂商集成包 → 该包缺失时被置为 None 的协议类名。
# 这一对一是必须的：只看到「某个 try 里 import 了某个模块」不足以证明保护有效，
# 还要证明**缺包时确实有一个可用的降级值**（否则类名根本不存在，注册表会 AttributeError，
# 甚至 import lib 直接失败）。
_VENDOR_PACKAGES = {
    "langchain_anthropic": "AnthropicModel",
    "langchain_ollama": "OllamaModel",
    "langchain_deepseek": "DeepSeekModel",
    "langchain_google_genai": "GeminiModel",
}


def _names_in(node: ast.AST) -> set[str]:
    """收集一个 ``except`` 子句能捕获的异常名（``except (A, B):`` 也算）。"""
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            found.add(child.id)
        elif isinstance(child, ast.Attribute):
            found.add(child.attr)
    return found


def _guarded_vendor_imports(tree: ast.Module) -> dict[str, tuple[set[str], set[str]]]:
    """返回 ``{模块名: (捕获的异常名, 被置 None 的目标名)}``。

    只认**模块顶层**的 ``try``：藏在函数体或其它语句里的 try 在导入期不会执行，
    因此不构成保护。
    """
    guarded: dict[str, tuple[set[str], set[str]]] = {}

    for node in tree.body:
        if not isinstance(node, ast.Try):
            continue

        imported = {
            child.module
            for child in ast.walk(node)
            if isinstance(child, ast.ImportFrom) and child.module
        }
        caught: set[str] = set()
        fallbacks: set[str] = set()
        for handler in node.handlers:
            if handler.type is not None:
                caught |= _names_in(handler.type)
            for statement in handler.body:
                if isinstance(statement, ast.Assign):
                    for target in statement.targets:
                        if isinstance(target, ast.Name):
                            fallbacks.add(target.id)

        for module in imported:
            guarded[module] = (caught, fallbacks)

    return guarded


def test_optional_vendor_imports_are_guarded():
    """可选厂商集成必须在**模块顶层**的 try/except 里导入，并给出降级值。

    这是 A13 的回归保护：旧实现把 ``langchain_ollama`` 放在模块顶层硬导入，
    干净环境下实测会连带 28 个 collection error。

    本测试刻意检查保护的**形状**而不仅仅是「出现过 import」：早期版本只断言
    「模块名出现在文件里某个 try 内」，那样即使 guard 写错（不在顶层、不捕获
    ImportError、不设置降级值）也会通过。
    """
    import lib.models.providers as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    guarded = _guarded_vendor_imports(tree)

    for vendor, class_name in _VENDOR_PACKAGES.items():
        assert vendor in guarded, f"{vendor} 未在模块顶层的 try/except 内导入"

        caught, fallbacks = guarded[vendor]
        assert "ImportError" in caught, (
            f"{vendor} 的保护没有捕获 ImportError（实际捕获：{sorted(caught) or '无'}）"
        )
        assert class_name in fallbacks, (
            f"{vendor} 缺包时未把 {class_name} 置为降级值（实际：{sorted(fallbacks) or '无'}）"
        )


def test_guard_table_covers_every_optional_protocol_class():
    """元测试：新增可选协议类时必须同时登记，否则上一测试会漏掉它。"""
    import lib.models.providers as module

    registered = set(_VENDOR_PACKAGES.values())
    optional = {
        name
        for name in ("AnthropicModel", "OllamaModel", "DeepSeekModel", "GeminiModel")
        if hasattr(module, name)
    }

    assert optional == registered


def test_availability_probes_match_installed_packages():
    from importlib.util import find_spec

    from lib.models import is_anthropic_available, is_ollama_available

    for probe, package in (
        (is_anthropic_available, "langchain_anthropic"),
        (is_ollama_available, "langchain_ollama"),
    ):
        assert probe() == (find_spec(package) is not None)


def test_missing_optional_dependency_raises_import_error_naming_the_package():
    """依赖缺失时取模型类必须抛 ImportError，并指明该装哪个包。"""
    from lib.models.registry import ModelProviderRegistry, ModelProviderSpec

    registry = ModelProviderRegistry([ModelProviderSpec(
        key="ollama",
        protocol="ollama",
        model_class=None,
        display_name="Ollama",
        requires_package="langchain-ollama",
    )])

    with pytest.raises(ImportError) as excinfo:
        registry.get_model_class("ollama")
    assert "langchain-ollama" in str(excinfo.value)

    # 依赖缺失的 provider 不进入可选列表
    assert "ollama" not in registry.list_types()


def test_models_package_exposes_optional_provider_flags():
    """models 包必须能导入，并暴露可选依赖探测函数与全部协议类。"""
    import lib.models as models

    for name in (
        "OllamaModel", "AnthropicModel", "GeminiModel", "DeepSeekModel",
        "is_ollama_available", "is_anthropic_available",
    ):
        assert hasattr(models, name), f"lib.models 缺少导出: {name}"


def test_token_usage_is_exported_from_models_package():
    """``session_usage`` 返回该类型，因此调用方必须能直接命名它。

    这是重写附带的**纯新增**：旧实现只把 ``TokenUsage`` 留在 ``lib.models.base``，
    没放进包导出面。
    """
    import lib.models as models

    assert hasattr(models, "TokenUsage")
    assert "TokenUsage" in models.__all__
