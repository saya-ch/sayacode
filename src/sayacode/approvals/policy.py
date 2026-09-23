"""会话信任策略与官方 HITL 的条件中断。

只读、询问、Jev 自动审理和完全信任共用一套工具边界。Jev 只替询问档
审理原本需要人工确认的调用，不能扩大权限；低置信度或服务异常仍交给人。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from ..config import TRUST_LEVELS, TrustLevel, normalize_trust
from ..paths import context_value, workspace_path

Action = Literal["allow", "ask", "deny"]
READ_TOOLS = frozenset(
    {
        "read_file",
        "list_directory",
        "glob_search",
        "grep_search",
        "read_output_file",
        "list_skills",
        "load_skill",
        "read_skill_resource",
        "search_memory",
        "analyze_project",
        "list_symbols",
        "git",
        "web_search",
        "write_todos",
        "task_status",
        "task_wait",
        "task_delivery",
        "send_message_to_subagent",
        "report_to_parent",
    }
)


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
            return PolicyDecision("deny", "Tool unavailable in read-only trust")
        if _call_key(name, arguments, context) in self.session_grants:
            return PolicyDecision("allow", "Exact call approved for this session", "session")
        return PolicyDecision("ask", "Side-effecting tool requires approval")


def policy_for(context: Any) -> Policy:
    """从上下文取出会话策略，拿不到就给默认询问策略。传入上下文，返回策略对象。返回的默认对象不要外存，记批准会丢。"""
    policy = context_value(context, "policy")
    return policy if isinstance(policy, Policy) else Policy()


__all__ = [
    "Policy",
    "PolicyDecision",
    "TrustLevel",
    "TRUST_LEVELS",
    "READ_TOOLS",
    "is_read_only",
    "normalize_trust",
    "policy_for",
]
