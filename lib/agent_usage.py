"""兼容 shim：用量记录已搬至 lib.agent.usage，原路径仅做转发。"""

from lib.agent.usage import (
    estimate_result,
    record_invoke_result,
    record_stream_chunk,
    safe_token_count,
)

__all__ = [
    "estimate_result",
    "record_invoke_result",
    "record_stream_chunk",
    "safe_token_count",
]


def __getattr__(name: str):
    """未显式列出的属性一律转发到新模块，保证旧引用继续生效。"""
    import importlib

    return getattr(importlib.import_module("lib.agent.usage"), name)
