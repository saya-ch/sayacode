"""Small, user-facing prompt preferences for the LangChain agent."""

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
MODES = {"build", "plan", "review"}


def normalize_style(value: str | None) -> str:
    name = _STYLE_ALIASES.get(str(value or "standard").strip().lower())
    if name is None:
        raise ValueError(f"Unknown style: {value}")
    return name


def normalize_language(value: str | None) -> str:
    language = str(value or "auto").strip().lower()
    if language in {"chinese", "中文", "简体中文"}:
        language = "zh"
    if language in {"english", "英文"}:
        language = "en"
    if language not in LANGUAGES:
        raise ValueError(f"Unknown language: {value}")
    return language


def normalize_mode(value: str | None) -> str:
    mode = str(value or "build").strip().lower()
    if mode not in MODES:
        raise ValueError(f"Unknown mode: {value}")
    return mode


@dataclass(slots=True)
class PromptPreferences:
    style: str = "standard"
    language: str = "auto"
    mode: str = "build"

    def normalized(self) -> "PromptPreferences":
        return PromptPreferences(
            style=normalize_style(self.style),
            language=normalize_language(self.language),
            mode=normalize_mode(self.mode),
        )


def build_system_prompt(
    workspace: str,
    preferences: PromptPreferences | None = None,
    *,
    project_instructions: str = "",
) -> str:
    """Return presentation guidance; tool permissions live in the runtime."""
    prefs = (preferences or PromptPreferences()).normalized()
    language = {
        "zh": "Respond in Simplified Chinese unless the user requests another language.",
        "en": "Respond in English unless the user requests another language.",
        "auto": "Use the user's language unless asked otherwise.",
    }[prefs.language]
    mode = {
        "build": "You may implement requested changes using available tools.",
        "plan": "Investigate and propose a plan without changing the workspace.",
        "review": "Review the workspace and report actionable findings with evidence.",
    }[prefs.mode]
    parts = [
        "You are SAYACODE, a terminal coding assistant.",
        f"Workspace: {workspace}",
        language,
        STYLES[prefs.style][1],
        mode,
    ]
    if project_instructions.strip():
        parts.extend(("Project instructions:", project_instructions.strip()))
    return "\n".join(parts)
