"""本地终端偏好和程序版本。"""

from __future__ import annotations

import json
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from ..prompts import PromptPreferences, normalize_language, normalize_style


def _package_version() -> str:
    try:
        return version("sayacode")
    except PackageNotFoundError:
        return "2.0.0"


def _state_home() -> Path:
    return Path(os.environ.get("SAYACODE_HOME") or Path.home() / ".sayacode").expanduser()


def load_preferences() -> PromptPreferences:
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
