"""工具结果 artifact 的契约：声明的形状与校验。

LangChain 的 ``ToolMessage.artifact`` 承载「不进模型视野的结构化产物」。
这里声明我们实际写入的形状，让生产方与消费方（审计、``/trace``）对齐，
并把「形状不对」变成可见的告警，而不是被静默忽略。
"""

from __future__ import annotations

from typing import Any, Dict, List

# 说明 outcome 取值：ok 正常，denied 拒绝，spilled 落盘。
OUTCOMES = ("ok", "denied", "spilled")

# 字段 → 期望类型。未列出的字段是扩展字段，不参与校验（向后兼容）。
ARTIFACT_SCHEMA = {
    "tool": str,
    "outcome": str,
    "chars": int,
    "spill_path": str,
}


def build_tool_artifact(tool: str, outcome: str, **extra: Any) -> Dict[str, Any]:
    """构造符合契约的 artifact：值为 None 的字段不写入。"""
    artifact: Dict[str, Any] = {"tool": str(tool), "outcome": str(outcome)}
    for key, value in extra.items():
        if value is not None:
            artifact[key] = value
    return artifact


def validate_tool_artifact(artifact: Any) -> List[str]:
    """返回违反契约的说明列表；空列表代表合规。

    空 artifact（``None`` / ``{}``）是合法的：多数工具没有结构化产物。
    """
    if artifact is None or artifact == {}:
        return []
    if not isinstance(artifact, dict):
        return ["artifact 必须是 dict，实际是 " + type(artifact).__name__]

    problems: List[str] = []
    for field, expected in ARTIFACT_SCHEMA.items():
        if field not in artifact:
            continue
        value = artifact[field]
        if value is None:
            continue
        if expected is int and isinstance(value, bool):
            problems.append(f"{field} 期望 int，实际是 bool")
        elif not isinstance(value, expected):
            problems.append(
                f"{field} 期望 {expected.__name__}，实际是 {type(value).__name__}"
            )

    outcome = artifact.get("outcome")
    if outcome is not None and outcome not in OUTCOMES:
        problems.append(f"outcome 取值不在 {OUTCOMES}：{outcome!r}")
    return problems


__all__ = [
    "ARTIFACT_SCHEMA",
    "OUTCOMES",
    "build_tool_artifact",
    "validate_tool_artifact",
]
