"""Supervisor 模式的多 Agent 调度。

用 ``langgraph_supervisor`` 接管调度：每个 worker 是 supervisor 图的一个节点，
结果从图 state 读取，不再走 mailbox 文件。

设计说明（为什么 spawn 直接调子图，而不是每次都过 supervisor LLM）：

* ``/team spawn <type> <task>`` 是**强制路由**（用户点名 builder/planner/reviewer），
  不需要再花一次 supervisor LLM 调用去“猜”路由到谁——直接 invoke 命名的子图，
  与 ``Send(to=...)`` 语义等价，且离线可测（FakeModel 即可）。后台注册表
  （``delegate_pool``）的 ``submit``/``poll`` 与此同口径：前者等价 Send 派单，
  后者等价 Command 汇聚，区别只在线程池与图内执行的载体不同。
* supervisor 图（``create_supervisor(...).compile()``）仍保留，用于未来的自动路由
  （``supervisor.invoke({"messages": [...]})`` 让 LLM 自己选 handoff）。两者共用
  同一个子 agent 工厂，路由层与执行层不耦合。
* builder 走隔离 worktree（崩溃隔离 + 可观测性，supervisor 本身无工作区隔离，
  并发 builder 必冲突）；planner/reviewer 只读，不进 worktree。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from .team_agents import _mode_for_agent_type, build_team_agent
from .team_worktree import TeamWorktreeManager

import atexit
import threading

logger = logging.getLogger(__name__)

# 路径 → (saver, conn)：同文件全进程单连接，并行委托不再各开连接撞锁。
_SHARED_CHECKPOINTERS: Dict[str, tuple] = {}
_SHARED_CHECKPOINTERS_LOCK = threading.Lock()


def new_worker_id() -> str:
    """生成新的 worker 标识（原 WorkerManager.new_worker_id，mailbox 子进程模型已删除）。"""
    from uuid import uuid4

    return f"w{str(uuid4()).replace('-', '')[:8]}"


def close_shared_checkpointers() -> int:
    """关闭全部共享 checkpointer 连接，返回关闭数量。

    连接的生命周期是「整个进程」（随进程退出释放），但显式关闭能让
    测试与嵌入式调用方主动回收，避免 `unclosed database` 资源警告。
    """
    with _SHARED_CHECKPOINTERS_LOCK:
        entries = list(_SHARED_CHECKPOINTERS.values())
        _SHARED_CHECKPOINTERS.clear()
    closed = 0
    for _saver, conn in entries:
        try:
            conn.close()
            closed += 1
        except Exception:
            pass
    return closed


atexit.register(close_shared_checkpointers)


def _shared_checkpointer(path: str) -> tuple:
    """取（或建）指定路径的共享 checkpointer。WAL 读不阻塞写，忙等待 30s。"""
    with _SHARED_CHECKPOINTERS_LOCK:
        entry = _SHARED_CHECKPOINTERS.get(path)
        if entry is not None:
            return entry
        import os
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except Exception as exc:
            logger.debug("WAL 模式设置失败，回退默认日志模式: %s", exc)
        saver = SqliteSaver(conn)
        saver.setup()
        _SHARED_CHECKPOINTERS[path] = (saver, conn)
        return saver, conn


class TeamSupervisor:
    """Supervisor 模式的多 Agent 调度器。"""

    def __init__(
        self,
        *,
        model: Any,
        workspace: Path,
        runtime: Any,
        tools: list,
        home: Path,
    ):
        self._model = model
        self._workspace = Path(workspace).expanduser().resolve()
        self._runtime = runtime
        self._tools = list(tools or [])
        self._home = Path(home).expanduser().resolve()
        self._supervisor: Any = None
        self._agents: Dict[str, Any] = {}
        self._checkpointer: Any = None
        self._checkpointer_conn: Any = None
        self.worktrees = TeamWorktreeManager(self._home / "worktrees")
        # 记录 worker 映射：worker_id 关联 thread_id 等执行状态。
        self._workers: Dict[str, Dict[str, Any]] = {}

    # 划分图构造逻辑。

    def _team_checkpointer(self) -> Any:
        """团队共享 checkpointer（thread_id=team-<worker> 隔离会话）。

        同一文件全进程只开一个连接（WAL + 长忙等待）：并行委托经不同
        manager/supervisor 进来时不再各开连接撞锁。生命周期随进程，
        cleanup() 不关闭共享连接。
        """
        if self._checkpointer is not None:
            return self._checkpointer
        saver, conn = _shared_checkpointer(str(self._home / "teams" / "team_checkpoints.sqlite3"))
        self._checkpointer_conn = conn
        self._checkpointer = saver
        return saver

    def _get_agent(self, agent_type: str) -> Any:
        """按类型取（并缓存）编译好的子 ReAct 图。"""
        if agent_type not in self._agents:
            self._agents[agent_type] = build_team_agent(
                model=self._model,
                workspace=self._workspace,
                agent_type=agent_type,
                runtime=self._runtime,
                tools=self._tools_for(agent_type),
                checkpointer=self._team_checkpointer(),
            )
        return self._agents[agent_type]

    def _tools_for(self, agent_type: str) -> list:
        """子 agent 的工具集。

        当前返回主 workspace 的工具列表（调用方在 spawn 时已按隔离 workspace
        重建）。这里保留钩子：以后 planner/reviewer 需要
        只读工具子集时，在此过滤，不动调用方。
        """
        return self._tools

    def get_supervisor_graph(self) -> Any:
        """编译好的 supervisor 图（自动路由用，强制路由的 spawn 不经过它）。"""
        if self._supervisor is None:
            from langgraph_supervisor import create_supervisor

            agents = [self._get_agent(name) for name in ("builder", "planner", "reviewer")]
            self._supervisor = create_supervisor(
                agents,
                model=self._model,
                output_mode="last_message",
            ).compile()
        return self._supervisor

    def auto(self, task: str, *, thread_id: str = "team-auto") -> Dict[str, Any]:
        """自动路由：让 supervisor LLM 自己选 handoff（需要真模型）。"""
        result = self.get_supervisor_graph().invoke(
            {"messages": [{"role": "user", "content": task}]},
            {"configurable": {"thread_id": thread_id}},
        )
        messages = result.get("messages", [])
        final_msg = messages[-1] if messages else None
        content = getattr(final_msg, "content", final_msg) if final_msg else ""
        return {"response": content if isinstance(content, str) else str(content), "raw": result}

    # 划分派单与结果汇聚逻辑。

    def spawn(self, agent_type: str, task: str, workspace: str = ".",
              worker_id: str | None = None) -> str:
        """执行命名的子 agent，返回 worker_id（同步执行，结果落 _workers）。

        worktree 由调用方准备好后经 ``workspace`` 传入隔离路径；
        这里只记录，不再自己建 worktree——建与查的归属都在 worktrees，
        supervisor 只管“跑图 + 记结果”，避免两处建 worktree 打架。
        """
        agent_mode = _mode_for_agent_type(agent_type)
        if not worker_id:
            worker_id = new_worker_id()
        thread_id = f"team-{worker_id}"
        source_workspace = str(Path(workspace).expanduser().resolve())

        agent = self._get_agent(agent_type)
        try:
            result = agent.invoke(
                {"messages": [{"role": "user", "content": task}]},
                {"configurable": {"thread_id": thread_id}},
            )
        except Exception as exc:
            self._workers[worker_id] = {
                "thread_id": thread_id,
                "agent_type": agent_type,
                "agent_mode": agent_mode,
                "worktree": "",
                "branch": "",
                "source_commit": "",
                "source_workspace": source_workspace,
                "response": f"子 Agent 执行失败: {exc}",
                "ok": False,
                "error": str(exc),
                "status": "failed",
            }
            return worker_id

        messages = result.get("messages", []) if isinstance(result, dict) else []
        final_msg = messages[-1] if messages else None
        content = getattr(final_msg, "content", final_msg) if final_msg else ""
        if not isinstance(content, str):
            content = str(content or "")

        self._workers[worker_id] = {
            "thread_id": thread_id,
            "agent_type": agent_type,
            "agent_mode": agent_mode,
            "worktree": "",
            "branch": "",
            "source_commit": "",
            "source_workspace": source_workspace,
            "response": content,
            "ok": True,
            "status": "completed",
            "turns": 1,
        }
        return worker_id

    def resume(self, worker_id: str, follow_up: str) -> str:
        """追问已完成的 worker：复用同一 thread 继续跑，记录追加一轮。

        thread 状态在团队 checkpointer 里（thread_id=team-<worker>），
        因此重建过的 agent 图也能接上同一会话。失败的 worker 不可追问。
        """
        follow_up = str(follow_up or "").strip()
        if not follow_up:
            raise ValueError("追问内容不能为空")
        if len(follow_up) > 20_000:
            raise ValueError("追问内容不能超过 20000 个字符")
        worker = self._workers.get(worker_id)
        if worker is None:
            raise KeyError("未知 worker: " + str(worker_id))
        if not worker.get("ok", False):
            raise RuntimeError("失败的 worker 不可追问，先修好再重派")
        agent = self._get_agent(str(worker.get("agent_type") or "builder"))
        result = agent.invoke(
            {"messages": [{"role": "user", "content": follow_up}]},
            {"configurable": {"thread_id": str(worker.get("thread_id") or ("team-" + worker_id))}},
        )
        messages = result.get("messages", []) if isinstance(result, dict) else []
        final_msg = messages[-1] if messages else None
        content = getattr(final_msg, "content", final_msg) if final_msg else ""
        if not isinstance(content, str):
            content = str(content or "")
        worker["response"] = content
        worker["ok"] = True
        worker["status"] = "completed"
        worker["turns"] = int(worker.get("turns", 1) or 1) + 1
        return worker_id

    def attach_worktree(
        self, worker_id: str, *, worktree: str, branch: str, source_commit: str = ""
    ) -> None:
        """把调用方建好的 worktree 信息挂到 worker 记录上。"""
        worker = self._workers.get(worker_id)
        if worker is not None:
            worker["worktree"] = worktree
            worker["branch"] = branch
            worker["source_commit"] = source_commit

    def get_worker_state(self, worker_id: str) -> Optional[Dict[str, Any]]:
        """返回 worker 的持久化状态（供 /team status 用）。"""
        return self._workers.get(worker_id)

    def get_result(self, worker_id: str, *, mark_read: bool = True) -> Optional[Dict[str, Any]]:
        """从图 state 读取结果（同步执行：spawn 返回时结果已就绪）。"""
        worker = self._workers.get(worker_id)
        if worker is None:
            return None
        payload: Dict[str, Any] = {
            "ok": worker["ok"],
            "response": worker["response"],
            "worker_id": worker_id,
            "agent_type": worker["agent_type"],
            "worktree": worker["worktree"],
            "branch": worker["branch"],
            "source_workspace": worker["source_workspace"],
        }
        if not worker["ok"]:
            payload["error"] = worker.get("error", "")
        return payload

    def wait(self, worker_id: str, *, timeout: float = 60.0) -> Optional[Dict[str, Any]]:
        """等待 worker 完成（同步执行，这里直接返回结果）。"""
        return self.get_result(worker_id)

    def get_delivery(self, worker_id: str) -> Optional[Dict[str, Any]]:
        """返回 worktree 交付信息（分支、diff、提交，只读检查）。"""
        worker = self._workers.get(worker_id)
        if worker is None or not worker["worktree"]:
            return None
        try:
            return self.worktrees.inspect(
                worker["worktree"],
                source_commit=str(worker.get("source_commit") or ""),
            )
        except Exception:
            return {
                "worktree": worker["worktree"],
                "branch": worker["branch"],
                "status": "",
                "diff_stat": "",
                "commits": "",
            }

    def cleanup(self) -> int:
        """清理 worker 状态（图内执行无进程；隔离 worktree 由调用方按需拆除）。

        共享 checkpointer 连接不在这里关闭——它归全进程所有，关掉等于掐断
        别人的写通道；进程退出由 ``close_shared_checkpointers`` 统一回收。
        """
        count = len(self._workers)
        self._workers.clear()
        self._agents.clear()
        self._supervisor = None
        self._checkpointer_conn = None
        self._checkpointer = None
        return count

    def get_status(self) -> str:
        """返回团队状态行。"""
        lines = ["团队: supervisor", f"成员: {len(self._workers)}"]
        for worker_id, worker in self._workers.items():
            lines.append(f"  {worker_id}: {worker['status']} ({worker['agent_type']})")
        return "\n".join(lines)

__all__ = ["TeamSupervisor", "new_worker_id", "close_shared_checkpointers"]
