"""SAIAgent 使用的 Agent runtime 组件。

负责 LangGraph agent 生命周期管理。核心类：AgentRunner。
调用链：SAIAgent→AgentRunner→LangGraph。

system 组装走 SayaPromptMiddleware 的 dynamic_prompt 机制
（外层每轮 refresh() 一次）；历史由 checkpointer 持有，压缩后由外层
sync_messages 覆盖；首轮全量导入与历史转换见 lib.agent_recovery。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterator, List, Optional, Union
import os

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from ..i18n import tr
from ..cli.theme import print_warning

try:
    from langchain.agents import create_agent as create_langchain_agent
except ImportError:
    create_langchain_agent = None


MessageLike = Union[SystemMessage, HumanMessage, AIMessage]


# ==============================================================================
# 维护 Turn 状态机。
# ==============================================================================


class TurnTransition(Enum):
    """每次 Agent turn 的转换原因 — 参考 Claude Code queryLoop transition。"""
    NEXT_TURN = "next_turn"
    COMPLETED = "completed"
    STREAM_INTERRUPTED = "stream_interrupted"
    MODEL_ERROR = "model_error"
    MAX_RETRIES = "max_retries"
    ABORTED = "aborted"


@dataclass
class TurnState:
    """追踪单次 Agent turn 的执行状态。"""
    transition: TurnTransition = TurnTransition.COMPLETED
    turn_count: int = 0
    tool_use_count: int = 0
    needs_follow_up: bool = False
    error_message: str = ""

    @property
    def is_terminal(self) -> bool:
        """Turn 是否为终止状态（不需要继续）。"""
        return self.transition in (
            TurnTransition.COMPLETED,
            TurnTransition.MODEL_ERROR,
            TurnTransition.MAX_RETRIES,
            TurnTransition.ABORTED,
        )

    @property
    def should_continue(self) -> bool:
        """Turn 是否需要继续（有工具调用需要执行）。"""
        return self.transition == TurnTransition.NEXT_TURN and self.needs_follow_up


@dataclass
class AgentRunner:
    """负责 model/tool 绑定与 LangGraph agent 生命周期。

    单工厂：永远 create_agent + 中间件；checkpointer 有无只决定
    持久化语义（增量历史 vs 每轮全量），不切换工厂。

    * 持久化（checkpoint_path 给出且 sqlite 可用）：中间件 +
      SqliteSaver（thread_id 隔离）+ store。调用方只传增量消息；
      历史由 checkpointer 持有，压缩后由外层 sync_messages 覆盖。
    * 无持久化：同一工厂、同一中间件，只是 checkpointer=None，
      调用方每次传全量（graph_enabled 为 False 时走这里）。
    """

    model: Any
    tools: List[BaseTool]
    system_prompt: str
    agent: Optional[Any] = None
    model_with_tools: Optional[Any] = None
    permissions: Optional[Any] = None
    safety_checker: Optional[Any] = None
    checkpoint_path: Optional[str] = None
    thread_id: str = ""
    # 以下由 rebuild() 管理，调用方不要直接碰。
    prompt_middleware: Optional[Any] = None
    _trace_handler: Optional[Any] = None
    _saver: Optional[Any] = None
    _saver_conn: Optional[Any] = None
    _store: Optional[Any] = None

    @property
    def graph_enabled(self) -> bool:
        """是否走图路径（中间件 + 持久化都就绪）。"""
        return self.agent is not None and self._saver is not None

    def rebuild(self) -> Optional[Any]:
        """重建模型绑定与 agent 图（可观测走 LangSmith 环境配置，无需代码内 handler）。"""
        self.close()
        self._trace_handler = None
        self.model_with_tools = self._bind_tools()
        self.agent = self._create_agent()
        return self.agent

    def close(self) -> None:
        """关掉 checkpointer 连接（rebuild/析构时调用，避免 sqlite 锁残留）。"""
        conn, self._saver_conn = self._saver_conn, None
        self._saver, self._store, self.prompt_middleware = None, None, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def __del__(self) -> None:
        """析构兜底：调用方漏掉 close() 时释放 sqlite 连接。"""
        try:
            self.close()
        except Exception:
            pass

    def _thread_config(self) -> Dict[str, Any]:
        return {"configurable": {"thread_id": self.thread_id or "default"}}

    def _run_config(self) -> Dict[str, Any]:
        """执行用配置：线程隔离 + 模型调用追踪回调（随 config 透传到模型节点）。"""
        config = self._thread_config()
        if self._trace_handler is not None:
            config["callbacks"] = [self._trace_handler]
        return config

    def invoke(self, messages: List[MessageLike]) -> Optional[Dict[str, Any]]:
        """同步执行 agent 并返回结果。"""
        if not self.agent:
            return None
        if self.graph_enabled:
            return self.agent.invoke({"messages": messages}, self._run_config())
        return self.agent.invoke({"messages": messages})

    def stream(self, payload: Any) -> Optional[Iterator[Any]]:
        """流式执行。payload 是消息列表（包成 {"messages": …}）或 Command（恢复）。"""
        if not self.agent or not hasattr(self.agent, "stream"):
            return None
        body = payload if not isinstance(payload, list) else {"messages": payload}

        # 同时订阅两种模式，逐词输出思考进展，是思考中唯一来源。
        # 节点级输出工具调用标签与执行结果，补充不调用模型的节点。
        # 只订阅节点模式，长模型调用期间结构上无任何可显示内容。
        config = self._run_config() if self.graph_enabled else None
        try:
            if config is not None:
                return self.agent.stream(body, config, stream_mode=["updates", "messages"])
            return self.agent.stream(body, stream_mode=["updates", "messages"])
        except TypeError:
            pass

        try:
            if config is not None:
                return self.agent.stream(body, config, stream_mode="updates")
            return self.agent.stream(body, stream_mode="updates")
        except TypeError:
            try:
                return self.agent.stream(body, stream_mode="values")
            except TypeError:
                return self.agent.stream(body)

    def resume(self, resume_value: Any) -> Optional[Iterator[Any]]:
        """从 __interrupt__ 恢复：resume_value 即 middleware 收到的答案。"""
        from langgraph.types import Command

        return self.stream(Command(resume=resume_value))

    def invoke_command(self, resume_value: Any) -> Optional[Dict[str, Any]]:
        """非流路径的中断恢复（invoke 遇到中断是正常返回，不是抛错）。"""
        from langgraph.types import Command

        if not self.agent or not self.graph_enabled:
            return None
        return self.agent.invoke(Command(resume=resume_value), self._run_config())

    def thread_message_count(self) -> int:
        """当前线程已持久化的消息数（0 = 空线程，首轮按全量导入处理）。"""
        if not self.graph_enabled:
            return 0
        agent = self.agent
        if agent is None:
            return 0
        try:
            state = agent.get_state(self._thread_config())
            messages = (state.values or {}).get("messages", []) if state else []
            return len(messages or [])
        except Exception:
            return 0

    def _current_thread_messages(self) -> List[MessageLike]:
        """取当前线程全量消息（失败返回空列表）。"""
        try:
            state = self.agent.get_state(self._thread_config()) if self.agent else None
            messages = (state.values or {}).get("messages", []) if state else []
            return list(messages or [])
        except Exception:
            return []

    @staticmethod
    def _count_user_turns(messages: List[Any]) -> int:
        """数 Human 轮次（System/Tool/AI 不计）。"""
        count = 0
        for msg in messages or []:
            try:
                if isinstance(msg, HumanMessage):
                    count += 1
                elif message_kind(msg) in ("human", "user"):
                    count += 1
            except Exception:
                continue
        return count

    def current_turn_count(self) -> int:
        """当前线程已落盘的用户轮次（Human 条数）。"""
        if not self.graph_enabled:
            return 0
        return self._count_user_turns(self._current_thread_messages())

    def list_rewind_points(self) -> List[Dict[str, Any]]:
        """列出可回退的轮次检查点（新→旧，含 0）。"""
        if not self.graph_enabled:
            return []
        agent = self.agent
        if agent is None:
            return []
        current = self.current_turn_count()
        created_map: Dict[int, Any] = {}
        try:
            history = list(agent.get_state_history(self._thread_config()))
            for snap in history:
                try:
                    msgs = (snap.values or {}).get("messages", []) or []
                    turns = self._count_user_turns(msgs)
                    if turns not in created_map:
                        created_map[turns] = getattr(snap, "created_at", "") or ""
                except Exception:
                    continue
        except Exception:
            pass
        return [
            {"turns": turns, "created_at": str(created_map.get(turns, "") or "")}
            for turns in range(current, -1, -1)
        ]

    def rewind_to_turn_count(self, target: int) -> bool:
        """回退到目标轮次边界（基于 get_state_history 找边界后分叉）。"""
        if not self.graph_enabled or self.agent is None:
            return False
        try:
            target_int = int(target)
        except (TypeError, ValueError):
            return False
        if target_int < 0:
            return False
        current = self.current_turn_count()
        if target_int > current:
            return False
        if target_int == current:
            return True
        try:
            history = list(self.agent.get_state_history(self._thread_config()))
        except Exception:
            return False
        # 找目标轮次的 turn-complete 快照：Human 数==target 且末条为 AI（0 则取空）。
        target_ids: Optional[set] = None
        for snap in history:
            try:
                msgs = list((snap.values or {}).get("messages", []) or [])
            except Exception:
                continue
            if self._count_user_turns(msgs) != target_int:
                continue
            if target_int == 0:
                if len(msgs) == 0:
                    target_ids = set()
                    break
                continue
            if not msgs:
                continue
            try:
                last_kind = message_kind(msgs[-1])
            except Exception:
                last_kind = ""
            if last_kind in ("ai", "assistant", "aimessagechunk"):
                try:
                    target_ids = {getattr(m, "id", None) for m in msgs if getattr(m, "id", None)}
                except Exception:
                    target_ids = None
                break
        current_msgs = self._current_thread_messages()
        if target_ids is None:
            # 兜底：按 Human 位置截到目标轮的末条。
            human_idx = [
                i for i, m in enumerate(current_msgs)
                if isinstance(m, HumanMessage) or message_kind(m) in ("human", "user")
            ]
            if target_int == 0:
                keep = 1 if current_msgs and isinstance(current_msgs[0], SystemMessage) else 0
                keep_ids = {getattr(m, "id", None) for m in current_msgs[:keep] if getattr(m, "id", None)}
                target_ids = keep_ids
            elif len(human_idx) < target_int:
                return False
            else:
                end = human_idx[target_int] if target_int < len(human_idx) else len(current_msgs)
                keep_msgs = current_msgs[:end]
                target_ids = {getattr(m, "id", None) for m in keep_msgs if getattr(m, "id", None)}
        try:
            from langchain_core.messages import RemoveMessage

            keep_set = target_ids or set()
            to_remove = [getattr(m, "id", None) for m in current_msgs if getattr(m, "id", None) not in keep_set]
            to_remove = [i for i in to_remove if i]
            if not to_remove:
                return True
            self.agent.update_state(
                self._thread_config(), {"messages": [RemoveMessage(id=i) for i in to_remove]}
            )
            return True
        except Exception:
            return False

    def sync_messages(self, full_messages: List[MessageLike]) -> None:
        """用镜像全量覆盖图状态：只在压缩后调用（平时增量追加，不碰）。"""
        if not self.graph_enabled:
            return
        agent = self.agent
        if agent is None:
            return
        agent.update_state(
            self._thread_config(), {"messages": list(full_messages)}
        )

    def refresh_prompt(self, system_text: str) -> None:
        """设置本轮 system 文本（中间件每轮调用一次，与今天同成本）。"""
        if self.prompt_middleware is not None:
            self.prompt_middleware.refresh(system_text)

    def remember_turn(self, turn_no: int, user_input: str, response: str) -> None:
        """把回合摘要写进 store（历史真相源为 session + checkpointer，store 供子 agent 读）。"""
        if not self.graph_enabled or self._store is None:
            return
        try:
            self._store.put(
                ("memories", self.thread_id or "default"),
                f"turn-{turn_no}",
                {"user_input": str(user_input)[:2000], "response": str(response)[:4000]},
            )
        except Exception:
            pass

    def _bind_tools(self) -> Any:
        try:
            if hasattr(self.model, "bind_tools"):
                return self.model.bind_tools(self.tools)
            return self.model
        except Exception as exc:
            print_warning(tr("agent.warn_bind_tools", error=str(exc)))
            return self.model

    def _open_checkpointer(self) -> Optional[Any]:
        """打开 workspace 级 SqliteSaver（thread_id 隔离会话）。打不开就返回 None，
        调用方走同一工厂的无持久化模式——缺 sqlite 包的旧环境不能因此罢工。"""
        if not self.checkpoint_path:
            print_warning(tr("agent.warn_create_agent", error="no checkpoint_path: running without persistence"))
            return None
        try:
            import sqlite3

            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError:
            print_warning(tr("agent.warn_create_agent", error="langgraph.checkpoint.sqlite unavailable: running without persistence"))
            return None
        try:
            path = str(self.checkpoint_path)
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(path, check_same_thread=False)
            saver = SqliteSaver(conn)
            saver.setup()
            self._saver_conn = conn
            return saver
        except Exception as exc:
            print_warning(tr("agent.warn_create_agent", error=f"checkpointer: {exc}"))
            return None

    def _open_store(self) -> Optional[Any]:
        try:
            from langgraph.store.memory import InMemoryStore
        except ImportError:
            return None
        try:
            return InMemoryStore()
        except Exception:
            return None

    def _create_agent(self) -> Optional[Any]:
        # 单工厂：永远 create_agent。失败返回 None，调用方按旧语义处理。
        if create_langchain_agent is None:
            print_warning(tr("agent.warn_create_agent", error="langchain.agents.create_agent unavailable"))
            return None
        try:
            return self._create_graph_agent()
        except Exception as exc:
            print_warning(tr("agent.warn_create_agent", error=str(exc)))
            return None

    def _create_graph_agent(self) -> Optional[Any]:
        """单工厂：中间件洋葱 + 可选 checkpointer/store（无持久化也走这里）。"""
        from . import middleware as _middleware_factory
        from .middleware import (
            SayaHookMiddleware,
            SayaPermissionMiddleware,
            SayaSafetyMiddleware,
            SayaPromptMiddleware,
        )

        if not (
            hasattr(self.model_with_tools, "invoke") or callable(self.model_with_tools)
        ):
            print_warning(tr("agent.warn_create_agent", error="model does not implement LangChain invoke"))
            return None

        saver = self._open_checkpointer()
        if saver is None:
            # 无持久化：同一工厂继续跑，只是增量语义退化为每轮全量
            # （graph_enabled 为 False）。中间件仍在，不丢权限/安全。
            # 降级是行为变更，必须留痕：控制台警告 + 审计事件，排障时可回溯。
            print_warning(tr("agent.warn_create_agent", error="persistence unavailable: running without persistence (same factory, full messages each turn)"))
            try:
                from .audit import append_audit_event

                append_audit_event("agent", "graph_downgrade", allowed=True, details={"reason": "checkpointer_unavailable", "fallback": "ephemeral"})
            except Exception:
                pass
        self._saver = saver
        self._store = self._open_store()

        middlewares: List[Any] = [SayaHookMiddleware()]
        if self.permissions is not None:
            middlewares.append(SayaPermissionMiddleware(self.permissions))
        middlewares.append(SayaSafetyMiddleware())
        self.prompt_middleware = SayaPromptMiddleware()
        middlewares.append(self.prompt_middleware)
        # 明确 system prompt 归中间件所有，避免两处打架。
        self.prompt_middleware.refresh(self.system_prompt)
        # 瞬时失败先由官方重试中间件在图内退避（保住图进度）；
        # 外层 run() 整轮重试仍保留做兜底（覆盖 invoke 层以上的异常）。
        middlewares.extend(_middleware_factory.build_retry_middlewares())
        # 单轮调用上限正式挂载，无限工具循环必须被结束，而不是转到底。
        # 由流式与截断测试共同锁定。
        tool_limit, model_limit = _middleware_factory.build_guardrail_middlewares()
        middlewares.extend([tool_limit, model_limit])
        # 已知上下文窗口时挂一层工具结果剪枝，未知则禁用。
        # 注，官方调用上限在此版本下会导致流式文本重复，暂不挂载。
        # 单轮上限由守卫中间件提供，失控由上层重试恢复兜底，只挂一层。
        editing = _middleware_factory.build_context_editing_middleware(
            getattr(self.model, "context_window", 0) or 0
        )
        if editing is not None:
            middlewares.append(editing)

        kwargs: Dict[str, Any] = {
            "middleware": middlewares,
        }
        if saver is not None:
            kwargs["checkpointer"] = saver
        if self._store is not None:
            kwargs["store"] = self._store
        return create_langchain_agent(
            self.model_with_tools,
            self.tools,
            **kwargs,
        )


def message_to_chat_dict(message: MessageLike) -> Dict[str, str]:
    """将 LangChain 消息转换为本地 chat dict 格式。"""
    if isinstance(message, SystemMessage):
        role = "system"
    elif isinstance(message, AIMessage):
        role = "assistant"
    else:
        role = "user"

    return {"role": role, "content": content_to_text(message.content)}


def content_to_text(content: Any) -> str:
    """从常见的 LangChain 消息 content 结构中提取文本。"""
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                text_parts.append(str(block["text"]))
            elif hasattr(block, "type") and getattr(block, "type", None) == "text":
                text_value = getattr(block, "text", None)
                if text_value:
                    text_parts.append(str(text_value))
        return "\n".join(text_parts)

    if content is None:
        return ""

    return str(content)


def message_kind(msg: Any) -> str:
    """尽量稳定地识别 LangChain 消息类型。"""
    message_type = getattr(msg, "type", None)
    if message_type:
        return str(message_type).lower()
    return msg.__class__.__name__.lower()


def extract_tool_names(tool_calls: Any) -> List[str]:
    """从不同供应商的 tool_calls 结构中提取工具名。"""
    tool_names: list[str] = []
    for tool_call in tool_calls or []:
        name = None
        if isinstance(tool_call, dict):
            name = tool_call.get("name")
            if not name and isinstance(tool_call.get("function"), dict):
                name = tool_call["function"].get("name")
        else:
            name = getattr(tool_call, "name", None)
            function = getattr(tool_call, "function", None)
            if not name and isinstance(function, dict):
                name = function.get("name")
            elif not name and function is not None:
                name = getattr(function, "name", None)
        tool_names.append(str(name or "unknown"))
    return tool_names


__all__ = [
    "AgentRunner",
    "TurnTransition",
    "TurnState",
    "content_to_text",
    "extract_tool_names",
    "message_kind",
    "message_to_chat_dict",
]
