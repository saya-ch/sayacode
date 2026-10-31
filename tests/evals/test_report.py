"""报告必须区分运行成功、任务验收、人工待判和不可用用量。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evals.report import generate_report


def _trial(
    root: Path, *, case_id: str = "case-a", variant: str = "single", repeat: int = 1, **changes: Any
) -> Path:
    value = {
        "experiment_id": "test",
        "case_id": case_id,
        "split": "holdout",
        "category": "bugfix",
        "variant": variant,
        "repeat": repeat,
        "status": "completed",
        "passed": True,
        "automatic_passed": True,
        "human_review_required": False,
        "duration_seconds": float(repeat),
        "usage": {
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "reported_calls": 1,
            "missing_usage_calls": 0,
            "model_calls": 1,
        },
        "cost": {"amount": 0.1, "currency": "USD", "coverage": "complete"},
        "checks": [],
        "failure_category": None,
        "task_count": 0,
    }
    value.update(changes)
    path = root / f"{case_id}-{variant}-{repeat}" / "result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_status_is_not_acceptance_and_human_review_stays_pending(tmp_path: Path) -> None:
    _trial(tmp_path)
    _trial(tmp_path, repeat=2, passed=None, human_review_required=True)
    _trial(tmp_path, repeat=3, status="timeout", passed=False, automatic_passed=False)
    group = generate_report(tmp_path)["groups"]["single"]
    assert group["status_counts"]["completed"] == 2
    assert group["status_counts"]["timeout"] == 1
    assert (group["task_passed"], group["task_failed"], group["task_unjudged"]) == (1, 1, 1)
    assert group["task_success_rate"] is None
    assert group["judged_success_rate"] == 0.5
    assert group["automatic_tasks"]["trials"] == 2
    assert group["automatic_tasks"]["task_success_rate"] == 0.5
    assert group["automatic_checks"]["passed"] == 2
    assert group["automatic_checks"]["fully_automatic_acceptances"] == 1
    assert group["human_review"]["pending"] == 1
    assert group["repeat_stability"]["complete_cases"] == 1
    assert group["repeat_stability"]["judged_complete_cases"] == 0
    assert group["repeat_stability"]["all_repeats_passed_rate"] is None


def test_repetition_stability_uses_complete_cases_not_independent_trials(tmp_path: Path) -> None:
    for repeat in range(1, 4):
        _trial(tmp_path, repeat=repeat)
        _trial(tmp_path, case_id="case-b", repeat=repeat, passed=repeat != 2)
    _trial(tmp_path, case_id="incomplete")
    report = generate_report(tmp_path)
    stability = report["groups"]["single"]["repeat_stability"]
    assert stability["complete_cases"] == 2
    assert stability["incomplete_cases"] == 1
    assert stability["all_repeats_passed_cases"] == 1
    assert stability["all_repeats_passed_rate"] == 0.5
    assert "置信区间" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_cost_includes_failed_runs_and_zero_success_is_undefined(tmp_path: Path) -> None:
    _trial(tmp_path)
    _trial(tmp_path, repeat=2, status="failed", passed=False, automatic_passed=False)
    group = generate_report(tmp_path)["groups"]["single"]
    assert group["cost"]["known_amount"] == pytest.approx(0.2)
    assert group["cost"]["per_success"] == pytest.approx(0.2)
    _trial(tmp_path, passed=False, automatic_passed=False)
    assert generate_report(tmp_path)["groups"]["single"]["cost"]["per_success"] is None


def test_partial_usage_never_becomes_a_complete_cost(tmp_path: Path) -> None:
    path = _trial(
        tmp_path,
        usage={
            "input_tokens": 10,
            "output_tokens": 2,
            "total_tokens": 12,
            "reported_calls": 1,
            "missing_usage_calls": 1,
            "model_calls": 2,
        },
        cost={"amount": 0.1, "currency": "USD", "coverage": "partial"},
    )
    original = path.read_bytes()
    group = generate_report(tmp_path)["groups"]["single"]
    assert group["usage"]["input_tokens"] == 10
    assert group["usage"]["coverage"] == "partial"
    assert group["cost"]["coverage"] == "partial"
    assert group["cost"]["per_success"] is None
    assert path.read_bytes() == original


def test_pairing_uses_only_common_judged_repeats_and_preserves_manifest_order(
    tmp_path: Path,
) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps({"config": {"case_ids": ["z", "case-a"], "repeats": 2}}), encoding="utf-8"
    )
    _trial(tmp_path, repeat=1, passed=False)
    _trial(tmp_path, repeat=2)
    _trial(tmp_path, variant="team", repeat=1)
    _trial(tmp_path, variant="team", repeat=3)
    report = generate_report(tmp_path)
    assert report["expected_repeats"] == 2
    assert [p["case_id"] for p in report["paired_cases"]] == ["z", "case-a"]
    pair = report["paired_cases"][1]
    assert pair["common_repeats"] == [1]
    assert pair["left_variant"] == "single"
    assert pair["right_variant"] == "team"
    assert pair["left_only_repeats"] == [2]
    assert pair["right_only_repeats"] == [3]
    assert pair["repeats"][0]["left_passed"] is False
    assert pair["repeats"][0]["right_passed"] is True
    assert pair["mean_success_delta"] == 1


def test_duplicate_identity_fails_instead_of_double_counting(tmp_path: Path) -> None:
    path = _trial(tmp_path)
    duplicate = tmp_path / "duplicate" / "result.json"
    duplicate.parent.mkdir()
    duplicate.write_bytes(path.read_bytes())
    with pytest.raises(ValueError, match="重复"):
        generate_report(tmp_path)


def test_empty_report_and_invalid_repeat_are_explicit(tmp_path: Path) -> None:
    report = generate_report(tmp_path)
    assert report["groups"] == {}
    assert report["paired_cases"] == []
    assert json.loads((tmp_path / "summary.json").read_text(encoding="utf-8")) == report
    with pytest.raises(ValueError, match="正整数"):
        generate_report(tmp_path, expected_repeats=0)


def test_failed_run_cannot_be_counted_successful_by_inconsistent_flag(tmp_path: Path) -> None:
    _trial(tmp_path, status="infrastructure_error", passed=True)
    group = generate_report(tmp_path)["groups"]["single"]
    assert group["task_passed"] == 0
    assert group["task_failed"] == 1
    assert group["automatic_checks"]["passed"] == 0
    assert group["duration_seconds"]["p95_unstable"] is True


def test_missing_usage_record_stays_unknown_even_if_cost_claims_complete(tmp_path: Path) -> None:
    _trial(tmp_path, usage={}, task_count=2)
    group = generate_report(tmp_path)["groups"]["single"]
    assert group["usage"]["coverage"] == "unknown"
    assert group["usage"]["trials_missing_usage"] == 1
    assert group["cost"]["coverage"] == "partial"
    assert group["cost"]["per_success"] is None
    assert group["subtasks"] == {"created": 2, "trials_with_subtasks": 1}


def test_automated_screening_of_review_tasks_never_enters_primary_rate(tmp_path: Path) -> None:
    _trial(tmp_path, case_id="coding", passed=False, automatic_passed=False)
    _trial(tmp_path, case_id="review", human_review_required=True, passed=None)
    report = generate_report(tmp_path)
    group = report["groups"]["single"]
    assert group["automatic_tasks"]["task_success_rate"] == 0
    assert group["automatic_tasks"]["cases"] == 1
    assert group["task_success_rate"] is None
    assert group["automatic_checks"]["pass_rate"] == 0.5
    assert group["human_review"]["pending"] == 1
    assert group["cost"]["per_success"] is None
    content = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "自动编码任务（主指标）" in content
    assert "全部试验与人工审查" in content


def test_null_usage_is_missing_and_call_mismatch_prevents_complete_cost(tmp_path: Path) -> None:
    path = _trial(tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["usage"]["input_tokens"] = None
    path.write_text(json.dumps(value), encoding="utf-8")
    group = generate_report(tmp_path)["groups"]["single"]
    assert group["usage"]["coverage"] == "unknown"
    assert group["cost"]["per_success"] is None
    value["usage"]["input_tokens"] = 10
    value["usage"]["model_calls"] = 2
    path.write_text(json.dumps(value), encoding="utf-8")
    group = generate_report(tmp_path)["groups"]["single"]
    assert group["usage"]["coverage"] == "partial"
    assert group["cost"]["coverage"] == "partial"
    assert group["cost"]["per_success"] is None


@pytest.mark.parametrize("field,value", [("input_tokens", -1), ("model_calls", 1.2)])
def test_invalid_usage_is_rejected_instead_of_truncated(
    tmp_path: Path,
    field: str,
    value: Any,
) -> None:
    path = _trial(tmp_path)
    trial = json.loads(path.read_text(encoding="utf-8"))
    trial["usage"][field] = value
    path.write_text(json.dumps(trial), encoding="utf-8")
    with pytest.raises(ValueError, match="非负整数"):
        generate_report(tmp_path)


@pytest.mark.parametrize("amount", [float("inf"), float("nan"), -0.01, True])
def test_invalid_cost_is_not_silently_added(tmp_path: Path, amount: Any) -> None:
    _trial(tmp_path, cost={"amount": amount, "currency": "USD", "coverage": "complete"})
    with pytest.raises(ValueError, match="有限非负"):
        generate_report(tmp_path)


def test_arbitrary_variant_order_controls_pairing_and_delta_direction(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "config": {"variants": ["summary_on", "summary_off"]},
            }
        ),
        encoding="utf-8",
    )
    _trial(tmp_path, variant="summary_off", passed=False)
    _trial(tmp_path, variant="summary_on")
    report = generate_report(tmp_path)
    assert report["schema_version"] == 2
    assert list(report["groups"]) == ["summary_on", "summary_off"]
    assert list(report["category_groups"]["bugfix"]) == ["summary_on", "summary_off"]
    pair = report["paired_cases"][0]
    assert pair["left_variant"] == "summary_on"
    assert pair["right_variant"] == "summary_off"
    assert pair["mean_success_delta"] == -1
    assert pair["repeats"] == [
        {
            "repeat": 1,
            "left_passed": True,
            "right_passed": False,
            "success_delta": -1,
        }
    ]
    assert "single_only_repeats" not in pair
    content = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "右组减左组" in content
    assert "左组：`summary_on`；右组：`summary_off`" in content


def test_variant_fallback_prefers_single_team_then_sorts_other_labels(tmp_path: Path) -> None:
    for label in ("z", "a", "team", "single"):
        _trial(tmp_path, variant=label)
    report = generate_report(tmp_path)
    assert report["variants"] == ["single", "team", "a", "z"]
    assert report["paired_cases"][0]["left_variant"] == "single"
    assert report["paired_cases"][0]["right_variant"] == "team"


@pytest.mark.parametrize("label", ["", "../escape", "Two", "a b", "汉字", None])
def test_unsafe_trial_variant_is_rejected(tmp_path: Path, label: Any) -> None:
    path = _trial(tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["variant"] = label
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="实验组标签"):
        generate_report(tmp_path)


@pytest.mark.parametrize("variants", [[], [""], ["unsafe/path"], ["same", "same"], "single"])
def test_invalid_configured_variant_list_is_rejected(tmp_path: Path, variants: Any) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps({"config": {"variants": variants}}), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        generate_report(tmp_path)


def test_undeclared_variant_does_not_silently_enter_experiment(tmp_path: Path) -> None:
    _trial(tmp_path, variant="unexpected")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"config": {"variants": ["summary_off", "summary_on"]}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="之外"):
        generate_report(tmp_path)


def test_summary_metrics_preserve_zero_observations_and_separate_missing_fields(
    tmp_path: Path,
) -> None:
    _trial(tmp_path, case_id="a", summary_count=0)
    _trial(tmp_path, case_id="b", summary_count=2, last_graph_tokens_approx=100)
    _trial(tmp_path, case_id="c", last_graph_tokens_approx=200)
    report = generate_report(tmp_path)
    metrics = report["groups"]["single"]["summary_metrics"]
    assert metrics == {
        "summary_count_observed_trials": 2,
        "trials_with_summary": 1,
        "summary_count_total": 2,
        "last_graph_tokens_approx_observed_trials": 2,
        "median_last_graph_tokens_approx": 150,
    }


def test_absent_summary_observation_does_not_become_zero(tmp_path: Path) -> None:
    _trial(tmp_path)
    metrics = generate_report(tmp_path)["groups"]["single"]["summary_metrics"]
    assert metrics["summary_count_observed_trials"] == 0
    assert metrics["trials_with_summary"] is None
    assert metrics["summary_count_total"] is None
    assert metrics["median_last_graph_tokens_approx"] is None


def test_aborted_experiment_is_marked_without_modifying_trials(tmp_path: Path) -> None:
    path = _trial(tmp_path)
    original = path.read_bytes()
    (tmp_path / "abort.json").write_text(json.dumps({"reason": "诊断"}), encoding="utf-8")
    report = generate_report(tmp_path)
    assert report["aborted"] is True
    assert report["eligible_for_comparison"] is False
    assert any("不可作为完整比较" in warning for warning in report["warnings"])
    assert "实验已中止" in (tmp_path / "report.md").read_text(encoding="utf-8")
    assert path.read_bytes() == original


def test_fixture_validation_cannot_be_presented_as_model_effect(tmp_path: Path) -> None:
    _trial(tmp_path, passed=False)
    (tmp_path / "manifest.json").write_text(
        json.dumps({"action": "check-fixtures"}), encoding="utf-8"
    )
    report = generate_report(tmp_path)
    assert report["kind"] == "fixture_validation"
    assert report["eligible_for_comparison"] is False
    assert report["aborted"] is False
    assert any("没有调用模型" in warning for warning in report["warnings"])
    assert "初始仓库判分自检" in (tmp_path / "report.md").read_text(encoding="utf-8")
