"""本地审计只记录运行元数据，不保存任务或模型请求正文。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage

from sayacode.application import SayacodeApp
from sayacode.audit import AuditLog, LangChainAuditCallback
from sayacode.tasks.inbox import on_task_update
from sayacode.tasks.records import TaskRecord


async def test_concurrent_callbacks_keep_every_audit_event(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    first = AuditLog(path)
    second = AuditLog(path)
    count = 200

    await asyncio.gather(
        *(asyncio.to_thread(first.append_sync, "model.completed", run_id=f"sync-{index}")
          for index in range(count)),
        *(second.append("model.completed", run_id=f"async-{index}")
          for index in range(count)),
    )

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == count * 2
    assert {row["run_id"] for row in rows} == {
        *(f"sync-{index}" for index in range(count)),
        *(f"async-{index}" for index in range(count)),
    }


async def test_model_error_does_not_write_prompt_or_credential_to_audit(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    callback = LangChainAuditCallback(
        audit, thread_id="source-thread", task_id="memory:job-1"
    )
    run_id = uuid4()
    callback.on_chat_model_start(
        {"name": "memory-contract"},
        [[HumanMessage(content="用户的私有记忆正文")]],
        run_id=run_id,
    )
    callback.on_llm_error(
        RuntimeError("api_key=visible-secret; prompt=用户的私有记忆正文"), run_id=run_id
    )

    rows = await audit.list(thread_id="source-thread")
    assert [row["event"] for row in rows] == ["model.started", "model.failed"]
    assert rows[-1]["details"]["error_type"] == "RuntimeError"
    assert rows[-1]["task_id"] == "memory:job-1"
    assert "visible-secret" not in audit.path.read_text(encoding="utf-8")
    assert "用户的私有记忆正文" not in audit.path.read_text(encoding="utf-8")


async def test_usage_audit_keeps_numeric_totals_only(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    callback = LangChainAuditCallback(audit, thread_id="source-thread", task_id="memory:job-2")
    run_id = uuid4()
    response = SimpleNamespace(
        generations=[
            [
                SimpleNamespace(
                    message=SimpleNamespace(
                        usage_metadata={
                            "input_tokens": 12,
                            "output_tokens": 7,
                            "total_tokens": 19,
                            "raw": "api_key=visible-secret; private source",
                        }
                    )
                )
            ]
        ]
    )
    callback.on_llm_end(response, run_id=run_id)
    rows = await audit.list(thread_id="source-thread")
    assert rows[0]["details"]["usage"] == {
        "input_tokens": 12,
        "output_tokens": 7,
        "total_tokens": 19,
    }
    assert "visible-secret" not in audit.path.read_text(encoding="utf-8")


async def test_invalid_utf8_audit_line_does_not_block_valid_history(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    await audit.append("run.started", thread_id="session-1")
    with audit.path.open("ab") as handle:
        handle.write(b'{"id":"broken","event":"tool.started","text":"\x98"}\n')
        handle.write(b"not-json\n")
    await audit.append("run.completed", thread_id="session-1")

    original = audit.path.read_bytes()
    rows = await audit.list(thread_id="session-1")
    assert [row["event"] for row in rows] == ["run.started", "run.completed"]
    assert [row["event"] for row in await audit.list(thread_id="session-1", limit=1)] == [
        "run.completed"
    ]
    assert audit.path.read_bytes() == original


async def test_task_status_audit_omits_task_body_and_private_snapshot(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    app = SimpleNamespace(audit=audit, _notifications=asyncio.Queue())
    record = TaskRecord(
        task_id="task-1",
        thread_id="task-task-1",
        parent_thread_id=None,
        role="reviewer",
        prompt="private-prompt",
        pending_input="private-followup",
        title="private-title",
        workspace=str(tmp_path),
        worktree_enabled=False,
        status="failed",
        last_outcome="failed",
        result="private-result",
        error="private-error",
        context_snapshot={"text": "private-context"},
        profile_snapshot={"api_key": "private-key"},
    )

    await on_task_update(app, record)

    rows = await audit.list(thread_id=record.thread_id)
    assert len(rows) == 1
    assert rows[0]["event"] == "task.status"
    assert rows[0]["task_id"] == record.task_id
    assert rows[0]["details"] == {
        "status": "failed",
        "role": "reviewer",
        "outcome": "failed",
        "turn_seq": 0,
        "delivery_state": "none",
        "parent_thread_id": None,
        "worktree_enabled": False,
        "unconfirmed_effects": False,
    }
    assert "private-" not in audit.path.read_text(encoding="utf-8")


async def test_tool_audit_does_not_persist_arguments_or_output(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    app = SimpleNamespace(audit=audit)
    await SayacodeApp._record_tool_event(
        app,
        {
            "type": "tool.completed",
            "tool_name": "grep_search",
            "tool_call_id": "call-1",
            "tool_input": {"pattern": "private-search-pattern", "path": "private-file-path"},
            "tool_output": "private-result",
            "duration_ms": 17,
        },
        "session-1",
    )

    rows = await audit.list(thread_id="session-1")
    assert rows[0]["details"] == {
        "tool_call_id": "call-1",
        "tool_name": "grep_search",
        "duration_ms": 17,
        "output_characters": len("private-result"),
        "error_type": None,
    }
    assert "private-" not in audit.path.read_text(encoding="utf-8")


async def test_error_chain_records_types_and_status_without_transport_contents(
    tmp_path: Path,
) -> None:
    class TransportFailure(RuntimeError):
        status_code = 503

        def __str__(self) -> str:
            raise AssertionError("审计不能读取异常正文")

        @property
        def response(self) -> None:
            raise AssertionError("审计不能读取 HTTP 响应")

        @property
        def request(self) -> None:
            raise AssertionError("审计不能读取 HTTP 请求")

    inner = TransportFailure("private-key private-prompt private-thinking")
    contextual = OSError("private-url private-headers private-body")
    inner.__context__ = contextual
    outer = RuntimeError("private-key private-request")
    outer.__cause__ = inner
    outer.__context__ = ValueError("不应优先于显式 cause")
    audit = AuditLog(tmp_path / "audit.jsonl")
    events: list[dict] = []
    callback = LangChainAuditCallback(audit, thread_id="session-1", on_model_event=events.append)

    callback.on_llm_error(outer, run_id=uuid4())

    details = (await audit.list())[0]["details"]
    assert details == {
        "error_type": "RuntimeError",
        "error_chain": ["RuntimeError", "TransportFailure", "OSError"],
        "status_code": 503,
    }
    assert events[0]["error_chain"] == details["error_chain"]
    assert "private-" not in audit.path.read_text(encoding="utf-8")


async def test_error_chain_stops_at_cycles_and_five_layers(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path / "audit.jsonl")
    callback = LangChainAuditCallback(audit, thread_id="session-1")
    first, second = RuntimeError("private-first"), ValueError("private-second")
    first.__cause__ = second
    second.__context__ = first
    callback.on_llm_error(first, run_id=uuid4())

    nodes = [RuntimeError("private-chain") for _ in range(7)]
    for parent, child in zip(nodes, nodes[1:]):
        parent.__cause__ = child
    callback.on_tool_error(nodes[0], run_id=uuid4())

    rows = await audit.list()
    assert rows[0]["details"]["error_chain"] == ["RuntimeError", "ValueError"]
    assert rows[1]["details"]["error_chain"] == ["RuntimeError"] * 5
    assert "private-" not in audit.path.read_text(encoding="utf-8")


@pytest.mark.parametrize("status", [True, "401", 99, 600, 401.0, None])
async def test_non_http_status_values_are_not_recorded(tmp_path: Path, status: object) -> None:
    class StatusFailure(RuntimeError):
        status_code = status

    audit = AuditLog(tmp_path / "audit.jsonl")
    callback = LangChainAuditCallback(audit, thread_id="session-1")
    callback.on_llm_error(StatusFailure("private-message"), run_id=uuid4())

    details = (await audit.list())[0]["details"]
    assert "status_code" not in details


async def test_status_property_failure_does_not_break_diagnostics(tmp_path: Path) -> None:
    class StatusFailure(RuntimeError):
        @property
        def status_code(self) -> int:
            raise ValueError("private-status-error")

    audit = AuditLog(tmp_path / "audit.jsonl")
    callback = LangChainAuditCallback(audit, thread_id="session-1")
    callback.on_llm_error(StatusFailure("private-message"), run_id=uuid4())

    details = (await audit.list())[0]["details"]
    assert details == {"error_type": "StatusFailure", "error_chain": ["StatusFailure"]}
    assert "private-" not in audit.path.read_text(encoding="utf-8")
