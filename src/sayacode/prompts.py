"""面向用户的提示偏好，服务于智能体。只管表达语气和语言，不管工具权限，权限由运行时判定。"""

from __future__ import annotations

from dataclasses import dataclass

STYLES = {
    "standard": ("标准", "Be clear, direct, and practical."),
    "concise": ("简洁", "Answer briefly while retaining the facts needed to act."),
    "tsundere": ("傲娇", "Use a lightly teasing tone without obscuring the answer."),
    "genki": ("元气", "Use an energetic, friendly tone."),
    "mesugaki": ("雌小鬼", "Use a playful, cheeky tone without insulting the user."),
    "onee-san": ("姐姐", "Use a calm, warm, reassuring tone."),
    "idol": ("偶像", "Use a bright, encouraging tone."),
    "catgirl": ("猫娘", "Use a playful catlike tone sparingly."),
    "mukuchi": ("无口", "Use very few words and a reserved tone."),
}

_STYLE_ALIASES = {name: name for name in STYLES}
_STYLE_ALIASES.update({label: name for name, (label, _) in STYLES.items()})
_STYLE_ALIASES.update({"default": "standard", "brief": "concise", "neko": "catgirl"})
LANGUAGES = {"auto", "zh", "en"}


def normalize_style(value: str | None) -> str:
    """把风格别名收敛到标准名。传入风格字串，返回标准风格名。未知名字会抛错，调用前可先展示可选名单。"""
    name = _STYLE_ALIASES.get(str(value or "standard").strip().lower())
    if name is None:
        raise ValueError(f"Unknown style: {value}")
    return name


def normalize_language(value: str | None) -> str:
    """把语言别名收敛到标准码。传入语言字串，返回自动中文英文三者之一。中文英文的多写法都认，未知会抛错。"""
    language = str(value or "auto").strip().lower()
    if language in {"chinese", "中文", "简体中文"}:
        language = "zh"
    if language in {"english", "英文"}:
        language = "en"
    if language not in LANGUAGES:
        raise ValueError(f"Unknown language: {value}")
    return language


@dataclass(slots=True)
class PromptPreferences:
    """用户表达偏好快照。风格和语言都存原文，用时再规范化。"""

    style: str = "standard"
    language: str = "auto"

    def normalized(self) -> "PromptPreferences":
        """返回规范化后的偏好副本。传入无，返回新对象。原对象不改，未知取值会抛错。"""
        return PromptPreferences(
            style=normalize_style(self.style),
            language=normalize_language(self.language),
        )


def build_system_prompt(
    workspace: str,
    preferences: PromptPreferences | None = None,
    *,
    project_instructions: str = "",
) -> str:
    """只返回表达层指引，工具权限由运行时决定。传入工作区和偏好加项目说明，返回系统提示文本。项目说明为空就不拼那一段。"""
    prefs = (preferences or PromptPreferences()).normalized()
    language = {
        "zh": "Respond in Simplified Chinese unless the user requests another language.",
        "en": "Respond in English unless the user requests another language.",
        "auto": "Use the user's language unless asked otherwise.",
    }[prefs.language]
    parts = [
        "You are SAYACODE, a terminal coding assistant.",
        f"Workspace: {workspace}",
        language,
        STYLES[prefs.style][1],
    ]
    if project_instructions.strip():
        parts.extend(("Project instructions:", project_instructions.strip()))
    return "\n".join(parts)
