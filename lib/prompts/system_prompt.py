"""
系统提示词模块。

定义 SAIAgent 的系统提示词，包括：
- 角色设定
- 能力描述
- 安全准则
- 上下文记忆使用方式
- 项目理解方式
- 可切换的人格/语言风格

提示词采用微模块化架构：每个行为约束是独立的 fragment 函数，
通过 get_system_prompt() 组合为完整提示词。
"""

from __future__ import annotations

import re
from typing import Optional

from .fragments.base_profile import build_base_profile
from .fragments.task_playbook import build_task_playbook
from .fragments.security_rules import build_security_rules
from .fragments.communication_style import build_communication_style
from .fragments.tool_descriptions import build_tool_descriptions
from .fragments.code_generation import build_code_generation_rules
from .fragments.personality_overlay import build_personality_overlay
from .fragments.mode_subagents import build_plan_mode_prompt, build_review_mode_prompt


SUPPORTED_PROMPT_STYLES = (
    "standard",
    "concise",
    "tsundere",
    "genki",
    "mesugaki",
    "onee-san",
    "idol",
    "catgirl",
    "mukuchi",
)

PROMPT_STYLE_LABELS = {
    "standard": "标准",
    "concise": "简洁",
    "tsundere": "傲娇",
    "genki": "元气",
    "mesugaki": "雌小鬼",
    "onee-san": "姐姐",
    "idol": "偶像",
    "catgirl": "猫娘",
    "mukuchi": "无口",
}

PROMPT_STYLE_ALIASES = {
    "standard": "standard",
    "default": "standard",
    "normal": "standard",
    "professional": "standard",
    "默认": "standard",
    "标准": "standard",
    "专业": "standard",
    "concise": "concise",
    "brief": "concise",
    "short": "concise",
    "简洁": "concise",
    "精简": "concise",
    "tsundere": "tsundere",
    "roast": "tsundere",
    "吐槽": "tsundere",
    "傲娇": "tsundere",
    "娇蛮": "tsundere",
    "genki": "genki",
    "energetic": "genki",
    "元气": "genki",
    "元气少女": "genki",
    "活力": "genki",
    "活力少女": "genki",
    "活力美少女": "genki",
    "mesugaki": "mesugaki",
    "bratty": "mesugaki",
    "teasing": "mesugaki",
    "雌小鬼": "mesugaki",
    "调皮": "mesugaki",
    "坏笑": "mesugaki",
    "小恶魔": "mesugaki",
    "onee-san": "onee-san",
    "oneesan": "onee-san",
    "onee": "onee-san",
    "姐姐": "onee-san",
    "姐姐系": "onee-san",
    "御姐": "onee-san",
    "大姐姐": "onee-san",
    "学姐": "onee-san",
    "idol": "idol",
    "偶像": "idol",
    "爱豆": "idol",
    "元气偶像": "idol",
    "catgirl": "catgirl",
    "neko": "catgirl",
    "nekomimi": "catgirl",
    "猫娘": "catgirl",
    "猫耳": "catgirl",
    "喵娘": "catgirl",
    "mukuchi": "mukuchi",
    "silent": "mukuchi",
    "mute": "mukuchi",
    "kuudere": "mukuchi",
    "cool": "mukuchi",
    "无口": "mukuchi",
    "無口": "mukuchi",
    "沉默": "mukuchi",
    "寡言": "mukuchi",
    "冷淡": "mukuchi",
    "冷静": "mukuchi",
    "高冷": "mukuchi",
}


# ==============================================================================
# 核心系统提示词（行为层：始终加载）
# ==============================================================================

def get_system_prompt(
    agent_name: str = "SAYA",
    workspace: Optional[str] = None,
    project_summary: Optional[str] = None,
    agent_mode: str = "build",
) -> str:
    """
    获取系统提示词（微模块化组合）。

    行为层 fragment 按顺序组合：
    1. 角色身份 (base_profile)
    2. 任务执行模式 (task_playbook)
    3. 通信风格 (communication_style)
    4. 工具描述 (tool_descriptions)
    5. 安全规则 (security_rules)
    6. 代码生成准则 (code_generation)

    模式层根据 agent_mode 条件加载：
    - plan → 详细 Plan 模式子 Agent 提示词
    - review → 详细 Review 模式子 Agent 提示词
    - build → 不加载额外模式提示词

    Args:
        agent_name: Agent 名称
        workspace: 工作区路径
        project_summary: 项目摘要
        agent_mode: Agent 工作模式（build/plan/review）

    Returns:
        格式化的系统提示词
    """
    sections = [
        build_base_profile(agent_name),
        build_task_playbook(),
        build_communication_style(),
        build_tool_descriptions(),
        build_security_rules(),
        build_code_generation_rules(),
    ]

    # 模式层：条件加载（行为-人格两层架构）
    mode = (agent_mode or "build").lower()
    if mode == "plan":
        sections.append(build_plan_mode_prompt())
    elif mode == "review":
        sections.append(build_review_mode_prompt())

    # 动态上下文段
    if project_summary:
        sections.append(f"### 当前项目\n{project_summary}")
    if workspace:
        sections.append(f"### 工作区\n当前工作区: {workspace}")

    # 工作环境段
    sections.append("""## 工作环境

### 项目感知
你能访问当前工作区的文件系统、Git 历史和项目结构。通过工具可以读取、搜索、编辑文件并执行命令。项目上下文和文件状态会在对话中动态更新。

### 记忆与持久化
跨轮对话的上下文会自动保留（包括修改过的文件和之前的决策）。不需要用户重复说明已经告知过的偏好和约束。""")

    prompt = "\n\n".join(sections)
    return prompt


# ==============================================================================
# 各人格风格的系统提示词（行为层 + 人格层叠加）
# ==============================================================================

def get_tsundere_prompt(
    agent_name: str = "SAYA",
    workspace: Optional[str] = None,
    project_summary: Optional[str] = None,
    agent_mode: str = "build",
) -> str:
    """获取傲娇风格提示词。"""
    base = get_system_prompt(
        agent_name=agent_name,
        workspace=workspace,
        project_summary=project_summary,
        agent_mode=agent_mode,
    )
    overlay = build_personality_overlay("tsundere", agent_name)
    if overlay:
        return base + "\n\n" + overlay
    return base


def get_concise_prompt(
    agent_name: str = "SAYA",
    workspace: Optional[str] = None,
    project_summary: Optional[str] = None,
    agent_mode: str = "build",
) -> str:
    """获取简洁版本提示词 — 保留行为核心但裁剪冗长描述。"""
    lines = [
        f"你是 {agent_name}，一个编程助手。",
        "",
        "能力：文件操作、代码编辑、Git 操作、项目分析、命令执行。",
        "准则：安全优先、简洁准确、主动帮助、需要落盘时直接修改工作区文件。",
        "输出：少废话，先给结果，再补必要说明。不要叙述内部思考过程。",
        "任务：修 bug 先报根因；做实现先给完成状态；做 review 先列问题。",
        "工具：多个独立只读操作并行执行；读后改、改后验；不用 Shell 替代专用工具。",
    ]
    if project_summary:
        lines.extend(["", f"项目：{project_summary}"])
    if workspace:
        lines.extend(["", f"工作区：{workspace}"])
    # 注入安全规则（即使是简洁模式也必须包含）
    lines.extend([
        "",
        build_security_rules(),
    ])
    return "\n".join(lines)


def get_genki_prompt(
    agent_name: str = "SAYA",
    workspace: Optional[str] = None,
    project_summary: Optional[str] = None,
    agent_mode: str = "build",
) -> str:
    """获取元气活力风格提示词。"""
    base = get_system_prompt(
        agent_name=agent_name,
        workspace=workspace,
        project_summary=project_summary,
        agent_mode=agent_mode,
    )
    overlay = build_personality_overlay("genki", agent_name)
    if overlay:
        return base + "\n\n" + overlay
    return base


def get_mesugaki_prompt(
    agent_name: str = "SAYA",
    workspace: Optional[str] = None,
    project_summary: Optional[str] = None,
    agent_mode: str = "build",
) -> str:
    """获取调皮坏笑风格提示词。"""
    base = get_system_prompt(
        agent_name=agent_name,
        workspace=workspace,
        project_summary=project_summary,
        agent_mode=agent_mode,
    )
    overlay = build_personality_overlay("mesugaki", agent_name)
    if overlay:
        return base + "\n\n" + overlay
    return base


def get_onee_san_prompt(
    agent_name: str = "SAYA",
    workspace: Optional[str] = None,
    project_summary: Optional[str] = None,
    agent_mode: str = "build",
) -> str:
    """获取姐姐系风格提示词。"""
    base = get_system_prompt(
        agent_name=agent_name,
        workspace=workspace,
        project_summary=project_summary,
        agent_mode=agent_mode,
    )
    overlay = build_personality_overlay("onee-san", agent_name)
    if overlay:
        return base + "\n\n" + overlay
    return base


def get_idol_prompt(
    agent_name: str = "SAYA",
    workspace: Optional[str] = None,
    project_summary: Optional[str] = None,
    agent_mode: str = "build",
) -> str:
    """获取偶像风格提示词。"""
    base = get_system_prompt(
        agent_name=agent_name,
        workspace=workspace,
        project_summary=project_summary,
        agent_mode=agent_mode,
    )
    overlay = build_personality_overlay("idol", agent_name)
    if overlay:
        return base + "\n\n" + overlay
    return base


def get_catgirl_prompt(
    agent_name: str = "SAYA",
    workspace: Optional[str] = None,
    project_summary: Optional[str] = None,
    agent_mode: str = "build",
) -> str:
    """获取猫娘风格提示词。"""
    base = get_system_prompt(
        agent_name=agent_name,
        workspace=workspace,
        project_summary=project_summary,
        agent_mode=agent_mode,
    )
    overlay = build_personality_overlay("catgirl", agent_name)
    if overlay:
        return base + "\n\n" + overlay
    return base


def get_mukuchi_prompt(
    agent_name: str = "SAYA",
    workspace: Optional[str] = None,
    project_summary: Optional[str] = None,
    agent_mode: str = "build",
) -> str:
    """获取无口风格提示词。"""
    base = get_system_prompt(
        agent_name=agent_name,
        workspace=workspace,
        project_summary=project_summary,
        agent_mode=agent_mode,
    )
    overlay = build_personality_overlay("mukuchi", agent_name)
    if overlay:
        return base + "\n\n" + overlay
    return base


# ==============================================================================
# 快捷函数
# ==============================================================================

def get_prompt_by_style(
    style: str = "standard",
    **kwargs
) -> str:
    """
    根据风格获取提示词

    Args:
        style: 风格 (standard, concise, tsundere, genki, mesugaki, onee-san, idol, catgirl, mukuchi)
        **kwargs: 其他参数传递给 get_system_prompt

    Returns:
        系统提示词
    """
    styles = {
        "standard": get_system_prompt,
        "concise": get_concise_prompt,
        "tsundere": get_tsundere_prompt,
        "genki": get_genki_prompt,
        "mesugaki": get_mesugaki_prompt,
        "onee-san": get_onee_san_prompt,
        "idol": get_idol_prompt,
        "catgirl": get_catgirl_prompt,
        "mukuchi": get_mukuchi_prompt,
    }

    canonical_style = normalize_prompt_style(style)
    func = styles.get(canonical_style or "standard", get_system_prompt)
    return func(**kwargs)


# ==============================================================================
# 辅助函数
# ==============================================================================

def normalize_prompt_style(value: Optional[str], fallback: Optional[str] = "standard") -> Optional[str]:
    """将用户输入的风格名规范化为系统支持的 canonical style。"""
    if value is None:
        return fallback

    raw = str(value).strip()
    if not raw:
        return fallback

    normalized = raw.lower().replace("_", "-")
    normalized = re.sub(r"[\s/|+]+", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip("-")

    if normalized in PROMPT_STYLE_ALIASES:
        return PROMPT_STYLE_ALIASES[normalized]
    if raw in PROMPT_STYLE_ALIASES:
        return PROMPT_STYLE_ALIASES[raw]
    if raw.lower() in PROMPT_STYLE_ALIASES:
        return PROMPT_STYLE_ALIASES[raw.lower()]

    return fallback


def prompt_style_label(style: Optional[str]) -> str:
    """返回适合展示的 prompt style 标签。"""
    normalized = normalize_prompt_style(style)
    if not normalized:
        return PROMPT_STYLE_LABELS["standard"]
    return PROMPT_STYLE_LABELS.get(normalized, normalized)


def list_prompt_styles() -> tuple[str, ...]:
    """返回支持的 canonical prompt style 列表。"""
    return SUPPORTED_PROMPT_STYLES


# ==============================================================================
# 导出
# ==============================================================================

__all__ = [
    'SUPPORTED_PROMPT_STYLES',
    'PROMPT_STYLE_LABELS',
    'normalize_prompt_style',
    'prompt_style_label',
    'list_prompt_styles',
    'get_system_prompt',
    'get_tsundere_prompt',
    'get_concise_prompt',
    'get_genki_prompt',
    'get_mesugaki_prompt',
    'get_onee_san_prompt',
    'get_idol_prompt',
    'get_catgirl_prompt',
    'get_mukuchi_prompt',
    'get_prompt_by_style',
]
