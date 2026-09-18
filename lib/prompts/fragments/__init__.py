"""微模块化提示词片段包入口。

职责是汇出行为层与人格层 fragment 构造器：每个构造器都是纯函数，
返回 str 片段。核心函数为 `build_base_profile` / `build_task_playbook` /
`build_personality_overlay` 等，由 `system_prompt` 按模式组装调用。
"""

from .base_profile import build_base_profile
from .task_playbook import build_task_playbook
from .security_rules import build_security_rules
from .communication_style import build_communication_style
from .tool_descriptions import build_tool_descriptions
from .code_generation import build_code_generation_rules
from .plan_execute import build_plan_execute_overlay
from .personality_overlay import build_personality_overlay

__all__ = [
    "build_base_profile",
    "build_task_playbook",
    "build_security_rules",
    "build_communication_style",
    "build_tool_descriptions",
    "build_code_generation_rules",
    "build_plan_execute_overlay",
    "build_personality_overlay",
]
