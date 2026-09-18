"""自主计划的模型可见工具：建表、销项、查表。

三个工具都是计划记分板（PlanStore）的薄封装：参数校验与落盘在
PlanStore 层，权限走标准的工具权限门（plan_* 默认 allow，只读写
本会话计划文件，无工作区副作用）。
"""

from __future__ import annotations

from typing import Callable, List

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ..core.plans import PlanStore


class PlanCreateInput(BaseModel):
    """plan_create 的输入。"""

    goal: str = Field(description="计划目标（一句话）")
    tasks: List[str] = Field(min_length=1, max_length=20, description="任务标题列表，按执行顺序排列")


class PlanUpdateInput(BaseModel):
    """plan_update 的输入。"""

    task_id: str = Field(description="任务 id（如 t1），见建表返回或 plan_get 快照")
    status: str = Field(description="新状态：todo/doing/done/failed/skipped")
    result: str = Field(default="", description="任务结果摘要（done/failed 时填写，供后继任务使用）")


def create_plan_tools(store_factory: Callable[[], PlanStore]) -> List[StructuredTool]:
    """创建绑定到当前会话计划存储的三个工具。"""

    def plan_create(goal: str, tasks: List[str]) -> str:
        # 委托 PlanStore 建表，失败直接返回中文原因。
        try:
            plan = store_factory().create(goal, tasks)
        except ValueError as exc:
            return "创建计划失败：" + str(exc)
        # 组装建表回执，引导逐项销项。
        lines = ["计划已创建，共 " + str(len(plan.tasks)) + " 个任务："]
        lines.extend("- " + t.id + ": " + t.title for t in plan.tasks)
        lines.append("逐个执行任务，每完成一个调用 plan_update 销项；全部 done 后做最终总结。")
        return chr(10).join(lines)

    def plan_update(task_id: str, status: str, result: str = "") -> str:
        # 委托 PlanStore 销项，失败直接返回中文原因。
        try:
            plan = store_factory().update(task_id, status, result)
        except ValueError as exc:
            return "更新计划失败：" + str(exc)
        return "已更新 " + task_id + "。" + chr(10) + plan.snapshot()

    def plan_get() -> str:
        # 读取当前计划快照，无表时引导建表。
        plan = store_factory().get()
        if plan is None:
            return "当前无计划。复杂多步任务先调用 plan_create 建表，单步任务直接做答。"
        return plan.snapshot()

    return [
        StructuredTool.from_function(
            func=plan_create,
            name="plan_create",
            description="为复杂多步任务创建执行计划（目标加有序任务表）。单步任务不要调用，直接做答。",
            args_schema=PlanCreateInput,
        ),
        StructuredTool.from_function(
            func=plan_update,
            name="plan_update",
            description="更新计划中某个任务的状态（开工标 doing，做完标 done 并写结果摘要，卡住标 failed）。",
            args_schema=PlanUpdateInput,
        ),
        StructuredTool.from_function(
            func=plan_get,
            name="plan_get",
            description="查看当前计划快照（目标、各任务状态与结果）。决定下一步前先看表。",
        ),
    ]


__all__ = ["PlanCreateInput", "PlanUpdateInput", "create_plan_tools"]
