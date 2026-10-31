"""汇总真实任务评估，保留运行状态、验收结果和缺失数据之间的区别。"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

STATUSES = ("completed", "failed", "paused", "timeout", "infrastructure_error")
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "reported_calls",
    "missing_usage_calls",
    "model_calls",
)
VARIANT_LABEL = re.compile(r"[a-z0-9_-]+")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是对象：{path}")
    return value


def _verdict(trial: dict[str, Any]) -> bool | None:
    # 正常结束不能代替任务验收；异常和超时也不能被记录中的误填 true 计为成功。
    return trial.get("passed") if trial["status"] == "completed" else False


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _outcomes(trials: list[dict[str, Any]]) -> dict[str, Any]:
    verdicts = [_verdict(trial) for trial in trials]
    successes = sum(verdict is True for verdict in verdicts)
    failures = sum(verdict is False for verdict in verdicts)
    unjudged = sum(verdict is None for verdict in verdicts)
    return {
        "trials": len(trials),
        "cases": len({t["case_id"] for t in trials}),
        "task_passed": successes,
        "task_failed": failures,
        "task_unjudged": unjudged,
        "task_success_rate": _ratio(successes, len(trials)) if not unjudged else None,
        "judged_success_rate": _ratio(successes, successes + failures),
    }


def _summary_metrics(trials: list[dict[str, Any]]) -> dict[str, Any]:
    counts = [t["summary_count"] for t in trials if t.get("summary_count") is not None]
    tokens = [
        t["last_graph_tokens_approx"]
        for t in trials
        if t.get("last_graph_tokens_approx") is not None
    ]
    return {
        "summary_count_observed_trials": len(counts),
        "trials_with_summary": sum(count > 0 for count in counts) if counts else None,
        "summary_count_total": sum(counts) if counts else None,
        "last_graph_tokens_approx_observed_trials": len(tokens),
        "median_last_graph_tokens_approx": statistics.median(tokens) if tokens else None,
    }


def _variant_order(trials: list[dict[str, Any]], config: dict[str, Any]) -> list[str]:
    labels = {t["variant"] for t in trials}
    configured = config.get("variants")
    if configured is not None:
        if not isinstance(configured, list) or not configured:
            raise ValueError("manifest.config.variants 必须是非空标签列表")
        if any(
            not isinstance(label, str) or not VARIANT_LABEL.fullmatch(label) for label in configured
        ):
            raise ValueError("实验组标签只能包含小写字母、数字、下划线和连字符")
        if len(configured) != len(set(configured)):
            raise ValueError("manifest.config.variants 不能包含重复标签")
        if labels - set(configured):
            raise ValueError("试验包含 manifest.config.variants 之外的实验组")
        return configured
    return sorted(
        labels,
        key=lambda label: (
            ("single", "team").index(label) if label in ("single", "team") else 2,
            label,
        ),
    )


def _summarize(trials: list[dict[str, Any]], expected_repeats: int) -> dict[str, Any]:
    statuses = Counter(trial["status"] for trial in trials)
    outcomes = _outcomes(trials)
    durations = sorted(float(trial["duration_seconds"]) for trial in trials)
    automatic_judged = [t for t in trials if isinstance(t.get("automatic_passed"), bool)]
    automatic_passes = sum(
        t["status"] == "completed" and t["automatic_passed"] is True for t in automatic_judged
    )
    review_required = [t for t in trials if t.get("human_review_required") is True]
    cases = sorted({t["case_id"] for t in trials})
    complete_cases = [
        case_id
        for case_id in cases
        if {t["repeat"] for t in trials if t["case_id"] == case_id}
        == set(range(1, expected_repeats + 1))
    ]
    fully_judged = [
        case_id
        for case_id in complete_cases
        if all(_verdict(t) is not None for t in trials if t["case_id"] == case_id)
    ]
    stable_cases = sum(
        all(_verdict(t) is True for t in trials if t["case_id"] == case_id)
        for case_id in fully_judged
    )
    usage = {
        field: sum(t.get("usage", {}).get(field) or 0 for t in trials) for field in USAGE_FIELDS
    }
    # 缓存命中只解释成本，不参与用量完整性判定。
    cache_read_tokens = sum(
        int(t.get("usage", {}).get("cache_read_tokens") or 0) for t in trials
    )
    cache_reported_calls = sum(
        int(t.get("usage", {}).get("cache_reported_calls") or 0) for t in trials
    )
    usage_records_missing = sum(
        any(t.get("usage", {}).get(field) is None for field in USAGE_FIELDS) for t in trials
    )
    amounts = [t.get("cost", {}).get("amount") for t in trials]
    cost_complete = (
        bool(trials)
        and not usage_records_missing
        and all(
            t.get("cost", {}).get("coverage") == "complete"
            and isinstance(amount, (int, float))
            and t.get("usage", {}).get("missing_usage_calls", 0) == 0
            and t["usage"]["reported_calls"] == t["usage"]["model_calls"]
            for t, amount in zip(trials, amounts, strict=True)
        )
    )
    known_amounts = [float(amount) for amount in amounts if isinstance(amount, (int, float))]
    cost_sum = sum(known_amounts) if known_amounts else None
    successes = outcomes["task_passed"]
    return {
        **outcomes,
        "status_counts": {status: statuses[status] for status in STATUSES},
        "automatic_tasks": _outcomes(
            [t for t in trials if t.get("human_review_required") is False]
        ),
        "automatic_checks": {
            "judged": len(automatic_judged),
            "passed": automatic_passes,
            "pass_rate": _ratio(automatic_passes, len(automatic_judged)),
            "fully_automatic_acceptances": sum(
                _verdict(t) is True and not t.get("human_review_required") for t in trials
            ),
        },
        "human_review": {
            "required": len(review_required),
            "pending": sum(_verdict(t) is None for t in review_required),
            "accepted": sum(_verdict(t) is True for t in review_required),
            "failed": sum(_verdict(t) is False for t in review_required),
        },
        "repeat_stability": {
            "expected_repeats": expected_repeats,
            "complete_cases": len(complete_cases),
            "judged_complete_cases": len(fully_judged),
            "incomplete_cases": len(cases) - len(complete_cases),
            "all_repeats_passed_cases": stable_cases,
            "all_repeats_passed_rate": _ratio(stable_cases, len(fully_judged)),
        },
        "duration_seconds": {
            "median": statistics.median(durations) if durations else None,
            "p95": durations[math.ceil(len(durations) * 0.95) - 1] if durations else None,
            "p95_unstable": len(durations) < 100,
            "p95_method": "nearest_rank",
        },
        "usage": {
            **usage,
            "cache_read_tokens": cache_read_tokens,
            "cache_reported_calls": cache_reported_calls,
            "cache_hit_rate": _ratio(cache_read_tokens, usage["input_tokens"]),
            "trials_missing_usage": usage_records_missing,
            "coverage": (
                "unknown"
                if not trials
                or usage_records_missing
                or (usage["model_calls"] and not usage["reported_calls"])
                else "partial"
                if usage["missing_usage_calls"] or usage["reported_calls"] < usage["model_calls"]
                else "complete"
            ),
        },
        "subtasks": {
            "created": sum(int(t.get("task_count", 0)) for t in trials),
            "trials_with_subtasks": sum(t.get("task_count", 0) > 0 for t in trials),
        },
        "summary_metrics": _summary_metrics(trials),
        "cost": {
            "currency": "USD",
            "known_amount": cost_sum,
            "coverage": "complete" if cost_complete else "partial" if known_amounts else "unknown",
            "per_success": cost_sum / successes
            if cost_complete
            and successes
            and not outcomes["task_unjudged"]
            and cost_sum is not None
            else None,
        },
        "failure_categories": dict(
            Counter(t["failure_category"] for t in trials if t.get("failure_category"))
        ),
    }


def _paired_cases(
    trials: list[dict[str, Any]],
    ordered_cases: list[str],
    variants: list[str],
) -> list[dict[str, Any]]:
    if len(variants) < 2:
        return []
    left_variant, right_variant = variants[:2]
    pairs = []
    for case_id in ordered_cases:
        by_variant = {
            variant: {
                trial["repeat"]: trial
                for trial in trials
                if trial["case_id"] == case_id and trial["variant"] == variant
            }
            for variant in (left_variant, right_variant)
        }
        common = sorted(by_variant[left_variant].keys() & by_variant[right_variant].keys())
        repeats = []
        for repeat in common:
            left = _verdict(by_variant[left_variant][repeat])
            right = _verdict(by_variant[right_variant][repeat])
            repeats.append(
                {
                    "repeat": repeat,
                    "left_passed": left,
                    "right_passed": right,
                    "success_delta": int(right) - int(left)
                    if left is not None and right is not None
                    else None,
                }
            )
        deltas = [r["success_delta"] for r in repeats if r["success_delta"] is not None]
        pairs.append(
            {
                "case_id": case_id,
                "left_variant": left_variant,
                "right_variant": right_variant,
                "common_repeats": common,
                "left_only_repeats": sorted(by_variant[left_variant].keys() - set(common)),
                "right_only_repeats": sorted(by_variant[right_variant].keys() - set(common)),
                "judged_pairs": len(deltas),
                "mean_success_delta": statistics.mean(deltas) if deltas else None,
                "repeats": repeats,
            }
        )
    return pairs


def _display(value: float | None, *, percent: bool = False) -> str:
    if value is None:
        return "未知／不适用"
    return f"{value:.1%}" if percent else f"{value:.2f}"


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# SAYACODE 初始仓库判分自检"
        if report["kind"] == "fixture_validation"
        else "# SAYACODE 任务评估报告",
        "",
        *[f"> 注意：{warning}" for warning in report["warnings"]],
        "",
        f"预期每题重复 **{report['expected_repeats']} 次**。状态正常结束与任务验收分开统计。",
        "",
        "## 自动编码任务（主指标）",
        "",
        "仅包含不需要人工复核的任务；审查题的自动筛查结果不混入本表。",
        "",
        "| 组别 | 题数 | 原始 N | 成功 | 失败 | 未判定 | 成功率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for variant, group in report["groups"].items():
        automatic = group["automatic_tasks"]
        lines.append(
            f"| {variant} | {automatic['cases']} | {automatic['trials']} "
            f"| {automatic['task_passed']} | {automatic['task_failed']} "
            f"| {automatic['task_unjudged']} "
            f"| {_display(automatic['task_success_rate'], percent=True)} |"
        )
    lines += [
        "",
        "## 全部试验与人工审查",
        "",
        "存在未判任务时整体成功率保持未知；已判成功率只作参考，可能存在选择偏差。",
        "自动检查通过只是筛查，不能替代人工审查验收。",
        "",
        "| 组别 | 题数 | 原始 N | 任务成功 | 失败 | 未判定 | 整体成功率 | 已判成功率 | 筛查通过／已判 | 待人工 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant, group in report["groups"].items():
        automatic = group["automatic_checks"]
        lines.append(
            f"| {variant} | {group['cases']} | {group['trials']} | {group['task_passed']} "
            f"| {group['task_failed']} | {group['task_unjudged']} "
            f"| {_display(group['task_success_rate'], percent=True)} "
            f"| {_display(group['judged_success_rate'], percent=True)} "
            f"| {automatic['passed']}/{automatic['judged']} | {group['human_review']['pending']} |"
        )
    lines += ["", "## 状态、稳定性与资源", ""]
    for variant, group in report["groups"].items():
        stability, duration, cost, usage = (
            group["repeat_stability"],
            group["duration_seconds"],
            group["cost"],
            group["usage"],
        )
        lines += [
            f"### {variant}",
            "",
            "- 运行状态："
            + "，".join(f"{k} {v}" for k, v in group["status_counts"].items())
            + "。",
            f"- 全部重复均成功：{stability['all_repeats_passed_cases']}/"
            f"{stability['judged_complete_cases']} 道完整且已判题；"
            f"不完整题 {stability['incomplete_cases']}，不纳入该比例。",
            f"- 耗时中位数 {_display(duration['median'])} 秒；P95 {_display(duration['p95'])} 秒"
            + ("（小样本，不稳定）。" if duration["p95_unstable"] else "。"),
            f"- 已报告 token：输入 {usage['input_tokens']}，输出 {usage['output_tokens']}；"
            f"模型调用 {usage['model_calls']}，缺少用量 {usage['missing_usage_calls']}。"
            f"用量覆盖：{usage['coverage']}，用量字段缺失的试验 {usage['trials_missing_usage']}。",
            f"- 输入缓存命中 {usage['cache_read_tokens']} token"
            f"（占输入 {_display(usage['cache_hit_rate'], percent=True)}；"
            f"命中观测覆盖 {usage['cache_reported_calls']} 次调用）。",
            f"- 创建子任务 {group['subtasks']['created']} 个，"
            f"实际派发的试验 {group['subtasks']['trials_with_subtasks']} 次。",
            f"- 摘要计数观测 {group['summary_metrics']['summary_count_observed_trials']} 次试验；"
            f"触发摘要的试验 {_display(group['summary_metrics']['trials_with_summary'])} 次，"
            f"摘要版本总数 {_display(group['summary_metrics']['summary_count_total'])}。",
            f"- 末尾图状态近似 token 观测 "
            f"{group['summary_metrics']['last_graph_tokens_approx_observed_trials']} 次试验，"
            f"中位数 {_display(group['summary_metrics']['median_last_graph_tokens_approx'])}。",
            f"- 已知费用 {_display(cost['known_amount'])} USD（{cost['coverage']}）；"
            f"每次成功费用 {_display(cost['per_success'])} USD。失败试验的费用也计入总费用。",
            "",
        ]
    lines += [
        "## 逐题配对",
        "",
        "仅比较配置顺序中前两个组的共同 repeat；人工未判的结果不用于计算差值。"
        "差值为右组减左组，正数表示右组成功率更高。",
        (
            f"左组：`{report['variants'][0]}`；右组：`{report['variants'][1]}`。"
            if len(report["variants"]) >= 2
            else "不足两个实验组，不生成配对结论。"
        ),
        "",
        "| 任务 | 共同重复编号 | 已判配对数 | 平均成功差值 |",
        "|---|---|---:|---:|",
    ]
    for pair in report["paired_cases"]:
        case_id = pair["case_id"].replace("|", "\\|")
        repeats = ", ".join(str(r) for r in pair["common_repeats"]) or "无"
        lines.append(
            f"| {case_id} | {repeats} | {pair['judged_pairs']} "
            f"| {_display(pair['mean_success_delta'], percent=True)} |"
        )
    lines += ["", "## 口径与限制", "", *[f"- {note}" for note in report["limitations"]], ""]
    return "\n".join(lines)


def generate_report(
    results_root: Path,
    *,
    expected_repeats: int | None = None,
) -> dict[str, Any]:
    """读取 trial，不修改原始记录，在结果根目录写 summary.json 和 report.md。"""
    manifest_path = results_root / "manifest.json"
    manifest = _read_json(manifest_path) if manifest_path.exists() else {}
    config = manifest.get("config", {})
    if expected_repeats is None:
        expected_repeats = manifest.get(
            "expected_repeats", config.get("expected_repeats", config.get("repeats", 3))
        )
    if not isinstance(expected_repeats, int) or expected_repeats < 1:
        raise ValueError("expected_repeats 必须是正整数")
    trials = [_read_json(path) for path in sorted(results_root.rglob("result.json"))]
    identities: set[tuple[str, str, int]] = set()
    for trial in trials:
        identity = (trial["case_id"], trial["variant"], trial["repeat"])
        if identity in identities:
            raise ValueError(f"同一试验出现重复记录：{identity}")
        identities.add(identity)
        if trial["status"] not in STATUSES:
            raise ValueError(f"未知的运行状态或分组：{identity}")
        if not isinstance(trial["variant"], str) or not VARIANT_LABEL.fullmatch(trial["variant"]):
            raise ValueError(f"实验组标签只能包含小写字母、数字、下划线和连字符：{identity}")
        if not isinstance(trial["repeat"], int) or trial["repeat"] < 1:
            raise ValueError(f"repeat 必须从 1 开始：{identity}")
        duration = float(trial["duration_seconds"])
        if not math.isfinite(duration) or duration < 0:
            raise ValueError(f"耗时必须是有限非负数：{identity}")
        for field in ("passed", "automatic_passed"):
            if trial.get(field) is not None and not isinstance(trial[field], bool):
                raise ValueError(f"{field} 必须为布尔值或 null：{identity}")
        usage = trial.get("usage", {})
        if not isinstance(usage, dict):
            raise ValueError(f"usage 必须为对象：{identity}")
        for field in USAGE_FIELDS:
            value = usage.get(field)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"usage.{field} 必须为非负整数或 null：{identity}")
        cost = trial.get("cost", {})
        if not isinstance(cost, dict):
            raise ValueError(f"cost 必须为对象：{identity}")
        amount = cost.get("amount")
        if amount is not None and (
            type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0
        ):
            raise ValueError(f"费用必须为有限非负数或 null：{identity}")
        if amount is not None and cost.get("currency") != "USD":
            raise ValueError(f"不能混合非 USD 费用：{identity}")
        count = trial.get("summary_count")
        if count is not None and (type(count) is not int or count < 0):
            raise ValueError(f"summary_count 必须为非负整数或 null：{identity}")
        tokens = trial.get("last_graph_tokens_approx")
        if tokens is not None and (
            type(tokens) not in (int, float) or not math.isfinite(tokens) or tokens < 0
        ):
            raise ValueError(f"last_graph_tokens_approx 必须为有限非负数或 null：{identity}")
    configured_cases = manifest.get("case_ids", config.get("case_ids", []))
    ordered_cases = list(
        dict.fromkeys(
            [
                *configured_cases,
                *sorted({t["case_id"] for t in trials}),
            ]
        )
    )
    variants = _variant_order(trials, config)
    groups = {
        variant: _summarize([t for t in trials if t["variant"] == variant], expected_repeats)
        for variant in variants
    }
    abort_path = results_root / "abort.json"
    aborted = abort_path.is_file()
    if aborted:
        _read_json(abort_path)
    fixture_validation = manifest.get("action") == "check-fixtures"
    invalid = (results_root / "invalid.json").is_file()
    warnings = ["实验已中止，现有结果只能用于诊断，不可作为完整比较。"] if aborted else []
    if invalid:
        warnings.append("本批已标记外部条件失效，保留原始结果，但不可用于机制收益比较。")
    if fixture_validation:
        warnings.append("本次为初始仓库判分自检，没有调用模型，不可作为 Agent 效果成绩。")
    report = {
        "schema_version": 2,
        "kind": "fixture_validation" if fixture_validation else "agent_evaluation",
        "eligible_for_comparison": not aborted and not fixture_validation and not invalid,
        "experiment_ids": sorted({t["experiment_id"] for t in trials}),
        "expected_repeats": expected_repeats,
        "variants": variants,
        "aborted": aborted,
        "invalidated": invalid,
        "warnings": warnings,
        "groups": groups,
        "category_groups": {
            category: {
                variant: _summarize(
                    [t for t in trials if t["variant"] == variant and t["category"] == category],
                    expected_repeats,
                )
                for variant in groups
            }
            for category in sorted({t["category"] for t in trials})
        },
        "paired_cases": _paired_cases(trials, ordered_cases, variants),
        "limitations": [
            "本报告只统计已落盘的试验；计划但未执行的组合不会被算成成功或失败。",
            "主指标只包含不需人工复核的任务；审查任务的自动筛查不混入该成功率。",
            "passed=null 保持未判；有未判样本时整体成功率为未知，已判成功率仅作参考。",
            "成功率分母包含失败、超时、暂停和基础设施错误，状态计数同时列出。",
            "完整重复稳定性要求存在 1..预期重复数的全部记录，且任务判定完整。",
            "输入输出 token 是已报告部分；缺失用量不补造，费用不完整时不算每次成功费用。",
            "重复试验不是独立题目，不据此生成置信区间；小样本只用于探索。",
            "逐题配对使用共同重复编号；缺少记录与人工未判结果会减少可比较范围。",
            "摘要与末尾图 token 只汇总实际观测样本，缺失不补零；近似 token 不是提供商计费值。",
            "eligible_for_comparison 排除已中止、外部条件失效和无模型自检，不代表统计充分或因果有效。",
            *manifest.get("notes", []),
        ],
    }
    results_root.mkdir(parents=True, exist_ok=True)
    (results_root / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (results_root / "report.md").write_text(_markdown(report), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="汇总 SAYACODE 真实任务评估结果")
    parser.add_argument("results_root", type=Path, help="包含 manifest 和 trial 的结果目录")
    parser.add_argument("--expected-repeats", type=int, help="覆盖 manifest 中的预期重复数")
    args = parser.parse_args()
    try:
        report = generate_report(args.results_root, expected_repeats=args.expected_repeats)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"无法生成报告：{exc}\n")
    print(f"已汇总 {sum(g['trials'] for g in report['groups'].values())} 次试验。")
    print(f"报告：{args.results_root / 'report.md'}")


if __name__ == "__main__":
    main()
