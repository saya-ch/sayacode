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
#
# 形状（2026-09：惰性化之后）：厂商 SDK 只允许出现在 ``_build_*`` builder 函数
# 体内的 ``try/except ImportError`` 里，缺包时 builder 返回 None；模块顶层不许
# 出现任何厂商 import（否则 ``import lib.models`` 又拖回全部 SDK）。
# 保护不变量没变，只是时机从 import 期推迟到首次解析：
# 缺包时 ``resolve_protocol_class()`` 给 None，注册表照样把该 provider 排除。

# 厂商集成包 → builder 名 → 缺包时被置为 None 的协议类名。
# 三列缺一不可：只看到 try 不足以证明保护有效，还要证明缺包时确实有一个
# 可用的降级值（否则类名根本不存在，注册表会 AttributeError）。
_VENDOR_BUILDERS = {
    "langchain_anthropic": ("_build_anthropic_model", "AnthropicModel"),
    "langchain_ollama": ("_build_ollama_model", "OllamaModel"),
    "langchain_deepseek": ("_build_deepseek_model", "DeepSeekModel"),
    "langchain_google_genai": ("_build_gemini_model", "GeminiModel"),
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


def _builder_guards(tree: ast.Module) -> dict[str, tuple[set[str], bool]]:
    """返回 ``{模块名: (捕获的异常名, 缺包时是否 return None)}``。

    只认**模块顶层 ``_build_*`` 函数体内的 try**：与旧版"只认模块顶层 try"是
    同一条标准（"藏起来的 try 不算保护"），位置从模块顶层挪到了 builder 函数体——
    因为 import 期根本不该碰厂商包，保护的时机也必须从 import 期挪到解析期。
    """
    guarded: dict[str, tuple[set[str], bool]] = {}

    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("_build_"):
            continue

        imported: set[str] = set()
        caught: set[str] = set()
        returns_none = False
        for child in ast.walk(node):
            if isinstance(child, ast.ImportFrom) and child.module:
                imported.add(child.module)
            elif isinstance(child, ast.Import):
                imported.update(alias.name for alias in child.names)
            if isinstance(child, ast.ExceptHandler):
                if child.type is not None:
                    caught |= _names_in(child.type)
                for statement in child.body:
                    if isinstance(statement, ast.Return) and isinstance(
                        statement.value, ast.Constant
                    ):
                        if statement.value.value is None:
                            returns_none = True

        for module in imported:
            guarded[module] = (caught, returns_none)

    return guarded


def _top_level_vendor_imports(tree: ast.Module) -> set[str]:
    """模块顶层的硬 import（import 期就会执行）——厂商包不许出现在这里。"""
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
    return found


def test_optional_vendor_imports_are_guarded():
    """可选厂商集成必须在 builder 的 try/except 里导入，缺包返回 None；
    模块顶层不许出现厂商硬导入。

    这是 A13 的回归保护（旧实现把 ``langchain_ollama`` 放在模块顶层硬导入，
    干净环境下实测会连带 28 个 collection error），形状随惰性化 evolution：
    保护从 import 期挪到解析期，但"缺包 → 可用降级值"的不变量没变。

    本测试刻意检查保护的**形状**而不仅仅是「出现过 import」。
    """
    import lib.models.providers as module

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    guarded = _builder_guards(tree)

    for vendor, (builder, class_name) in _VENDOR_BUILDERS.items():
        assert vendor in guarded, f"{vendor} 未在 builder 的 try/except 内导入"
        caught, returns_none = guarded[vendor]
        assert "ImportError" in caught, (
            f"{vendor} 的保护没有捕获 ImportError（实际捕获：{sorted(caught) or '无'}）"
        )
        assert returns_none, f"{vendor} 缺包时未返回 None 降级值"

    top_level = _top_level_vendor_imports(tree)
    for vendor in _VENDOR_BUILDERS:
        assert vendor not in top_level, (
            f"{vendor} 出现在模块顶层 import——import 期又拖回厂商 SDK"
        )
    assert "langchain_openai" not in top_level, (
        "langchain_openai（最慢的那个）出现在模块顶层 import"
    )


def test_guard_table_covers_every_optional_protocol_class():
    """元测试：新增可选协议类时必须同时登记，否则上一测试会漏掉它。"""
    import lib.models.providers as module

    registered = {class_name for _, class_name in _VENDOR_BUILDERS.values()}
    optional = {
        name
        for name in ("AnthropicModel", "OllamaModel", "DeepSeekModel", "GeminiModel")
        if hasattr(module, name)
    }

    assert optional == registered


def test_builders_are_registered_for_every_protocol():
    """元测试：PROTOCOL_SPECS 每加一个协议，builder 表必须同步，否则静默缺席。"""
    from lib.models import providers as module

    assert set(module._PROTOCOL_BUILDERS) == set(module.PROTOCOL_SPECS), (
        "builder 表与协议表不一致：新增协议必须配 builder"
    )


def test_availability_probes_match_installed_packages():
    from importlib.util import find_spec

    from lib.models import is_anthropic_available, is_ollama_available

    for probe, package in (
        (is_anthropic_available, "langchain_anthropic"),
        (is_ollama_available, "langchain_ollama"),
    ):
        assert probe() == (find_spec(package) is not None)


def test_missing_optional_dependency_raises_import_error_naming_the_package(monkeypatch):
    """依赖缺失时取模型类必须抛 ImportError，并指明该装哪个包。

    惰性化之后"缺失"不再是构造参数，而是解析结果：用 monkeypatch 把解析掐断，
    模拟 SDK 不存在（真删包测不了，CI 环境包是全的）。
    """
    from lib.models import providers as providers_module
    from lib.models import registry as registry_module
    from lib.models.registry import ModelProviderRegistry, ModelProviderSpec

    # 模拟 SDK 不存在：解析掐断 + 包探测掐断（list_types 只做包探测，不 import，
    # 所以两处都要模拟；真删包测不了，CI 环境包是全的）。
    monkeypatch.setattr(
        providers_module, "resolve_protocol_class", lambda protocol: None
    )
    monkeypatch.setattr(registry_module, "_package_available", lambda package: False)

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

    # 依赖缺失的 provider 不进入可选列表（list_types 只做包探测，不 import）。
    assert "ollama" not in registry.list_types()


def test_models_package_exposes_optional_provider_flags():
    """models 包必须能导入，并暴露可选依赖探测函数与全部协议类。"""
    import lib.models as models

    for name in (
        "OllamaModel", "AnthropicModel", "GeminiModel", "DeepSeekModel",
        "is_ollama_available", "is_anthropic_available",
    ):
        assert hasattr(models, name), f"lib.models 缺少导出: {name}"


def test_importing_providers_drags_no_vendor_sdk():
    """惰性化的核心不变量：import 期零厂商 SDK（子进程实证，不受已导入污染）。

    回退方式：providers.py 顶层出现厂商 import，或 registry import 期解析。
    """
    import subprocess
    import sys

    probe = (
        "import sys, lib.models.providers, lib.models.registry; "
        "mods = [m for m in sys.modules if m.split('.')[0] in "
        "{'langchain_openai', 'langchain_anthropic', 'langchain_ollama', "
        "'langchain_deepseek', 'langchain_google_genai'}]; "
        "print('VENDOR:' + ','.join(sorted(mods)))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parent.parent),
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "VENDOR:" in proc.stdout
    assert proc.stdout.split("VENDOR:")[1].strip() == "", (
        f"import 期拖入厂商模块：{proc.stdout.strip()}"
    )


def test_resolve_caches_protocol_class():
    """同一协议解析两次是同一对象——注册表、包导出、调用方拿到的必须是同一个类，
    否则 isinstance 两岸分裂（曾经因测试清缓存真实发生过一次）。"""
    from lib.models import providers as module

    assert module.resolve_protocol_class("openai") is module.resolve_protocol_class("openai")


def test_lazy_protocol_map_matches_dict_contract():
    """PROTOCOL_CLASSES 惰性外壳与旧 dict 同契约（registry 与旧代码照常用）。"""
    from lib.models.providers import PROTOCOL_CLASSES

    assert PROTOCOL_CLASSES.get("openai") is not None
    assert PROTOCOL_CLASSES.get("no-such-protocol") is None
    assert PROTOCOL_CLASSES["openai"] is not None
    with pytest.raises(KeyError):
        PROTOCOL_CLASSES["no-such-protocol"]
    assert "openai" in PROTOCOL_CLASSES
    assert "no-such-protocol" not in PROTOCOL_CLASSES
    assert len(PROTOCOL_CLASSES) == 6
    assert set(iter(PROTOCOL_CLASSES)) == {
        "openai", "azure_openai", "deepseek", "anthropic", "ollama", "gemini",
    }


def test_token_usage_is_exported_from_models_package():
    """``session_usage`` 返回该类型，因此调用方必须能直接命名它。

    这是重写附带的**纯新增**：旧实现只把 ``TokenUsage`` 留在 ``lib.models.base``，
    没放进包导出面。
    """
    import lib.models as models

    assert hasattr(models, "TokenUsage")
    assert "TokenUsage" in models.__all__
