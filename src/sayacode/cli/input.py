"""提示工具包的异步输入边界。"""

from __future__ import annotations

import sys
from typing import Any


async def _terminal_prompt(session: Any, label: str, **kwargs: Any) -> str:
    """任务打印通知时保持输入行完整。参数是提示会话与提示语，返回用户输入。
    后台通知来时借用终端安全渲染，避免冲掉正在编辑的行。
    非终端直接提问，调用方按返回串自行裁剪。"""
    if sys.stdout.isatty():
        from prompt_toolkit.patch_stdout import patch_stdout

        with patch_stdout():
            return str(await session.prompt_async(label, **kwargs))
    return str(await session.prompt_async(label, **kwargs))
