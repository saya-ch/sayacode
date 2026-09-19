"""兼容 shim：流抽取已搬至 lib.agent.stream，原路径仅做转发。"""

from lib.agent.stream import AgentStreamExtractor

__all__ = ["AgentStreamExtractor"]


def __getattr__(name: str):
    """未显式列出的属性一律转发到新模块，保证旧引用继续生效。"""
    import importlib

    return getattr(importlib.import_module("lib.agent.stream"), name)
