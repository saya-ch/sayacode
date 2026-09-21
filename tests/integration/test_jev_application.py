"""Jev 审理在完整应用流中的事件与执行契约。"""

from __future__ import annotations

import hashlib
import json

from langchain_core.messages import AIMessage

import sayacode.application as application_module
from tests.support import ContractModel, contract_app


class AllowReviewer:
    """模拟高置信度允许，避免测试访问外部服务。"""

    async def review(self, tool_calls, state):
        return {
            call["id"]: {
                "tool_call_id": call["id"],
                "tool_name": call["name"],
                "arguments_sha256": hashlib.sha256(
                    json.dumps(
                        call["args"],
                        sort_keys=True,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
                "action": "allow",
                "confidence": 0.98,
                "model": "jev-test",
                "request_id": "request-test",
                "reason": "test approval",
            }
            for call in tool_calls
        }


async def test_jev_auto_approval_is_visible_and_executes_once(tmp_path, monkeypatch) -> None:
    model = ContractModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_file",
                        "args": {"path": "approved.txt", "content": "approved"},
                        "id": "write-1",
                    }
                ],
            ),
            AIMessage(content="done"),
        ]
    )
    app = await contract_app(tmp_path, model)
    monkeypatch.setattr(application_module, "JevReviewer", lambda _config: AllowReviewer())
    try:
        await app.command(
            "reviewer",
            {
                "action": "setup",
                "config": {
                    "base_url": "https://typesafe.invalid",
                    "api_key": "review-key",
                    "model_id": "jev-test",
                },
            },
        )
        await app.command("trust", "jev")
        events = [event async for event in app.stream("write the approved file")]
        kinds = [event["type"] for event in events]
        assert "review.decision" in kinds
        assert "approval.requested" not in kinds
        assert kinds.index("review.decision") < kinds.index("tool.started")
        assert (app.workspace / "approved.txt").read_text(encoding="utf-8") == "approved"
        audit = await app.audit.list(thread_id=app.session_id)
        assert any(row["event"] == "review.decision" for row in audit)
    finally:
        await app.aclose()
