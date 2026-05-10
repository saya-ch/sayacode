"""
System Reminder 机制 v2。

根据运行时状态生成上下文提醒，注入到系统消息中。
纯文本拼接，无 I/O，无 API 调用。

新增：压缩紧急度分级、恢复状态提醒。
"""

from typing import Any, Dict, List, Optional


def get_system_reminders(state: Optional[Dict[str, Any]] = None) -> str:
    """根据运行时状态返回系统提醒内容。

    每个提醒是一个独立的检查条件。如果不需要提醒，返回空字符串。

    Args:
        state: 可选状态字典，支持的键：
            - agent_mode: "build" | "plan" | "review"
            - context_usage: float (0.0-1.0) 上下文使用比例
            - turn_count: int 当前轮次计数
            - language: str 用户语言偏好

    Returns:
        格式化的系统提醒文本，追加到系统消息末尾。
        无提醒需要时返回空字符串。
    """
    if not state:
        return ""

    reminders: List[str] = []

    # 模式提醒
    mode = state.get("agent_mode", "")
    if mode == "plan":
        reminders.append(
            "**Plan 模式**：只读规划。不要写文件、不执行 Shell 命令、不做 Git 变更。"
            "可以读取、搜索、分析并给出实施计划。"
        )
    elif mode == "review":
        reminders.append(
            "**Review 模式**：只读审查。不要写文件、不执行 Shell 命令、不做 Git 变更。"
            "发现问题优先，按严重度排序，给出文件/行号/影响/修复建议。"
        )

    # 上下文使用率提醒（分层分级）
    usage = state.get("context_usage", 0.0)
    if isinstance(usage, (int, float)):
        if usage > 0.85:
            usage_pct = int(usage * 100)
            reminders.append(
                f"上下文使用率 {usage_pct}%（紧急）。"
                "尽快压缩上下文：移除不再需要的文件引用，精简历史对话。"
                "如果压缩后仍不够，减少本轮的工具调用数量。"
            )
        elif usage > 0.70:
            usage_pct = int(usage * 100)
            reminders.append(
                f"上下文使用率 {usage_pct}%（偏高）。"
                "优先使用 search_replace 做精确编辑，避免大段重写。"
                "不需要的文件不要 read。"
            )

    # 语言一致性提醒
    language = state.get("language", "")
    if language == "zh-CN":
        reminders.append("用户使用中文，用中文回复。")

    if not reminders:
        return ""

    return "\n\n".join(f"- {r}" for r in reminders)


__all__ = ["get_system_reminders"]
