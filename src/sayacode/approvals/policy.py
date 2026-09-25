"""会话信任策略与官方 HITL 的条件中断。

只读、询问、工作区内自动改动和完全信任共用一套工具边界；
需要人工确认时交给官方 HITL。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..config import TRUST_LEVELS, TrustLevel, normalize_trust
from ..paths import context_value, workspace_path

Action = Literal["allow", "ask", "deny"]
FILE_MUTATIONS = frozenset({"write_file", "search_replace", "delete_file"})
READ_ONLY_ROLES = frozenset({"planner", "reviewer"})
QUERY_TOOLS = frozenset(
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
        "task_delivery",
    }
)
READ_ONLY_STATE_TOOLS = frozenset(
    {"write_todos", "task_status", "task_wait", "report_to_parent"}
)
READ_ONLY_ALLOWED_TOOLS = QUERY_TOOLS | READ_ONLY_STATE_TOOLS


def hooks_allowed(trust_level: str, agent_role: str) -> bool:
    """Hook 可执行本地命令，只在允许本机脚本的线程中启用。"""
    return trust_level in {"ask", "full"} and agent_role not in READ_ONLY_ROLES


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
    """判断调用是否可在只读档执行；待办和父子消息属于运行状态操作。"""
    if name in READ_ONLY_ALLOWED_TOOLS:
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
    """记录单线程信任档位和仅在询问档生效的精确调用授权。"""

    trust_level: TrustLevel = "ask"
    session_grants: set[str] = field(default_factory=set)

    def grant_call(self, name: str, arguments: Mapping[str, Any], context: Any) -> str:
        """记住一次精确调用，下次同形调用可直接放行。传入工具名参数和上下文，返回调用指纹。只在询问档位下调用，别的档位记了也用不上。"""
        key = _call_key(name, arguments, context)
        self.session_grants.add(key)
        return key

    def decide(self, name: str, arguments: Mapping[str, Any], context: Any) -> PolicyDecision:
        """先落实角色上限，再按当前线程档位判定一次工具调用。"""
        level = self.trust_level
        read_only = is_read_only(name, arguments)
        if context_value(context, "agent_role") in READ_ONLY_ROLES and not read_only:
            return PolicyDecision("deny", "规划和评审子 Agent 只能调用只读工具")
        if level == "full":
            return PolicyDecision("allow", "Full trust")
        if read_only:
            return PolicyDecision("allow", "Read-only tool")
        if level == "read_only":
            return PolicyDecision("deny", "Tool unavailable in read-only trust")
        if name == "send_message_to_subagent":
            return PolicyDecision("allow", "父子任务消息不需要工具审批")
        if level == "workspace_auto":
            if name in FILE_MUTATIONS:
                path = arguments.get("path")
                if not isinstance(path, str) or not path:
                    return PolicyDecision("deny", "文件写入必须提供路径")
                try:
                    target = workspace_path(context, path)
                    workspace = workspace_path(context)
                    parent = workspace_path(context, Path(path).parent)
                except (OSError, ValueError):
                    return PolicyDecision("deny", "文件写入路径无效")
                if not target.is_relative_to(workspace) or (
                    name == "delete_file" and not parent.is_relative_to(workspace)
                ):
                    return PolicyDecision("deny", "文件工具不能写入工作区外")
                return PolicyDecision("allow", "文件工具在工作区内自动放行")
            return PolicyDecision("ask", "此工具需要人工批准")
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
    "QUERY_TOOLS",
    "READ_ONLY_ALLOWED_TOOLS",
    "READ_ONLY_STATE_TOOLS",
    "READ_ONLY_ROLES",
    "FILE_MUTATIONS",
    "hooks_allowed",
    "is_read_only",
    "normalize_trust",
    "policy_for",
]
