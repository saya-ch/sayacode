"""子 Agent 委托工具：把可隔离的任务块交给子 Agent 执行。

``delegate_to_subagent`` 让模型自主决定是否分工：适合并行推进、需要隔离
工作区、或适合只读复核的子任务。执行走 TeamSupervisor 的同步 spawn→结果
链（builder 自动进隔离 worktree），与 /team 同一底座。
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


DELEGATE_TIMEOUT = 300.0
MAX_DELEGATE_CHARS = 20_000


class DelegateInput(BaseModel):
    """delegate_to_subagent 的输入。"""

    task: str = Field(description="交给子 Agent 的完整任务描述（含目标、范围、验收标准）")
    agent_type: str = Field(default="builder", description="子 Agent 类型：builder（实现，需隔离）/ planner（只读规划）/ reviewer（只读审查）")


def create_delegate_tool(spawn_fn: Callable[[str, str], str]) -> StructuredTool:
    """创建委托工具。``spawn_fn(task, agent_type)`` 返回子 Agent 结果文本。"""

    def delegate_to_subagent(task: str, agent_type: str = "builder") -> str:
        # 校验任务非空，拦截超长输入。
        task = str(task or "").strip()
        if not task:
            return "委托失败：任务描述不能为空"
        if len(task) > 20_000:
            return "委托失败：任务描述不能超过 20000 个字符"
        agent_type = str(agent_type or "builder").strip().lower() or "builder"
        try:
            result = spawn_fn(task, agent_type)
        except Exception as exc:
            return "子 Agent 执行失败：" + str(exc)
        result = str(result or "")
        # 截断超长结果，避免撑爆上下文。
        if len(result) > MAX_DELEGATE_CHARS:
            result = result[:MAX_DELEGATE_CHARS] + chr(10) + "……（结果过长已截断）"
        return result or "子 Agent 无返回"

    return StructuredTool.from_function(
        func=delegate_to_subagent,
        name="delegate_to_subagent",
        description="把可隔离的子任务委托给子 Agent（builder 实现 / planner 规划 / reviewer 审查），返回其结果。适合并行块与只读复核（可一次委托多个并行推进）；简单任务自己做，不要委托。",
        args_schema=DelegateInput,
    )


class DelegateResumeInput(BaseModel):
    """delegate_resume 的输入。"""

    handle_id: str = Field(description="此前委托返回的 worker id")
    follow_up: str = Field(description="追问内容（补充约束、要求返工或深入一层）")


def create_sync_resume_tool(resume_text_fn: Callable[[str, str], str]) -> StructuredTool:
    """创建同步追问工具：复用同一子 Agent 会话续跑，直接返回新结果。"""

    def delegate_resume(handle_id: str, follow_up: str) -> str:
        handle = str(handle_id or "").strip()
        follow_up = str(follow_up or "").strip()
        if not handle:
            return "追问失败：worker id 不能为空"
        if not follow_up:
            return "追问失败：追问内容不能为空"
        try:
            result = resume_text_fn(handle, follow_up)
        except (KeyError, ValueError, RuntimeError) as exc:
            return "追问失败：" + str(exc)
        result = str(result or "")
        if len(result) > MAX_DELEGATE_CHARS:
            result = result[:MAX_DELEGATE_CHARS] + chr(10) + "……（结果过长已截断）"
        return result or "子 Agent 无返回"

    return StructuredTool.from_function(
        func=delegate_resume,
        name="delegate_resume",
        description="追问此前委托的子 Agent（复用同一会话续跑，直接返回新结果）。",
        args_schema=DelegateResumeInput,
    )


def build_manager_spawn_fn(manager: Any, workspace: Any, timeout: float = DELEGATE_TIMEOUT) -> Callable[[str, str], str]:
    """把 TeamManager 包装成委托工具要的 spawn_fn。"""

    def spawn(task: str, agent_type: str) -> str:
        worker_id = manager.spawn(agent_type, task, workspace=str(workspace))
        record = manager.wait(worker_id, timeout=timeout)
        if not record:
            raise RuntimeError("子 Agent 等待超时或无结果")
        result = manager.get_result(worker_id)
        if isinstance(result, dict):
            text = str(result.get("response") or result.get("result") or result.get("output") or "")
            if text:
                return text
            return str(result)
        return str(result or "")

    return spawn


def _result_text(result: Any) -> str:
    """把 manager 结果规整成文本。"""
    if isinstance(result, dict):
        text = str(result.get("response") or result.get("result") or result.get("output") or "")
        return text if text else str(result)
    return str(result or "")


def build_manager_spawn_with_id(
    manager: Any, workspace: Any, timeout: float = DELEGATE_TIMEOUT
) -> Callable[[str, str], tuple]:
    """异步派单用：返回（结果文本，worker_id），供追问复用 thread。"""

    def spawn(task: str, agent_type: str) -> tuple:
        worker_id = manager.spawn(agent_type, task, workspace=str(workspace))
        record = manager.wait(worker_id, timeout=timeout)
        if not record:
            raise RuntimeError("子 Agent 等待超时或无结果")
        return _result_text(manager.get_result(worker_id)), worker_id

    return spawn


def build_manager_resume_fn(manager: Any, timeout: float = DELEGATE_TIMEOUT) -> Callable[[str, str], str]:
    """追问用：按 worker_id 复用 thread 续跑并取新结果。"""

    def resume(worker_id: str, follow_up: str) -> str:
        resumed_id = manager.resume(str(worker_id), str(follow_up))
        record = manager.wait(resumed_id, timeout=timeout)
        if not record:
            raise RuntimeError("追问等待超时或无结果")
        return _result_text(manager.get_result(resumed_id))

    return resume


__all__ = ["DELEGATE_TIMEOUT", "DelegateInput", "DelegateResumeInput", "build_manager_spawn_fn", "build_manager_spawn_with_id", "build_manager_resume_fn", "create_delegate_tool", "create_sync_resume_tool"]
