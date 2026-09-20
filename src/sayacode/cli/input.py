"""提示工具包的异步输入边界。"""

from __future__ import annotations

import sys
from typing import Any


async def _terminal_prompt(session: Any, label: str, **kwargs: Any) -> str:
    """任务打印通知时保持输入行完整。"""
    if sys.stdout.isatty():
        from prompt_toolkit.patch_stdout import patch_stdout

        with patch_stdout():
            return str(await session.prompt_async(label, **kwargs))
    return str(await session.prompt_async(label, **kwargs))
