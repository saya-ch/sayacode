"""离线检查父线程结果唤醒参与评估终态，不调用模型或网络。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evals import worker


@pytest.mark.parametrize(
    ("initial", "wake", "child_status", "expected"),
    [
        ("run.completed", None, "idle", "completed"),
        ("run.completed", "agent.wake.completed", "failed", "completed"),
        ("run.completed", "agent.wake.failed", "idle", "failed"),
        ("run.completed", "agent.wake.paused", "idle", "paused"),
        ("run.completed", "agent.wake.stopped", "idle", "failed"),
        ("run.failed", "agent.wake.completed", "idle", "failed"),
        ("run.paused", None, "idle", "paused"),
    ],
)
def test_turn_status_uses_parent_outcomes_without_rejecting_recovered_child(
    initial: str, wake: str | None, child_status: str, expected: str
) -> None:
    task: dict[str, Any] = {"task_id": "child", "status": child_status}
    if wake is not None:
        task["parent_wake"] = {"type": wake}
    assert worker.turn_status({"type": initial}, [task]) == expected


@pytest.mark.parametrize(
    ("wake_type", "expected", "expected_prompts"),
    [
        ("agent.wake.failed", "failed", ["初始任务"]),
        ("agent.wake.paused", "paused", ["初始任务"]),
        ("agent.wake.completed", "completed", ["初始任务", "脚本后续要求"]),
    ],
)
async def test_worker_persists_parent_wake_outcome_before_followup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wake_type: str,
    expected: str,
    expected_prompts: list[str],
) -> None:
    input_path = tmp_path / "state" / "input.json"
    input_path.parent.mkdir()
    input_path.write_text(
        json.dumps(
            {
                "prompt": "初始任务",
                "followups": ["脚本后续要求"],
                "team_enabled": True,
                "profile": {
                    "name": "offline",
                    "protocol": "openai_chat_completions",
                    "base_url": "https://unused.invalid/v1",
                    "api_key": "test-only",
                    "model_id": "offline",
                    "context_length": 8192,
                    "max_output_tokens": 512,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    prompts: list[str] = []
    closed: list[bool] = []
    record = SimpleNamespace(
        task_id="child",
        thread_id="task-child",
        parent_thread_id="eval-main",
        role="reviewer",
        status="failed",
    )

    async def get_state(*_args: Any) -> SimpleNamespace:
        return SimpleNamespace(values={"messages": []}, next=(), interrupts=())

    runtime = SimpleNamespace(get_state=get_state)

    async def open_runtime(_path: Path) -> SimpleNamespace:
        return runtime

    class OfflineApp:
        def __init__(self, **options: Any):
            self.runtime = runtime
            self.session_id = options["session_id"]
            self.tasks = SimpleNamespace(list=self.list_tasks)
            self.waits = 0

        async def initialize(self) -> None:
            pass

        async def stream(self, prompt: str, **options: Any):
            prompts.append(prompt)
            yield {"type": "run.completed", "response": "初始轮次已完成"}

        async def wait_for_tasks(self) -> list[dict[str, Any]]:
            self.waits += 1
            if self.waits > 1:
                return []
            # 与产品 wait_for_tasks 一致：一个 TaskRecord 视图附带单个 parent_wake。
            return [
                {
                    "task_id": "child",
                    "status": "failed",
                    "parent_wake": {
                        "type": wake_type,
                        "response": "父线程恢复处理的答案",
                    },
                }
            ]

        async def list_tasks(self) -> list[SimpleNamespace]:
            return [record]

        async def _context_for_thread(self, thread_id: str) -> tuple[object, None]:
            return object(), None

        async def aclose(self) -> None:
            closed.append(True)

    monkeypatch.setattr(worker, "AgentRuntime", SimpleNamespace(open=open_runtime))
    monkeypatch.setattr(worker, "SayacodeApp", OfflineApp)
    monkeypatch.setattr(worker, "workspace_digest", lambda: "unchanged")
    result = await worker.run(input_path)

    assert result["status"] == expected
    assert prompts == expected_prompts
    assert result["turns"][0]["final"]["type"] == "run.completed"
    assert result["turns"][0]["status"] == expected
    assert result["failed_child_tasks"] == 1
    assert result["task_count"] == 1
    assert "父线程恢复处理的答案" in result["response"]
    persisted = json.loads((input_path.parent / "worker-result.json").read_text(encoding="utf-8"))
    assert persisted["status"] == expected
    assert closed == [True]
