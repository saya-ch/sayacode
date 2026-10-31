"""诊断不能把已恢复的模型错误当成最终任务失败。"""

from evals.diagnose import _model_seconds, classify


def test_recovered_connection_does_not_override_acceptance() -> None:
    audit = [
        {
            "thread_id": "eval-main",
            "event": "model.failed",
            "details": {"error_type": "OpenAIConnectionError"},
        },
        {"thread_id": "eval-main", "event": "model.completed", "details": {}},
    ]
    assert classify({"status": "completed", "passed": True}, audit) == "accepted"
    assert classify({"status": "failed", "passed": False}, audit) == "runtime_failure_unclassified"
    assert classify({"status": "failed", "passed": False}, audit[:1]) == "model_connection"


def test_model_duration_unions_parallel_intervals() -> None:
    audit = [
        {
            "event": "model.completed",
            "at": "2026-10-21T00:00:10+00:00",
            "details": {"duration_ms": 10_000},
        },
        {
            "event": "model.completed",
            "at": "2026-10-21T00:00:15+00:00",
            "details": {"duration_ms": 10_000},
        },
    ]
    assert _model_seconds(audit) == 15
    assert _model_seconds([]) is None
