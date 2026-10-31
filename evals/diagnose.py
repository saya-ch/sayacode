"""从原生审计解释失败与耗时，不改写评分结果或重跑模型。"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


def classify(result: dict[str, Any], audit: list[dict[str, Any]]) -> str:
    """使用原生异常类型及判分事实，无法定位的原因保持未分类。"""
    status = result["status"]
    if status in {"timeout", "infrastructure_error"}:
        return str(status)
    if status != "completed":
        root_events = [
            row
            for row in audit
            if row.get("thread_id") == "eval-main"
            and row.get("event") in {"model.completed", "model.failed"}
        ]
        if root_events and root_events[-1]["event"] == "model.failed":
            details = root_events[-1].get("details") or {}
            if details.get("status_code") == 429:
                return "model_rate_limit"
            error = details.get("error_type", "")
            if error in {
                "APIConnectionError",
                "OpenAIConnectionError",
                "ConnectError",
                "ConnectTimeout",
                "ReadTimeout",
                "APITimeoutError",
            }:
                return "model_connection"
            return "model_failure"
        return "runtime_failure_unclassified"
    if result.get("passed") is None:
        return "human_review_pending"
    if result.get("passed"):
        return "accepted"
    failed = [row for row in result.get("checks", []) if not row.get("passed")]
    if failed and all(row.get("type") == "protected_file" for row in failed):
        return "protected_file_constraint"
    return "acceptance_failure"


def _model_seconds(audit: list[dict[str, Any]]) -> float | None:
    """合并模型调用时间区间，避免多 Agent 并发时累加出超过墙钟的比例。"""
    intervals = []
    for row in audit:
        details = row.get("details") or {}
        if row.get("event") not in {"model.completed", "model.failed"}:
            continue
        duration = details.get("duration_ms")
        if not isinstance(duration, (int, float)) or not row.get("at"):
            continue
        end = datetime.fromisoformat(row["at"]).timestamp()
        intervals.append((end - duration / 1000, end))
    if not intervals:
        return None
    total = 0.0
    start, end = sorted(intervals)[0]
    for left, right in sorted(intervals)[1:]:
        if left > end:
            total += end - start
            start, end = left, right
        else:
            end = max(end, right)
    return round(total + end - start, 3)


def diagnose(root: Path) -> dict[str, Any]:
    """仅产生附加诊断，保留原始 result.json 不变。"""
    trials = []
    for file in sorted(root.rglob("result.json")):
        result = json.loads(file.read_text(encoding="utf-8"))
        path = file.parent / "state" / "agent" / "audit.jsonl"
        audit = []
        if path.is_file():
            for line in path.read_bytes().splitlines():
                try:
                    audit.append(json.loads(line.decode("utf-8-sig")))
                except (ValueError, UnicodeError):
                    continue
        errors = Counter(
            (row.get("details") or {}).get("error_type", "unknown")
            for row in audit
            if row.get("event") == "model.failed"
        )
        trials.append(
            {
                "case_id": result["case_id"],
                "variant": result["variant"],
                "repeat": result["repeat"],
                "classification": classify(result, audit),
                "model_error_types": dict(errors),
                "model_time_union_seconds": _model_seconds(audit),
                "duration_seconds": result["duration_seconds"],
                "task_count": result.get("task_count", 0),
                "summary_count": result.get("summary_count"),
                "context_history": result.get("context_history", []),
            }
        )
    data = {
        "counts": dict(Counter(row["classification"] for row in trials)),
        "trials": trials,
        "notes": [
            "异常类型只能定位到已观察到的失败层，不证明具体上游或网络根因。",
            "回调层模型调用不等于供应商实际 HTTP 尝试次数；SDK 内部重试可能不可见。",
            "模型时间使用已结束调用区间的并集；缺失的在途请求不补造时长。",
            "这是附加诊断，不修改验收结果，不删除失败或补跑挑选最好结果。",
        ],
    }
    (root / "diagnostics.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    print(json.dumps(diagnose(parser.parse_args().directory)["counts"], ensure_ascii=False))
