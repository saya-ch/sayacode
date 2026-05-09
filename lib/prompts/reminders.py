"""
System Reminder 机制。

根据运行时状态生成上下文提醒，注入到系统消息中。
纯文本拼接，无 I/O，无 API 调用。
"""

from typing import Any, Dict, List, Optional


def get_system_reminders(state: Optional[Dict[str, Any]] = None) -> str:
    """根据运行时状态返回系统提醒内容。

    每个提醒是一个独立的检查条件。如果不需要提醒，返回空字符串。

    Args:
        state: 可选状态字典，支持的键：
            - agent_mode: "build" | "plan" | "review"
            - context_usage: float (0.0-1.0) 上下文使用比例
            - turn_count: int 当前轮次计数（未来扩展）
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
            "当前处于 **Plan 模式**：这是只读规划模式。"
            "不要写文件、不要删除文件、不要执行 Shell 命令、不要做 Git 变更。"
            "可以读取文件、搜索代码、分析项目并给出实施计划。"
        )
    elif mode == "review":
        reminders.append(
            "当前处于 **Review 模式**：这是只读审查模式。"
            "不要写文件、不要删除文件、不要执行 Shell 命令、不要做 Git 变更。"
            "以代码审查姿态工作：发现问题优先，按严重度排序，给出文件/位置/影响/修复建议。"
        )

    # 上下文使用率提醒
    usage = state.get("context_usage", 0.0)
    if isinstance(usage, (int, float)) and usage > 0.7:
        usage_pct = int(usage * 100)
        reminders.append(
            f"上下文使用率约为 {usage_pct}%。"
            "如果对话很长，考虑使用 /compact 压缩历史以释放上下文窗口。"
        )

    # 语言一致性提醒
    language = state.get("language", "")
    if language == "zh-CN":
        reminders.append("用户使用中文交流，请用中文回复。")

    if not reminders:
        return ""

    return "\n\n".join(f"- {r}" for r in reminders)


__all__ = ["get_system_reminders"]
