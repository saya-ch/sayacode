"""Supervisor 模式的子 agent 工厂。

每个子 agent 是完整的 ReAct 图（``create_agent`` + 中间件），
由 ``langgraph_supervisor`` 调度。子 agent 不持久化 session/memory
（headless 执行），结果直接从 supervisor 图 state 读取。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

from langchain.agents import create_agent

from ..prompts import get_prompt_by_style


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
    """为 supervisor 构建一个子 agent（编译好的 ReAct 图）。

    复用主 agent 的中间件链（Hook → Permission → Safety → Prompt），
    但跳过 MCP / session / memory（headless 执行，无持久化）。
    传 ``checkpointer`` 时 thread 可恢复，供追问复用同一会话。
    """
    from ..core.middleware import (
        SayaHookMiddleware,
        SayaPermissionMiddleware,
        SayaPromptMiddleware,
        SayaSafetyMiddleware,
    )

    agent_mode = _mode_for_agent_type(agent_type)
    system_prompt = get_prompt_by_style(
        style="standard",
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
