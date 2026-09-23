"""记忆整理模型的审计只记录元数据，不保存异常中的请求材料。"""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from langchain_core.messages import HumanMessage

from sayacode.audit import AuditLog, LangChainAuditCallback


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
