"""调度模式的子智能体工厂。

每个子智能体是完整的执行图，由调度层调度。
子智能体不持久化会话与记忆，结果直接从调度图状态读取。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

from langchain.agents import create_agent

from ..prompts import DEFAULT_PROMPT_STYLE, get_prompt_by_style


def _mode_for_agent_type(agent_type: str) -> str:
    """agent_type → agent mode（团队共享的唯一实现）。"""
    normalized = str(agent_type).lower()
    if any(token in normalized for token in ("plan", "architect", "research")):
        return "plan"
    if any(token in normalized for token in ("review", "audit", "inspect")):
        return "review"
    return "build"


def build_team_agent(
    *,
    model: Any,
    workspace: Path,
    agent_type: str,
    runtime: Any,
    tools: List[Any],
    checkpointer: Any = None,
):
    """为调度层构建一个子智能体。

    复用主智能体的中间件链，但跳过外部工具与会话记忆，无持久化。
    传检查点时线程可恢复，供追问复用同一会话。
    """
    from ..core.middleware import (
        SayaHookMiddleware,
        SayaPermissionMiddleware,
        SayaPromptMiddleware,
        SayaSafetyMiddleware,
    )

    agent_mode = _mode_for_agent_type(agent_type)
    system_prompt = get_prompt_by_style(
        style=DEFAULT_PROMPT_STYLE,
        agent_name=agent_type.upper(),
        workspace=str(workspace),
        project_summary="",
        agent_mode=agent_mode,
    )

    middleware: List[Any] = [SayaHookMiddleware()]
    if runtime is not None and getattr(runtime, "permissions", None) is not None:
        middleware.append(SayaPermissionMiddleware(runtime.permissions))
    # 说明 Safety 中间件无参，判据走原语，不读运行时。
    # 补充旧草稿传参属于笔误，签名应为无参构造。
    middleware.append(SayaSafetyMiddleware())
    prompt_mw = SayaPromptMiddleware()
    # 明确 system prompt 归中间件所有，避免两处打架。
    # 补充说明 AgentRunner 同理，刷新后不再传静态值。
    prompt_mw.refresh(system_prompt)
    middleware.append(prompt_mw)

    kwargs: dict = {"middleware": middleware, "name": agent_type}
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer
    return create_agent(
        model,
        tools,
        **kwargs,
    )


__all__ = ["build_team_agent", "_mode_for_agent_type"]
