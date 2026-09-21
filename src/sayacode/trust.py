"""会话信任等级共三种，由原生人工确认中间件支撑。判定先后是全放行先看，只读工具其次，只读档位再看，会话记住的精确调用最后看，剩下都要问人。拒绝只在执行阶段拦截，询问走原生中断等人拍板。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from langchain.agents.middleware import HumanInTheLoopMiddleware
from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage

from .config import TRUST_LEVELS, TrustLevel, normalize_trust

Action = Literal["allow", "ask", "deny"]
READ_TOOLS = frozenset(
    {
        "read_file",
        "list_directory",
        "glob_search",
        "grep_search",
        "read_output_file",
        "analyze_project",
        "list_symbols",
        "git",
        "web_search",
        "write_todos",
        "task_status",
        "task_wait",
        "task_delivery",
    }
)


def context_value(context: Any, name: str, default: Any = None) -> Any:
    """取出上下文里的命名值，字典和对象两种写法都认。传入上下文对象和名字，缺省给默认值。上下文形态不明时先用这个拿，不要直接点属性。"""
    return (
        context.get(name, default)
        if isinstance(context, Mapping)
        else getattr(context, name, default)
    )


def workspace_path(context: Any, value: str | Path = ".") -> Path:
    """以工作区为基准解析相对路径，绝对路径视为全局。传入上下文和待解路径，返回解析后的绝对路径。调用前保证上下文里有工作区，缺省会退到当前目录。"""
    root = Path(context_value(context, "workspace", Path.cwd())).expanduser().resolve()
    candidate = Path(value).expanduser()
    return (candidate if candidate.is_absolute() else root / candidate).resolve()


def _call_key(name: str, arguments: Mapping[str, Any], context: Any) -> str:
    # 把路径类参数先按工作区归一化，再连同工作区一起做摘要，这样同义写法能命中同一条记住的批准。
    normalized = dict(arguments)
    for key in ("path", "file_path", "root_dir", "cwd"):
        if value := normalized.get(key):
            normalized[key] = str(workspace_path(context, str(value)))
    identity = json.dumps(
        {"tool": name, "workspace": str(workspace_path(context)), "arguments": normalized},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def is_read_only(name: str, arguments: Mapping[str, Any]) -> bool:
    """判断工具调用是否只读无副作用。传入工具名和参数，返回真假。规划评审类的子智能体也算只读，新增写入工具要记得补进名单。"""
    if name in READ_TOOLS:
        return True
    return name == "delegate_to_subagent" and arguments.get("role", "planner") in {
        "planner",
        "reviewer",
    }


@dataclass(frozen=True)
class PolicyDecision:
    """一次权限判定的结果快照。动作只有放行询问拒绝三种，原因给人看，来源标明是信任档位还是会话记住。"""

    action: Action
    reason: str
    source: str = "trust"


@dataclass
class Policy:
    """记录单会话信任等级和已批准的精确调用。默认档位是询问，最保守。记住的调用只在同工作区同参数下有效，换会话就失效。"""

    trust_level: TrustLevel = "ask"
    session_grants: set[str] = field(default_factory=set)

    def grant_call(self, name: str, arguments: Mapping[str, Any], context: Any) -> str:
        """记住一次精确调用，下次同形调用可直接放行。传入工具名参数和上下文，返回调用指纹。只在询问档位下调用，别的档位记了也用不上。"""
        key = _call_key(name, arguments, context)
        self.session_grants.add(key)
        return key

    def decide(self, name: str, arguments: Mapping[str, Any], context: Any) -> PolicyDecision:
        """按优先级判定一次调用。传入工具名参数和上下文，返回放行询问或拒绝。顺序是全放行先过，只读工具放行，只读档位拦截非只读，会话记住的精确调用放行，最后都要问人。只读档位里的命令行工具例外，仍要问人。"""
        level = self.trust_level
        if level == "full":
            return PolicyDecision("allow", "Full trust")
        if is_read_only(name, arguments):
            return PolicyDecision("allow", "Read-only tool")
        if level == "read_only":
            if name == "execute_command_tool":
                return PolicyDecision("ask", "Shell requires approval even in read-only trust")
            return PolicyDecision("deny", "Tool unavailable in read-only trust")
        if _call_key(name, arguments, context) in self.session_grants:
            return PolicyDecision("allow", "Exact call approved for this session", "session")
        return PolicyDecision("ask", "Side-effecting tool requires approval")


def policy_for(context: Any) -> Policy:
    """从上下文取出会话策略，拿不到就给默认询问策略。传入上下文，返回策略对象。返回的默认对象不要外存，记批准会丢。"""
    policy = context_value(context, "policy")
    return policy if isinstance(policy, Policy) else Policy()


def decision_for(request: ToolCallRequest) -> PolicyDecision:
    """给中间件用的判定入口。传入原生工具调用请求，返回放行询问或拒绝。只做纯判定，不改状态不弹窗。"""
    return policy_for(request.runtime.context).decide(
        request.tool_call["name"], request.tool_call.get("args", {}), request.runtime.context
    )


class PolicyMiddleware(AgentMiddleware):
    """在原生确认之后，于执行阶段复核被拒绝的调用。只拦拒绝，不拦询问，询问留给原生中断处理。同步异步两条路逻辑一致，改一处要对齐另一处。"""

    @staticmethod
    def _denied(request: ToolCallRequest) -> ToolMessage | None:
        # 先做纯判定，非拒绝直接放过，只有拒绝才包成错误消息回给模型。
        decision = decision_for(request)
        if decision.action != "deny":
            return None
        return ToolMessage(
            content=decision.reason,
            status="error",
            name=request.tool_call["name"],
            tool_call_id=request.tool_call["id"],
            artifact={"action": "deny", "source": decision.source},
        )

    def wrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
        """同步执行拦截，被拒直接回错误消息，否则走原处理。传入请求和后继处理，返回工具消息或原结果。只处理拒绝，询问不在这里停。"""
        denied = self._denied(request)
        return denied if denied is not None else handler(request)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Any) -> Any:
        """异步执行拦截，行为与同步版一致。传入请求和后继处理，返回工具消息或原结果。异步链路实际走这一条，改逻辑要与同步版保持一致。"""
        denied = self._denied(request)
        return denied if denied is not None else await handler(request)


def build_approval_middleware(tools: list[Any]) -> HumanInTheLoopMiddleware:
    """按当前工具表装配原生询问中间件。传入工具对象表，返回中断配置。判定为询问的调用会中断等人拍板，只读搜索类工具常驻名单防止漏配。"""
    names = {item.name for item in tools} | {"glob_search", "grep_search", "write_todos"}
    return HumanInTheLoopMiddleware(
        interrupt_on={
            name: {
                "allowed_decisions": ["approve", "reject"],
                "when": lambda request: decision_for(request).action == "ask",
            }
            for name in names
        }
    )


__all__ = [
    "Policy",
    "PolicyDecision",
    "PolicyMiddleware",
    "TrustLevel",
    "TRUST_LEVELS",
    "READ_TOOLS",
    "build_approval_middleware",
    "context_value",
    "decision_for",
    "is_read_only",
    "normalize_trust",
    "policy_for",
    "workspace_path",
]
