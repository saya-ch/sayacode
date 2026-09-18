"""子 Agent 委托工具：把可隔离的任务块交给子 Agent 执行。

``delegate_to_subagent`` 让模型自主决定是否分工：适合并行推进、需要隔离
工作区、或适合只读复核的子任务。执行走 TeamManager 的同步 spawn→wait→结果
链（builder 自动进隔离 worktree），与 /team 同一底座。
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ..core.delegate_pool import MAX_DELEGATE_CONCURRENCY, delegate_slot

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
            # 限流并发委托，避免打满后台。
            with delegate_slot():
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


class DelegateCancelInput(BaseModel):
    """delegate_cancel 的输入。"""

    handle_id: str = Field(description="要取消的委托句柄 id")


class DelegatePollInput(BaseModel):
    """delegate_poll 的输入。"""

    handle_id: str = Field(description="派单返回的句柄 id")
    wait_seconds: float = Field(default=0, description="最多阻塞等待秒数（0 为只查一次）")


def create_async_delegate_tools(
    spawn_fn: Callable[[str, str], Any],
    registry: Any = None,
    resume_fn: Any = None,
) -> list:
    """创建异步委托双工具：派单即返句柄，poll 汇聚结果。

    ``resume_fn(恢复令牌, 追问)`` 给定时，派单可追问（spawn 返回元组时记录令牌）。
    """
    from ..core.delegate_pool import get_delegate_registry

    pool = registry if registry is not None else get_delegate_registry()

    def delegate_async(task: str, agent_type: str = "builder") -> str:
        # 校验派单输入，拦截空任务与超长描述。
        task = str(task or "").strip()
        if not task:
            return "派单失败：任务描述不能为空"
        if len(task) > 20_000:
            return "派单失败：任务描述不能超过 20000 个字符"
        agent_type = str(agent_type or "builder").strip().lower() or "builder"
        handle = pool.submit(spawn_fn, task, agent_type, resume_fn=resume_fn)
        return ("已派单：" + handle + "。先做其他任务，稍后用 delegate_poll 查询；"
                "收尾前必须 poll 所有未终结委托，拿到结果再总结。")

    def delegate_poll(handle_id: str, wait_seconds: float = 0) -> str:
        # 校验句柄非空，避免查询幽灵委托。
        handle = str(handle_id or "").strip()
        if not handle:
            return "查询失败：句柄 id 不能为空"
        try:
            job = pool.poll(handle, wait_seconds=max(0.0, float(wait_seconds or 0)))
        except KeyError:
            return "查询失败：未知句柄 " + handle
        except Exception as exc:
            return "查询失败：" + str(exc)
        if job.status == "done":
            result = str(job.result or "")
            if len(result) > MAX_DELEGATE_CHARS:
                result = result[:MAX_DELEGATE_CHARS] + chr(10) + "……（结果过长已截断）"
            return "已完成：" + chr(10) + (result or "子 Agent 无返回")
        if job.status == "failed":
            return "子 Agent 失败：" + str(job.error or "未知错误")
        return "未完成（" + job.status + "）：先做别的，稍后再 poll " + handle

    return [
        StructuredTool.from_function(
            func=delegate_async,
            name="delegate_async",
            description="异步派单给子 Agent，立即返回句柄（不等结果）。派多个可并行推进；派单后先做其他任务，再用 delegate_poll 汇聚。后台不做交互确认。",
            args_schema=DelegateInput,
        ),
        StructuredTool.from_function(
            func=delegate_poll,
            name="delegate_poll",
            description="查询异步委托的状态并取结果（done 给结果，failed 给原因，pending/running 先做别的）。收尾前必须 poll 所有未终结委托。",
            args_schema=DelegatePollInput,
        ),
    ]


class DelegateResumeInput(BaseModel):
    """delegate_resume 的输入。"""

    handle_id: str = Field(description="此前派单返回的句柄 id（须已终结）")
    follow_up: str = Field(description="追问内容（补充约束、要求返工或深入一层）")


def create_cancel_tool(cancel_fn: Callable[[str], Any]) -> StructuredTool:
    """创建取消工具：未开跑直接撤，已开跑发 abort 快速收尾。"""

    def delegate_cancel(handle_id: str) -> str:
        # 校验句柄非空，已终结委托直接放行。
        handle = str(handle_id or "").strip()
        if not handle:
            return "取消失败：句柄 id 不能为空"
        try:
            job = cancel_fn(handle)
        except KeyError:
            return "取消失败：未知句柄 " + handle
        if job.status in ("done", "failed", "cancelled"):
            return "无需取消：委托已终结（" + job.status + "）"
        return "已取消：" + handle + "（未开跑直接撤回，已开跑中止收尾中，用 poll 确认）"

    return StructuredTool.from_function(
        func=delegate_cancel,
        name="delegate_cancel",
        description="提前取消异步委托（未开跑直接撤回，已开跑发中止信号快速收尾）。派错单、方向错了就取消，别等它跑完。",
        args_schema=DelegateCancelInput,
    )


def create_resume_tool(
    resume_fn: Callable[[str, str], Any],
    notifications_fn: Callable[[], list],
) -> list:
    """创建追问与通知工具（共享注册表的句柄空间）。"""

    def delegate_resume(handle_id: str, follow_up: str) -> str:
        # 校验句柄与追问均非空，守住 resume 入口。
        handle = str(handle_id or "").strip()
        follow_up = str(follow_up or "").strip()
        if not handle:
            return "追问失败：句柄 id 不能为空"
        if not follow_up:
            return "追问失败：追问内容不能为空"
        try:
            job = resume_fn(handle, follow_up)
        except (KeyError, ValueError, RuntimeError) as exc:
            return "追问失败：" + str(exc)
        return ("已追问：" + handle + "（第 " + str(job.turns + 1) + " 轮），后台执行中；"
                "用 delegate_poll 取新结果。")

    def delegate_notifications() -> str:
        fresh = notifications_fn()
        if not fresh:
            return "暂无新完成的委托。"
        lines = ["新完成的委托："]
        for job in fresh:
            state = "完成" if job.status == "done" else "失败"
            preview = str(job.result or job.error or "")[:300]
            lines.append("- " + job.handle + "（" + job.agent_type + "，" + state + "）：" + preview)
        lines.append("用 delegate_poll 取完整结果，需要可 delegate_resume 追问。")
        return chr(10).join(lines)

    return [
        StructuredTool.from_function(
            func=delegate_resume,
            name="delegate_resume",
            description="追问已完成的异步委托（复用同一子 Agent 会话续跑）。后台执行，poll 取新结果。",
            args_schema=DelegateResumeInput,
        ),
        StructuredTool.from_function(
            func=delegate_notifications,
            name="delegate_notifications",
            description="取自上次以来新完成的委托（完成推送）。每轮顺手查一次，别让做完的活躺着。",
        ),
    ]


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


__all__ = ["DELEGATE_TIMEOUT", "MAX_DELEGATE_CONCURRENCY", "DelegateCancelInput", "DelegateInput", "DelegatePollInput", "DelegateResumeInput", "build_manager_spawn_fn", "build_manager_spawn_with_id", "build_manager_resume_fn", "create_async_delegate_tools", "create_cancel_tool", "create_delegate_tool", "create_resume_tool"]
