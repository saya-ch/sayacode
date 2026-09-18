"""自主计划的状态图：规划→执行→评估→收尾/重规划。

替代裸 for 循环编排：控制态（轮次/停滞/路由）进图 state，随 checkpointer
持久化；JSON 计划文件只当模型可见镜像（plan_* 工具与 /plan 照常用）。

planner 小图同样挂中间件：plan 工具调用的 Hook/审计覆盖与主 turn 一致，
不因换执行载体而丢失。执行仍走主 Agent 的 turn（中间件与恢复路径不变）。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, TypedDict

try:
    from langchain.agents import create_agent as create_langchain_agent
except ImportError:
    create_langchain_agent = None

try:
    from langgraph.graph import END, START, StateGraph
except ImportError:
    StateGraph = None
    END = "end"
    START = "__start__"


class PlanGraphState(TypedDict, total=False):
    """计划图状态：节点返回的键逐个覆盖，未返回的键保持。"""

    goal: str
    tasks: List[Dict[str, Any]]
    rounds: int
    max_rounds: int
    stalls: int
    last_pending: List[str]
    last_response: str
    decision: str
    final: str


def _rows_of(plan: Any) -> List[Dict[str, Any]]:
    """PlanStore 计划转任务行（含 Send 阶段用的 depends_on，默认空）。"""
    tasks = getattr(plan, "tasks", None) or []
    rows = []
    for task in tasks:
        depends = list(getattr(task, "depends_on", None) or [])
        rows.append({
            "id": str(getattr(task, "id", "")),
            "title": str(getattr(task, "title", "")),
            "status": str(getattr(task, "status", "todo")),
            "result": str(getattr(task, "result", "")),
            "depends_on": [str(d) for d in depends],
        })
    return rows


def build_plan_graph(
    *,
    model: Any,
    plan_tools: List[Any],
    run_turn: Callable[[str], str],
    store: Any,
    overlay: str,
    checkpointer: Any,
    permissions: Any = None,
) -> Any:
    """构建自主计划图并编译。缺件即抛错。"""
    if StateGraph is None:
        raise RuntimeError("需要支持 StateGraph 的 langgraph 版本")
    if create_langchain_agent is None:
        raise RuntimeError("需要支持 create_agent 的 langchain 版本")
    if checkpointer is None:
        raise RuntimeError("计划图需要 checkpointer 持久化控制态")

    from .middleware import SayaHookMiddleware, SayaPermissionMiddleware, SayaSafetyMiddleware

    middlewares: List[Any] = [SayaHookMiddleware()]
    if permissions is not None:
        middlewares.append(SayaPermissionMiddleware(permissions))
    middlewares.append(SayaSafetyMiddleware())
    planner_agent = create_langchain_agent(model, list(plan_tools), middleware=middlewares)

    def _read_tasks() -> List[Dict[str, Any]]:
        # 模型经工具侧写表，节点侧必须重读文件，不能用内存缓存。
        plan = store.refresh()
        return _rows_of(plan) if plan is not None else []

    def planner(state: Dict[str, Any]) -> Dict[str, Any]:
        """跑一次规划子图并重读任务表，失败时直接收尾。"""
        goal = str(state.get("goal") or "")
        try:
            result = planner_agent.invoke({
                "messages": [{"role": "user", "content": goal + "\n\n" + overlay}],
            })
        except Exception as exc:
            return {"final": "规划失败：" + str(exc), "tasks": [], "rounds": 1}
        tasks = _read_tasks()
        rounds = int(state.get("rounds") or 0) + 1
        if not tasks:
            text = _last_text(result)
            return {"final": text, "tasks": [], "rounds": rounds}
        return {"tasks": tasks, "rounds": rounds, "last_response": _last_text(result)}

    def executor(state: Dict[str, Any]) -> Dict[str, Any]:
        """按当前计划快照推进一轮主 Agent 执行。"""
        plan = store.get()
        snapshot = plan.snapshot() if plan is not None else ""
        response = run_turn("继续按计划推进：先 plan_get 看表，做一步销项一步。\n" + snapshot)
        rounds = int(state.get("rounds") or 0) + 1
        return {"tasks": _read_tasks(), "rounds": rounds, "last_response": response}

    def replan(state: Dict[str, Any]) -> Dict[str, Any]:
        """连续停滞时强制重规划，禁重复已失败动作。"""
        plan = store.get()
        snapshot = plan.snapshot() if plan is not None else ""
        response = run_turn(
            "计划已连续两轮无进展，必须重规划：修订剩余任务（拆分、换路"
            "或标记 skipped 并说明原因），不要重复已失败的动作。\n" + snapshot
        )
        rounds = int(state.get("rounds") or 0) + 1
        return {"tasks": _read_tasks(), "rounds": rounds, "last_response": response}

    def evaluator(state: Dict[str, Any]) -> Dict[str, Any]:
        """按待办、轮次与停滞计数路由下一步走向。"""
        tasks = state.get("tasks") or _read_tasks()
        pending = [t["id"] for t in tasks if t.get("status") not in ("done", "failed", "skipped")]
        if not tasks:
            return {"decision": "done"}
        if not pending:
            return {"decision": "done"}
        if int(state.get("rounds") or 0) >= int(state.get("max_rounds") or 6):
            return {"decision": "exhausted", "tasks": tasks}
        last_pending = list(state.get("last_pending") or [])
        # 待办集合两轮不变即记一次停滞，满 2 次强制重规划。
        stalls = int(state.get("stalls") or 0) + (1 if pending == last_pending and last_pending else 0)
        if stalls >= 2:
            return {"decision": "replan", "tasks": tasks, "stalls": 0, "last_pending": pending}
        return {"decision": "execute", "tasks": tasks, "stalls": stalls, "last_pending": pending}

    def finish(state: Dict[str, Any]) -> Dict[str, Any]:
        """汇总最终文本，轮次耗尽时注明剩余任务去向。"""
        text = str(state.get("last_response") or state.get("final") or "")
        if state.get("decision") == "exhausted":
            text += "\n\n计划未完全终结（轮次耗尽），剩余任务见 plan_get。"
        return {"final": text}

    def route_entry(state: Dict[str, Any]) -> str:
        """入口路由：已有任务表则直接评估，否则先规划。"""
        if state.get("tasks") or int(state.get("rounds") or 0) > 0:
            return "evaluator"
        return "planner"

    def route_decision(state: Dict[str, Any]) -> str:
        """按评估决策分发执行、重规划或收尾。"""
        decision = str(state.get("decision") or "done")
        if decision == "execute":
            return "executor"
        if decision == "replan":
            return "replan"
        return "finish"

    builder = StateGraph(PlanGraphState)
    builder.add_node("planner", planner)
    builder.add_node("executor", executor)
    builder.add_node("replan", replan)
    builder.add_node("evaluator", evaluator)
    builder.add_node("finish", finish)
    builder.add_conditional_edges(START, route_entry, {"planner": "planner", "evaluator": "evaluator"})
    builder.add_edge("planner", "evaluator")
    builder.add_edge("executor", "evaluator")
    builder.add_edge("replan", "evaluator")
    builder.add_conditional_edges(
        "evaluator", route_decision,
        {"executor": "executor", "replan": "replan", "finish": "finish"},
    )
    builder.add_edge("finish", END)
    return builder.compile(checkpointer=checkpointer)


def _last_text(result: Any) -> str:
    """从 agent 结果里取最后一段文本。"""
    messages: list = []
    if isinstance(result, dict):
        messages = result.get("messages", []) or []
    for message in reversed(messages):
        content = getattr(message, "content", message)
        if isinstance(content, str) and content.strip():
            return content
    output = result.get("output") if isinstance(result, dict) else None
    return str(output or result or "")


def open_plan_checkpointer(workspace: Any) -> tuple:
    """打开计划图专用 SqliteSaver（与主图同目录不同文件），返回 (saver, conn)。"""
    import os
    import sqlite3
    from pathlib import Path

    from langgraph.checkpoint.sqlite import SqliteSaver

    from ..runtime.session_store import workspace_state_dir

    path = str(workspace_state_dir(Path(workspace).expanduser().resolve()) / "plan_checkpoints.sqlite3")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    return saver, conn


__all__ = ["PlanGraphState", "build_plan_graph", "open_plan_checkpointer"]
