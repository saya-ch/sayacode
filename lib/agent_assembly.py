"""兼容 shim：对话装配已搬至 lib.agent.assembly，原路径仅做转发。"""

from lib.agent.assembly import (
    build_system_content,
    build_system_prompt_text,
    finish_turn,
    history_messages,
    start_turn,
)

__all__ = [
    "build_system_content",
    "build_system_prompt_text",
    "finish_turn",
    "history_messages",
    "start_turn",
]


def __getattr__(name: str):
    """未显式列出的属性一律转发到新模块，保证旧引用继续生效。"""
    import importlib

    return getattr(importlib.import_module("lib.agent.assembly"), name)
