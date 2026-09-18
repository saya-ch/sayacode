"""自主计划的状态存储：目标 + 任务表 + 轮次计数。

计划是模型自主推进多步任务的共享记分板：模型经 ``plan_create`` 建表，
每完成一步经 ``plan_update`` 销项，编排循环（``SAIAgent.run_with_plan``）
按表判断继续、重规划或收尾。

真相源是进程内 ``PlanStore._plan`` + 图侧 checkpointer 持久化的控制态
（轮次/停滞/路由进图 state）；``plans/<session_id>.json`` 文件镜像已删除，
不再写入。读保留兼容：内存为空时尝试读一次遗留文件（损坏/缺失则视为
无计划），供老会话过渡与单测断言 ``store._path()`` 使用。

权威序列化的唯一入口是 ``Plan.to_dict/from_dict``，调用方（含
``plan_graph._rows_of``）只做视图投影，不复刻字段枚举。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import re

PLAN_SCHEMA_VERSION = 1

TASK_TODO = "todo"
TASK_DOING = "doing"
TASK_DONE = "done"
TASK_FAILED = "failed"
TASK_SKIPPED = "skipped"
VALID_TASK_STATUS = (TASK_TODO, TASK_DOING, TASK_DONE, TASK_FAILED, TASK_SKIPPED)
TERMINAL_TASK_STATUS = (TASK_DONE, TASK_FAILED, TASK_SKIPPED)


@dataclass
class PlanTask:
    """计划中的单个任务。"""

    id: str
    title: str
    status: str = TASK_TODO
    result: str = ""
    depends_on: List[str] = field(default_factory=list)


@dataclass
class Plan:
    """一次自主计划：目标 + 任务表 + 已跑轮次。"""

    goal: str
    tasks: List[PlanTask] = field(default_factory=list)
    rounds: int = 0
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def all_done(self) -> bool:
        """全部任务是否都已终结（done/failed/skipped）。"""
        return bool(self.tasks) and all(t.status in TERMINAL_TASK_STATUS for t in self.tasks)

    def pending_ids(self) -> List[str]:
        """未终结任务的 id 列表（todo/doing）。"""
        return [t.id for t in self.tasks if t.status not in TERMINAL_TASK_STATUS]

    def rows(self) -> List[Dict[str, Any]]:
        """任务行视图（含图调度用的 depends_on）：唯一行投影入口。

        ``plan_graph`` 的快照/路由只读此视图，不另写字段枚举，
        避免与 ``to_dict`` 的序列化重复。
        """
        return [
            {
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "result": t.result,
                "depends_on": list(t.depends_on or []),
            }
            for t in self.tasks
        ]

    def snapshot(self) -> str:
        """渲染成注入下一轮输入的计划快照。"""
        lines = [f"目标：{self.goal}", f"轮次：{self.rounds}"]
        for task in self.tasks:
            mark = {"todo": "○", "doing": "◐", "done": "●", "failed": "✕", "skipped": "—"}.get(task.status, "?")
            lines.append(f"- [{mark}] {task.id}: {task.title}（{task.status}）")
            if task.result:
                lines.append(f"  结果：{task.result[:500]}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """转可序列化字典。"""
        return {
            "schema_version": PLAN_SCHEMA_VERSION,
            "goal": self.goal,
            "rounds": self.rounds,
            "updated_at": self.updated_at,
            "tasks": [asdict(t) for t in self.tasks],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Plan":
        """从字典恢复计划，非法条目按 todo 重建。"""
        tasks = []
        for index, raw in enumerate(data.get("tasks") or []):
            if not isinstance(raw, dict):
                continue
            status = str(raw.get("status") or TASK_TODO)
            raw_depends = raw.get("depends_on") or []
            depends = [str(d) for d in raw_depends] if isinstance(raw_depends, (list, tuple)) else []
            tasks.append(PlanTask(
                id=str(raw.get("id") or f"t{index + 1}"),
                title=str(raw.get("title") or ""),
                status=status if status in VALID_TASK_STATUS else TASK_TODO,
                result=str(raw.get("result") or ""),
                depends_on=depends,
            ))
        return cls(
            goal=str(data.get("goal") or ""),
            tasks=tasks,
            rounds=int(data.get("rounds") or 0),
            updated_at=str(data.get("updated_at") or ""),
        )


class PlanStore:
    """单个会话的计划存储：内存为真相源，文件只读兼容。

    与 Store/checkpointer 分工：``_plan`` 是唯一真相源，图侧 checkpointer
    持久化轮次/停滞等控制态；模型写表后同实例内存即时可见，无需 ``refresh()``
    合并。遗留 ``plans/<session>.json`` 文件不再写入，仅在内存为空时读一次
    做过渡兼容（损坏/缺失视为无计划）。
    """

    def __init__(self, workspace: str | Path, session_id: str = "default") -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.session_id = str(session_id or "default")
        self._plan: Optional[Plan] = None

    @classmethod
    def for_runtime(cls, runtime: Any) -> "PlanStore":
        """从 RuntimeContext 解析工作区与会话 id。"""
        session = getattr(runtime, "session", None)
        session_id = getattr(session, "session_id", "default") if session is not None else "default"
        return cls(getattr(runtime, "workspace"), session_id=session_id)

    def _path(self) -> Path:
        """遗留计划文件路径（只读兼容用，不再写入）。"""
        from ..runtime.session_store import workspace_state_dir

        return workspace_state_dir(Path(self.workspace)) / "plans" / f"{self._safe_session_id()}.json"

    def _safe_session_id(self) -> str:
        """清洗会话 id，防路径穿越，非法字符压成横线。"""
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", self.session_id).strip("-")
        return cleaned or "default"

    def _commit(self, plan: Plan) -> None:
        """提交计划到内存真相源（只刷新时间戳，不再落盘）。"""
        plan.updated_at = datetime.now(timezone.utc).isoformat()
        self._plan = plan

    def _read_legacy_file(self) -> Optional[Plan]:
        """读一次遗留文件镜像：缺失/损坏返回 None，不抛错。"""
        try:
            path = self._path()
            if not path.is_file():
                return None
            plan = Plan.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None
        self._plan = plan
        return plan

    def create(self, goal: str, titles: List[str]) -> Plan:
        """新建计划（覆盖同会话旧表），返回计划。"""
        goal = str(goal or "").strip()
        if not goal:
            raise ValueError("计划目标不能为空")
        clean_titles = [str(t or "").strip() for t in titles or []]
        clean_titles = [t for t in clean_titles if t]
        if not clean_titles:
            raise ValueError("计划至少需要一个任务")
        # 任务上限 20 个，防模型一次建超大计划拖慢编排。
        if len(clean_titles) > 20:
            raise ValueError("计划任务不能超过 20 个")
        plan = Plan(goal=goal, tasks=[PlanTask(id=f"t{i + 1}", title=t) for i, t in enumerate(clean_titles)])
        self._commit(plan)
        return plan

    def get(self) -> Optional[Plan]:
        """读当前计划（无计划返回 None；内存空时读一次遗留文件兼容）。"""
        if self._plan is not None:
            return self._plan
        return self._read_legacy_file()

    def update(self, task_id: str, status: str, result: str = "") -> Plan:
        """更新任务状态并提交，返回更新后计划。"""
        plan = self.get()
        if plan is None:
            raise ValueError("当前无计划，先调用 plan_create")
        status = str(status or "").strip().lower()
        if status not in VALID_TASK_STATUS:
            raise ValueError(f"非法状态: {status}，可选: {'/'.join(VALID_TASK_STATUS)}")
        for task in plan.tasks:
            if task.id == task_id:
                task.status = status
                if result:
                    task.result = str(result)[:2000]
                self._commit(plan)
                return plan
        raise ValueError(f"计划中没有任务: {task_id}")

    def note_round(self) -> int:
        """轮次计数加一并提交，返回当前轮次。"""
        plan = self.get()
        if plan is None:
            return 0
        plan.rounds += 1
        self._commit(plan)
        return plan.rounds

    def refresh(self) -> Optional[Plan]:
        """重读真相源：内存命中直接返回，内存空时读一次遗留文件。

        同实例内存即时可见，不再需要丢缓存；保留方法名供 ``plan_graph``
        节点侧调用，语义不变。
        """
        return self.get()

    def clear(self) -> None:
        """清空当前计划（内存 + 遗留文件残留清理）。"""
        self._plan = None
        try:
            path = self._path()
            if path.is_file():
                path.unlink()
        except Exception:
            pass


__all__ = [
    "Plan",
    "PlanStore",
    "PlanTask",
    "TASK_DOING",
    "TASK_DONE",
    "TASK_FAILED",
    "TASK_SKIPPED",
    "TASK_TODO",
    "VALID_TASK_STATUS",
]
