"""CLI 最小运行时多语言支持。

职责是按语言偏好取文案：TRANSLATIONS 存全部中英文案，tr 按
当前语言选文案并填充参数。核心函数为 tr、set_language、
normalize_language，由展示层调用。
"""

from __future__ import annotations

import locale
import os
from typing import Any

from .i18n_en import EN_STRINGS
from .i18n_zh import ZH_STRINGS

SUPPORTED_LANGUAGES = ("auto", "zh-CN", "en")

_CURRENT_LANGUAGE = "auto"


TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": EN_STRINGS,
    "zh-CN": ZH_STRINGS,
}


def normalize_language(value: Any) -> str:
    """归一化语言标识到支持的取值。"""
    if value is None:
        return "auto"

    text = str(value).strip()
    if not text:
        return "auto"

    normalized = text.lower().replace("_", "-")
    aliases = {
        "auto": "auto",
        "system": "auto",
        "default": "auto",
        "zh": "zh-CN",
        "zh-cn": "zh-CN",
        "cn": "zh-CN",
        "chinese": "zh-CN",
        "english": "en",
        "en": "en",
        "en-us": "en",
    }
    return aliases.get(normalized, text if text in SUPPORTED_LANGUAGES else "auto")


def detect_system_language() -> str:
    """探测系统语言并映射到支持的取值。"""
    candidates = [
        os.environ.get("LC_ALL"),
        os.environ.get("LANG"),
        locale.getlocale()[0],
    ]
    for candidate in candidates:
        if not candidate:
            continue
        lowered = str(candidate).lower()
        if lowered.startswith("zh"):
            return "zh-CN"
        if lowered.startswith("en"):
            return "en"
    return "en"


def set_language(value: Any) -> str:
    """设置当前 CLI 语言偏好。"""
    global _CURRENT_LANGUAGE
    _CURRENT_LANGUAGE = normalize_language(value)
    return _CURRENT_LANGUAGE


def get_language_preference() -> str:
    """返回当前语言偏好设置。"""
    return _CURRENT_LANGUAGE


def get_effective_language() -> str:
    """返回实际生效的语言。"""
    if _CURRENT_LANGUAGE == "auto":
        return detect_system_language()
    return _CURRENT_LANGUAGE


def tr(key: str, **kwargs: Any) -> str:
    """按当前语言取文案并填充参数。"""
    language = get_effective_language()
    template = (
        TRANSLATIONS.get(language, {}).get(key)
        or TRANSLATIONS["en"].get(key)
        or key
    )
    return template.format(**kwargs)


def on_off(value: bool) -> str:
    """返回开关量的本地化文案。"""
    return tr("common.on" if value else "common.off")


def language_label(code: Any) -> str:
    """返回语言代码的展示名。"""
    normalized = normalize_language(code)
    return tr(f"lang.{normalized}")
