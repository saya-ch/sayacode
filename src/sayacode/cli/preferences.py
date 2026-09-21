"""本地终端偏好和程序版本。"""

from __future__ import annotations

import json
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from ..prompts import PromptPreferences, normalize_language, normalize_style


def _package_version() -> str:
    """取已安装包版本，取不到回落默认版本。
    无参数，返回版本串。
    未安装场景靠回落保证头图可显示。"""
    try:
        return version("sayacode")
    except PackageNotFoundError:
        return "2.0.0"


def _state_home() -> Path:
    """定位本地状态目录，支持环境变量改址。
    无参数，返回目录路径。
    缺省落在用户家目录下，调用方自行建目录。"""
    return Path(os.environ.get("SAYACODE_HOME") or Path.home() / ".sayacode").expanduser()


def load_preferences() -> PromptPreferences:
    """读本地偏好文件，坏文件回落默认值。
    无参数，返回偏好对象。
    只取语言与风格两项，其余内容原样保留。"""
    try:
        document = json.loads((_state_home() / "config.json").read_text(encoding="utf-8"))
        data = document.get("preferences", {}) if isinstance(document, dict) else {}
        if not isinstance(data, dict):
            return PromptPreferences()
        return PromptPreferences(
            style=normalize_style(data.get("style")),
            language=normalize_language(data.get("language")),
        )
    except (OSError, ValueError):
        return PromptPreferences()


def save_preferences(preferences: PromptPreferences) -> None:
    """保存语言与风格偏好，保留文件其余字段。
    参数是偏好对象，返回无。
    先写临时文件再原子替换，坏文件按空文档处理。"""
    home = _state_home()
    home.mkdir(parents=True, exist_ok=True)
    target = home / "config.json"
    try:
        document = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
    except (OSError, ValueError):
        document = {}
    if not isinstance(document, dict):
        document = {}
    document["preferences"] = {
        "style": preferences.style,
        "language": preferences.language,
    }
    temporary = home / "config.json.tmp"
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
