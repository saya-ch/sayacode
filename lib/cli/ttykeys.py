"""POSIX 原始终端按键读取：菜单与权限提示共用转义序列解析。

职责：封装 termios/tty 的 raw 模式与 ESC 序列读取。
核心函数：raw_terminal、read_escape_tail。
调用链位置：lib.cli.parser 与 lib.cli.permissions → 本模块。
Windows 走 msvcrt，由各调用点自行分支。
"""

from __future__ import annotations

import select
import sys
from contextlib import contextmanager
from typing import Iterator


@contextmanager
def raw_terminal() -> Iterator[None]:
    """把 stdin 切到 raw 模式，退出时无条件恢复原设置。"""
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def read_escape_tail(timeout: float = 0.01, length: int = 2) -> str:
    """读取 ESC 之后终端已就绪的转义序列字节（最多 ``length`` 个）。

    只消费「已经送到」的字节：单独按 ESC 时结果为空的序列，调用点据此
    把它当独立按键，而不是阻塞等待后续输入。
    """
    sequence = ""
    while len(sequence) < length and select.select([sys.stdin], [], [], timeout)[0]:
        sequence += sys.stdin.read(1)
    return sequence


__all__ = ["raw_terminal", "read_escape_tail"]
