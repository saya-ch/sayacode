"""回归：工具「语义失败」必须进入审计信号。

真实问题：审计里 3865 次工具调用 ``error_type`` **全为 null**。3.2.7 之后的修复
（``fb51bcb``）只认 ``"Tool failed:"`` 字符串前缀，漏掉了最常见的一类：

- ``execute_command_tool`` 以 ``exit_code != 0`` 正常返回（构建失败、测试失败、命令不存在）；
- 命令超时后 ``timed_out = True``。

这类调用 ``ToolMessage.status`` 仍是 ``success``，只有返回值里的结构化字段能看出来。
2026-11-07 复盘把它列为产品存活缺陷（R4 §2）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import ToolMessage

from sayacode.agent.events import EventProjector, _output_failed


def _finished(output: Any, call_id: str = "c1") -> dict[str, Any]:
    return {
        "method": "tools",
        "params": {
            "data": {
                "event": "tool-finished",
                "tool_call_id": call_id,
                "output": output,
            }
        },
    }


def _started(call_id: str = "c1", name: str = "execute_command_tool") -> dict[str, Any]:
    return {
        "method": "tools",
        "params": {
            "data": {
                "event": "tool-started",
                "tool_call_id": call_id,
                "tool_name": name,
                "input": {},
            }
        },
    }


# --- _output_failed：纯函数层 -------------------------------------------------


def test_nonzero_exit_code_is_failure() -> None:
    assert _output_failed({"exit_code": 1, "timed_out": False}) is True


def test_zero_exit_code_is_not_failure() -> None:
    assert _output_failed({"exit_code": 0, "timed_out": False}) is False


def test_timed_out_is_failure() -> None:
    assert _output_failed({"exit_code": 0, "timed_out": True}) is True


def test_tool_failed_prefix_is_failure() -> None:
    assert _output_failed("Tool failed: FileNotFoundError: x") is True


def test_json_encoded_exit_code_is_failure() -> None:
    """部分运行时把 dict 返回值序列化成 JSON 字符串。"""
    assert _output_failed('{"exit_code": 2, "timed_out": false}') is True
    assert _output_failed('{"exit_code": 0, "timed_out": false}') is False


def test_plain_text_is_not_failure() -> None:
    assert _output_failed("src/a.py:1: import os") is False


def test_none_is_not_failure() -> None:
    assert _output_failed(None) is False


# --- EventProjector：接线层 ---------------------------------------------------


def test_completed_event_carries_failure_signal() -> None:
    """exit_code≠0 的调用必须是 tool.completed + tool_failed=True，不能静默成功。"""
    projector = EventProjector()
    projector.normalize(_started(), "t1")
    events = projector.normalize(
        _finished(ToolMessage(content={"exit_code": 1, "timed_out": False}, tool_call_id="c1")),
        "t1",
    )
    assert len(events) == 1
    assert events[0]["type"] == "tool.completed"
    assert events[0]["tool_failed"] is True


def test_completed_event_success_has_no_failure_signal() -> None:
    projector = EventProjector()
    projector.normalize(_started(), "t1")
    events = projector.normalize(
        _finished(ToolMessage(content={"exit_code": 0, "timed_out": False}, tool_call_id="c1")),
        "t1",
    )
    assert events[0]["type"] == "tool.completed"
    assert events[0]["tool_failed"] is False


def test_completed_event_detects_tool_failed_text() -> None:
    projector = EventProjector()
    projector.normalize(_started(), "t1")
    events = projector.normalize(
        _finished(ToolMessage(content="Tool failed: boom", tool_call_id="c1")), "t1"
    )
    assert events[0]["tool_failed"] is True


def test_raised_tool_still_maps_to_failed_event() -> None:
    """原有行为不能回退：工具抛异常仍走 tool.failed 事件。"""
    projector = EventProjector()
    projector.normalize(_started(), "t1")
    events = projector.normalize(
        {
            "method": "tools",
            "params": {"data": {"event": "tool-error", "tool_call_id": "c1", "message": "boom"}},
        },
        "t1",
    )
    assert events[0]["type"] == "tool.failed"
