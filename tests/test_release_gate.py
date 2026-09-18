"""发布门禁脚本的回归测试：已移除包管理器的引用检查必须是整词匹配。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]

# 包管理器名拆开书写：本文件也在门禁的扫描范围内。
_MANAGER = "u" + "v"


def _load_gate() -> ModuleType:
    """按路径加载发布门禁脚本（scripts 不是可导入包）。"""
    spec = importlib.util.spec_from_file_location(
        "check_release_under_test", ROOT / "scripts" / "check_release.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_standalone_reference_is_detected():
    gate = _load_gate()
    assert gate.has_removed_package_manager_reference(_MANAGER + " run") is True
    assert gate.has_removed_package_manager_reference(_MANAGER + ".lock") is True
    assert gate.has_removed_package_manager_reference("先 " + _MANAGER) is True


def test_dependency_name_containing_the_token_is_not_flagged():
    gate = _load_gate()
    # 回归：依赖名里恰好含该记号，不应被当成残留引用。
    assert gate.has_removed_package_manager_reference("uvicorn>=0.29") is False
    assert gate.has_removed_package_manager_reference("fluvio") is False


def test_repository_has_no_removed_package_manager_references():
    _load_gate().assert_no_removed_package_manager_references()
