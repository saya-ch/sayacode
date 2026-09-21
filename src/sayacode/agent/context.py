"""注入图运行和工具调用的上下文。

上下文随每次调用传入，句柄是工厂编译结果。
两者都不存对话历史，历史归图框架所有。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Profile


@dataclass(frozen=True, slots=True)
class AgentContext:
    """单次运行的依赖，由工具运行时注入。

    参数是工作区加信任等级加策略加输出目录加会话标识，附带任务和画像等可选信息。
    返回值是只读数据对象，调用方直接读字段即可。
    坑点是实例不可变，通知字段只影响当次模型调用，不写入持久状态。"""

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
    """编译好的图，连同模型和摘要配置一起持有。

    参数是图对象加画像加模型加工作区，运行时按句柄找到执行入口。
    返回值同样是只读持有器，不负责打开关闭资源。
    坑点是图内已固化中间件顺序，换画像或换工具必须重建句柄。"""

    graph: Any
    profile: Profile
    model: Any
    workspace: Path
