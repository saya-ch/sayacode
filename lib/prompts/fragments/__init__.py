"""微模块化提示词片段。

每个片段都是可独立测试、可组合的纯函数，返回 str。
"""

from .base_profile import build_base_profile
from .task_playbook import build_task_playbook
from .security_rules import build_security_rules
from .communication_style import build_communication_style
from .tool_descriptions import build_tool_descriptions
from .code_generation import build_code_generation_rules
from .personality_overlay import build_personality_overlay

__all__ = [
    "build_base_profile",
    "build_task_playbook",
    "build_security_rules",
    "build_communication_style",
    "build_tool_descriptions",
    "build_code_generation_rules",
    "build_personality_overlay",
]
