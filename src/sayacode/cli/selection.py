"""在交互终端使用方向键选择，在管道输入下保留纯文本流程。"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from typing import TypeVar

from .input import _terminal_prompt
from .theme import Palette

T = TypeVar("T")


def _is_interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


async def choose_option(
    prompt_session: object,
    *,
    label: str,
    fallback_label: str,
    options: Sequence[tuple[T, str]],
    invalid_message: str,
    notice: Callable[[str], None],
    language: str,
) -> T:
    """用 prompt_toolkit 原生单选菜单；重定向输入可输编号或选项名称。"""
    if not options:
        raise ValueError("Selection requires at least one option")
    if _is_interactive_terminal():
        from prompt_toolkit.shortcuts.choice_input import ChoiceInput
        from prompt_toolkit.styles import Style

        hint = (
            "↑↓ 选择 · Enter 确认 · Ctrl+C 取消"
            if language == "zh"
            else ("Up/Down select · Enter confirm · Ctrl+C cancel")
        )
        return await ChoiceInput[T](
            message=label,
            options=options,
            symbol="›",
            bottom_toolbar=hint,
            style=Style.from_dict(
                {
                    "selected-option": f"bold {Palette.prompt}",
                    "bottom-toolbar": f"bg:{Palette.toolbar_bg} {Palette.toolbar_fg}",
                    "bottom-toolbar.text": f"bg:{Palette.toolbar_bg} {Palette.toolbar_fg}",
                }
            ),
        ).prompt_async()

    # 测试替身、管道和简易终端都使用无 ANSI 的输入。
    while True:
        answer = (await _terminal_prompt(prompt_session, fallback_label)).strip()
        if answer.isdecimal() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        for value, description in options:
            if answer.casefold() in {str(value).casefold(), description.casefold()}:
                return value
        notice(invalid_message)
