"""Agent operating modes and permission presets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .permissions import (
    MUTATING_TOOLS,
    PermissionAction,
    set_session_permission_rules,
)


SUPPORTED_AGENT_MODES = ("build", "plan", "review")

AGENT_MODE_ALIASES = {
    "build": "build",
    "default": "build",
    "work": "build",
    "edit": "build",
    "implement": "build",
    "开发": "build",
    "实现": "build",
    "构建": "build",
    "plan": "plan",
    "planning": "plan",
    "readonly": "plan",
    "read-only": "plan",
    "read_only": "plan",
    "设计": "plan",
    "规划": "plan",
    "计划": "plan",
    "只读": "plan",
    "review": "review",
    "audit": "review",
    "inspect": "review",
    "审查": "review",
    "检查": "review",
    "评审": "review",
}

AGENT_MODE_LABELS = {
    "build": "构建",
    "plan": "规划",
    "review": "审查",
}


@dataclass(frozen=True)
class AgentMode:
    """One runtime mode."""

    name: str
    label: str
    description: str
    permission_rules: Dict[str, PermissionAction]
    prompt_overlay: str


MUTATION_DENY_RULES: Dict[str, PermissionAction] = {
    **{name: "deny" for name in MUTATING_TOOLS},
    "mcp_*": "deny",
}

AGENT_MODES: Dict[str, AgentMode] = {
    "build": AgentMode(
        name="build",
        label=AGENT_MODE_LABELS["build"],
        description="默认实现模式。允许按权限策略申请写文件、执行命令和 Git 变更。",
        permission_rules={},
        prompt_overlay="""## 当前工作模式：Build

- 可以在权限策略允许或用户确认后修改文件、运行命令、执行 Git 变更。
- 用户要求实现、修复、重构、生成文件时，默认直接推进并验证。
- 修改前保持最小必要上下文，修改后说明关键文件和验证结果。
""",
    ),
    "plan": AgentMode(
        name="plan",
        label=AGENT_MODE_LABELS["plan"],
        description="只读规划模式。禁止写文件、删文件、执行 shell 和 Git 变更。",
        permission_rules=MUTATION_DENY_RULES,
        prompt_overlay="""## 当前工作模式：Plan

- 这是只读规划模式。不要写文件、不要删除文件、不要执行 shell 命令、不要执行 Git 变更。
- 可以读取文件、搜索代码、分析项目并给出计划。
- 输出应包含目标拆解、风险、建议改动文件、验证方案和需要用户确认的取舍。
- 如果用户要求直接修改，先说明当前是 Plan 模式，并提示切换到 `/mode build`。
""",
    ),
    "review": AgentMode(
        name="review",
        label=AGENT_MODE_LABELS["review"],
        description="只读审查模式。聚焦 bug、风险、回归和测试缺口，禁止变更工作区。",
        permission_rules=MUTATION_DENY_RULES,
        prompt_overlay="""## 当前工作模式：Review

- 这是只读审查模式。不要写文件、不要删除文件、不要执行 shell 命令、不要执行 Git 变更。
- 以代码审查姿态工作：发现问题优先，按严重度排序，给出文件/位置/影响/修复建议。
- 不要先写泛泛总结。没有明确问题时直接说明未发现明确问题，并补残余风险。
- 如果用户要求直接修改，先说明当前是 Review 模式，并提示切换到 `/mode build`。
""",
    ),
}


def normalize_agent_mode(value: Optional[str], fallback: Optional[str] = "build") -> Optional[str]:
    """Normalize a user mode value."""
    if value is None:
        return fallback
    raw = str(value).strip()
    if not raw:
        return fallback
    normalized = raw.lower().replace("_", "-")
    return AGENT_MODE_ALIASES.get(normalized) or AGENT_MODE_ALIASES.get(raw) or fallback


def get_agent_mode(mode: Optional[str]) -> AgentMode:
    """Return the mode definition."""
    normalized = normalize_agent_mode(mode)
    return AGENT_MODES[normalized or "build"]


def agent_mode_label(mode: Optional[str]) -> str:
    """Return display label for mode."""
    normalized = normalize_agent_mode(mode)
    return AGENT_MODE_LABELS.get(normalized or "build", normalized or "build")


def list_agent_modes() -> tuple[str, ...]:
    """Return supported canonical mode names."""
    return SUPPORTED_AGENT_MODES


def apply_agent_mode_permissions(mode: Optional[str]) -> AgentMode:
    """Apply in-memory permission overrides for mode."""
    definition = get_agent_mode(mode)
    set_session_permission_rules(definition.permission_rules, source=f"mode:{definition.name}")
    return definition


def get_agent_mode_prompt_overlay(mode: Optional[str]) -> str:
    """Return prompt overlay for mode."""
    return get_agent_mode(mode).prompt_overlay


def render_agent_mode_summary(active_mode: Optional[str]) -> str:
    """Render mode list for CLI display."""
    lines = [
        "Agent Modes",
        f"Active: {normalize_agent_mode(active_mode)} ({agent_mode_label(active_mode)})",
        "",
    ]
    for mode in SUPPORTED_AGENT_MODES:
        definition = AGENT_MODES[mode]
        lines.append(f"- {definition.name}: {definition.label} - {definition.description}")
    return "\n".join(lines)


__all__ = [
    "AGENT_MODES",
    "AgentMode",
    "agent_mode_label",
    "apply_agent_mode_permissions",
    "get_agent_mode",
    "get_agent_mode_prompt_overlay",
    "list_agent_modes",
    "normalize_agent_mode",
    "render_agent_mode_summary",
]
