"""注入图运行和工具调用的上下文。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Profile


@dataclass(frozen=True, slots=True)
class AgentContext:
    """单次运行的依赖。由工具运行时注入。"""

    workspace: Path
    trust_level: str
    policy: Any
    output_dir: Path
    session_id: str
    task_id: str | None = None
    profile_name: str | None = None
    is_background: bool = False
    output_limit_bytes: int = 64 * 1024
    task_notification: str | None = None


@dataclass(frozen=True, slots=True)
class AgentHandle:
    """编译好的图。连同模型和摘要配置一起持有。"""

    graph: Any
    profile: Profile
    model: Any
    workspace: Path
