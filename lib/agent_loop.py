"""兼容 shim：执行循环已搬至 lib.agent.loop，原路径仅做转发。"""

from lib.agent.loop import (
    build_graph_import,
    build_messages,
    coerce_stream_delta,
    extract_response,
    extract_stream_delta,
    extract_token_event,
    invoke_with_messages,
    iter_agent_stream,
    prepare_messages,
    refresh_turn_prompt,
    reminder_state,
    reset_graph_state_for_retry,
    reset_turn_state,
    run_turn,
    run_with_plan_graph,
    stream_extractor_for,
    stream_turn,
    sync_turn_state,
)

__all__ = [
    "build_graph_import",
    "build_messages",
    "coerce_stream_delta",
    "extract_response",
    "extract_stream_delta",
    "extract_token_event",
    "invoke_with_messages",
    "iter_agent_stream",
    "prepare_messages",
    "refresh_turn_prompt",
    "reminder_state",
    "reset_graph_state_for_retry",
    "reset_turn_state",
    "run_turn",
    "run_with_plan_graph",
    "stream_extractor_for",
    "stream_turn",
    "sync_turn_state",
]


def __getattr__(name: str):
    """未显式列出的属性一律转发到新模块，保证 monkeypatch 写法继续生效。"""
    import importlib

    return getattr(importlib.import_module("lib.agent.loop"), name)
