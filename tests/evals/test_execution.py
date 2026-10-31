"""评估执行的计量、隐藏判分和超时收尾合同。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from evals import run
from evals.graders import file_fingerprints, grade_case
from evals.run import ActiveContainers, terminal_http_status
from evals.worker import usage_from_audit


def write_audit(path: Path, events: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def event(kind: str, run_id: str, usage: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"event": f"model.{kind}", "run_id": run_id, "details": {"usage": usage}}


def test_usage_includes_child_calls_and_deduplicates_ends(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    complete = {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}
    child = {"input_tokens": 20, "output_tokens": 2, "total_tokens": 22}
    write_audit(
        path,
        [
            event("started", "main"),
            event("completed", "main", complete),
            event("completed", "main", complete),
            event("started", "child"),
            event("completed", "child", child),
            event("started", "failed"),
            event("failed", "failed"),
        ],
    )
    assert usage_from_audit(path) == {
        "input_tokens": 30,
        "output_tokens": 6,
        "total_tokens": 36,
        "cache_read_tokens": 0,
        "cache_reported_calls": 0,
        "model_calls": 3,
        "reported_calls": 2,
        "missing_usage_calls": 1,
    }


def test_usage_records_prompt_cache_hits_without_affecting_completeness(
    tmp_path: Path,
) -> None:
    """缓存命中单独统计，不改变核心用量完整性判定。"""
    path = tmp_path / "audit.jsonl"
    cached = {
        "input_tokens": 100,
        "output_tokens": 5,
        "total_tokens": 105,
        "cache_read_tokens": 80,
    }
    uncached = {"input_tokens": 50, "output_tokens": 3, "total_tokens": 53}
    write_audit(
        path,
        [
            event("started", "a"),
            event("completed", "a", cached),
            event("started", "b"),
            event("completed", "b", uncached),
        ],
    )
    usage = usage_from_audit(path)
    assert usage["input_tokens"] == 150
    assert usage["cache_read_tokens"] == 80
    assert usage["cache_reported_calls"] == 1
    # 核心三字段都齐全，完整性不受缓存字段缺失影响。
    assert usage["reported_calls"] == 2
    assert usage["missing_usage_calls"] == 0


def test_quota_stop_uses_terminal_status_not_error_text(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    quota = {
        "event": "model.failed",
        "thread_id": "eval-main",
        "details": {"status_code": 429, "error_type": "OpenAIRateLimitError"},
    }
    write_audit(path, [quota])
    assert terminal_http_status(path) == 429
    write_audit(path, [quota, {"event": "model.completed", "thread_id": "eval-main"}])
    assert terminal_http_status(path) is None
    write_audit(
        path,
        [
            {
                "event": "model.failed",
                "thread_id": "eval-main",
                "details": {"message": "429", "error_type": "OtherError"},
            }
        ],
    )
    assert terminal_http_status(path) is None


def test_child_quota_is_not_masked_by_main_completion_or_other_failure(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    quota = {
        "event": "model.failed",
        "thread_id": "eval-child",
        "details": {"status_code": 429},
    }
    write_audit(
        path,
        [
            quota,
            {"event": "model.completed", "thread_id": "eval-main"},
            {"event": "model.failed", "thread_id": "eval-other", "details": {"status_code": 500}},
        ],
    )
    assert terminal_http_status(path) == 429
    write_audit(path, [quota, {"event": "model.completed", "thread_id": "eval-child"}])
    assert terminal_http_status(path) is None


@pytest.mark.parametrize("failure", [None, subprocess.TimeoutExpired("docker", 20)])
def test_stop_preserves_reason_when_container_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch, failure: Exception | None
) -> None:
    def command(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if failure:
            raise failure
        return subprocess.CompletedProcess(args[0], 1, "", "")

    monkeypatch.setattr(run, "command", command)
    active = ActiveContainers()
    active.add("owned-test-container")
    active.stop("model_rate_limit")
    active.stop("operator_interrupt")
    assert active.stopping.is_set()
    assert active.reason == "model_rate_limit"
    assert active.cleanup_errors == ["TimeoutExpired" if failure else "ContainerStopFailed"]


def test_completed_trial_still_stops_on_child_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = run.load_cases()[0]
    experiment = tmp_path / "experiment"

    def container(trial: Path, *_: Any) -> None:
        audit = trial / "state" / "agent" / "audit.jsonl"
        audit.parent.mkdir()
        write_audit(
            audit,
            [{"event": "model.failed", "thread_id": "child", "details": {"status_code": 429}}],
        )

    monkeypatch.setattr(run, "_container", container)
    monkeypatch.setattr(
        run, "command", lambda *args, **_: subprocess.CompletedProcess(args[0], 0, "", "")
    )
    active = ActiveContainers()
    result = run.execute_trial(
        case, "team", 1, experiment, {}, "unused-test-image", fixture_only=True, active=active
    )
    assert result is not None and result["status"] == "completed"
    assert active.reason == "model_rate_limit"


def test_driver_saves_abort_and_report_even_when_stop_command_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "experiment"
    case = next(case for case in run.load_cases() if case["split"] == "dev")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evals.run",
            "run",
            "--cases",
            case["id"],
            "--variants",
            "single",
            "--repeats",
            "1",
            "--concurrency",
            "1",
            "--output",
            str(output),
        ],
    )
    monkeypatch.setattr(run, "build_image", lambda: ("offline-image", "offline-id"))

    def command(args: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        if args[0] == "docker":
            raise subprocess.TimeoutExpired(args, 20)
        return subprocess.CompletedProcess(args, 0, "offline\n", "")

    def execute(*args: Any, **options: Any) -> None:
        options["active"].add("owned-offline-container")
        raise RuntimeError("离线注入的执行器故障")

    monkeypatch.setattr(run, "command", command)
    monkeypatch.setattr(run, "execute_trial", execute)
    with pytest.raises(RuntimeError, match="离线注入"):
        run.main()
    abort = json.loads((output / "abort.json").read_text(encoding="utf-8"))
    report = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert abort["reason"] == "evaluation_error"
    assert abort["cleanup_errors"] == ["TimeoutExpired"]
    assert report["aborted"] is True
    assert report["eligible_for_comparison"] is False


def test_usage_survives_forcibly_truncated_audit(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    write_audit(
        path,
        [
            event("started", "main"),
            event(
                "completed",
                "main",
                {
                    "input_tokens": 10,
                    "output_tokens": 4,
                    "total_tokens": 14,
                },
            ),
            event("started", "unfinished"),
        ],
    )
    with path.open("ab") as stream:
        stream.write(b'{"event":"model.completed","run_id":"unfinished","details":"\xe4\xb8')
    usage = usage_from_audit(path)
    assert usage["input_tokens"] == 10
    assert usage["model_calls"] == 2
    assert usage["reported_calls"] == 1
    assert usage["missing_usage_calls"] >= 1


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": 10},
        {"input_tokens": 10, "output_tokens": None, "total_tokens": 10},
        {"input_tokens": 10, "output_tokens": -1, "total_tokens": 9},
        {"input_tokens": 10, "output_tokens": True, "total_tokens": 11},
    ],
)
def test_incomplete_usage_never_looks_complete(tmp_path: Path, usage: dict[str, Any]) -> None:
    path = tmp_path / "audit.jsonl"
    write_audit(path, [event("started", "main"), event("completed", "main", usage)])
    actual = usage_from_audit(path)
    assert actual["input_tokens"] == 10
    assert actual["missing_usage_calls"] >= 1
    assert actual["output_tokens"] >= 0


def test_hidden_grader_goes_through_stdin_without_becoming_workspace_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "answer.py").write_text("value = 42\n", encoding="utf-8")
    initial = file_fingerprints(workspace)
    seen: list[tuple[list[str], str | None, float]] = []
    code = "from answer import value\nassert value == 42"

    def execute(args: list[str], stdin: str | None, timeout: float) -> tuple[int, str]:
        seen.append((args, stdin, timeout))
        return 0, ""

    checks = grade_case(
        {"checks": [{"type": "python", "code": code}], "protected_paths": ["answer.py"]},
        workspace,
        "",
        execute,
        initial,
    )
    assert all(check["passed"] for check in checks)
    assert seen == [(["python", "-"], code, 30)]
    assert file_fingerprints(workspace) == initial


def test_grader_cannot_read_file_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.txt").write_text("hidden", encoding="utf-8")
    checks = grade_case(
        {"checks": [{"type": "file_contains", "path": "../outside.txt", "values": ["hidden"]}]},
        workspace,
        "",
        lambda *_: (0, ""),
        {},
    )
    assert checks[0]["passed"] is False
    assert checks[0]["error_type"] == "ValueError"


def test_protected_file_change_fails_independent_of_command_exit(tmp_path: Path) -> None:
    protected = tmp_path / "test_public.py"
    protected.write_text("assert False\n", encoding="utf-8")
    initial = file_fingerprints(tmp_path)
    protected.write_text("assert True\n", encoding="utf-8")
    checks = grade_case(
        {
            "checks": [{"type": "command", "args": ["python", "test_public.py"]}],
            "protected_paths": ["test_public.py"],
        },
        tmp_path,
        "",
        lambda *_: (0, ""),
        initial,
    )
    assert checks[0]["passed"] is True
    assert checks[1]["passed"] is False
