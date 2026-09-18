"""
Tool permission policy engine — 参考 Claude Code PermissionMode / ToolPermissionContext.

实际生效的判定优先级（从高到低，实现见 PermissionRuntime._decide）：

0. **危险工具地板**：DANGEROUS_TOOLS 在**任何来源**解析出的 ``allow`` 都在
   ``PermissionRuntime._decide`` 这唯一决策出口被强制降级为 ``deny``。
   地板按**解析后的工具名**判定，因此 ``delete_*`` / ``*`` 这类通配规则、
   直接改写 session 字典、策略文件、session/mode 规则一视同仁。
1. mode 规则中的 ``deny`` —— 硬约束，模式（plan/review）注入的只读要求不可被绕开
2. session 授权 —— 用户在确认弹窗中选「始终允许（当前会话）」逐项累加的规则
3. mode 规则中的 ``allow`` / ``ask``
4. user / project 策略文件（project 覆盖 user），策略层内部的优先级见
   PermissionPolicy._decide_raw：
   **显式 tools deny > commands > paths > tools（支持 prefix* 通配）> default**
5. built-in 默认规则

两个必须区分的概念：

- **mode 规则**（mode_rules）：由 /mode 切换整体替换，不携带用户授权。
- **session 授权**（session_rules）：确认弹窗逐项累加，切换 mode 不清空。

二者历史上共用同一个 dict，导致「切一次 /mode build 清空全部会话授权」。
它们现在由 SessionPermissionState 承载，并在同一进程的所有 runtime 之间共享
（**共享引用**，PermissionRuntime() 默认也复用它），因此不再依赖
「先设模式还是先建 runtime」的调用顺序，也不存在只拿到私有快照的 fail-open 运行时。

危险工具在任何来源下的 ``allow`` 都会被强制降级为 ``deny``，
降级来源记入 stripped_dangerous，并会出现在 /permissions 摘要里；
``/permissions reset``（clear_session_rules）会连同会话授权一起清除这些记录。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional
import json
import re

from .audit import append_audit_event
from ..i18n import tr
from .paths import SayacodePaths
from .private_io import write_private_json


PermissionAction = str
VALID_ACTIONS = {"allow", "ask", "deny"}

# 权限来源类型
PermissionSource = str
SOURCE_USER = "user"
SOURCE_PROJECT = "project"
SOURCE_SESSION = "session"
SOURCE_BUILTIN = "built-in"

# 特别危险工具集合（可以 allow → force-deny）
DANGEROUS_TOOLS = {
    "delete_file",
    "git_push",
}

READ_ONLY_TOOLS = {
    "read_file",
    "glob_search",
    "grep_search",
    "list_directory",
    "analyze_project",
    "get_project_summary",
    "list_project_files",
    "get_file_info",
    "list_symbols",
    "find_symbol",
    "git_status",
    "git_diff",
    "git_log",
    "git_branch",
    "git_remote",
    "check_command_safety_tool",
    "get_system_info",
    "list_environment_variables",
    "read_output_file",
    "web_search",
}

SAFE_WRITE_TOOLS = {
    "write_file",
    "search_replace",
    "create_directory",
    "batch_edit",
}

SAFE_GIT_TOOLS = {
    "git_add",
    "git_commit",
}

DIRECT_MUTATING_TOOLS = {
    "execute_command_tool",
    "git_checkout",
    "git_pull",
    "git_stash",
}

ASK_TOOLS = {
    "delete_file",
    "git_push",
}

RESTRICTED_TOOLS = ASK_TOOLS
MUTATING_TOOLS = SAFE_WRITE_TOOLS | SAFE_GIT_TOOLS | DIRECT_MUTATING_TOOLS | ASK_TOOLS

DEFAULT_COMMAND_RULES: Dict[str, PermissionAction] = {
    # 说明 Shell 命令默认放宽，但绕行命令仍需确认。
    "git push*": "ask",
    "git reset*": "ask",
    "git clean*": "ask",
    "git checkout*": "ask",
    "git switch*": "ask",
    "rm *": "ask",
    "del *": "ask",
    "erase *": "ask",
    "rd *": "ask",
    "rmdir *": "ask",
    "remove-item *": "ask",
}

DEFAULT_TOOL_RULES: Dict[str, PermissionAction] = {
    **{name: "allow" for name in READ_ONLY_TOOLS},
    **{name: "allow" for name in SAFE_WRITE_TOOLS},
    **{name: "allow" for name in SAFE_GIT_TOOLS},
    **{name: "allow" for name in DIRECT_MUTATING_TOOLS},
    **{name: "ask" for name in ASK_TOOLS},
}

PATH_ARGUMENT_KEYS = {
    "path",
    "file_path",
    "directory",
    "dir_path",
    "target_path",
    "source_path",
    "src",
    "dst",
    "cwd",
}


@dataclass(frozen=True)
class PermissionRequest:
    """权限询问载荷。"""

    tool_name: str
    action: PermissionAction
    arguments_preview: str
    source: str


@dataclass(frozen=True)
class PermissionDecision:
    """权限检查结果。"""

    allowed: bool
    action: PermissionAction
    reason: str
    source: str


# ==============================================================================
# 会话权限状态 — session 授权 / mode 规则 / 回退态
# ==============================================================================


@dataclass
class SessionPermissionState:
    """进程级共享的会话权限状态（session 授权 + mode 规则 + 回退态）。

    这三者属于「会话」而非「工作区」：工作区切换只重载 user/project 策略文件，
    不应影响用户已授予的会话权限。因此同一进程内所有 PermissionRuntime 共享
    同一个实例（是共享而非快照拷贝），从而消除「先设模式还是先建 runtime」
    的顺序依赖。

    字段说明：

    - ``session_rules``：确认弹窗逐项累加的用户授权，切换 mode 时不清空。
    - ``mode_rules``：由 /mode 整体替换的模式规则；其中 deny 是硬约束。
    - ``stripped_dangerous``：记录哪些危险工具的 allow 被强制降级为 deny。
    """

    session_rules: Dict[str, PermissionAction] = field(default_factory=dict)
    session_rule_source: str = "session"
    mode_rules: Dict[str, PermissionAction] = field(default_factory=dict)
    mode_rule_source: str = ""
    is_in_fallback: bool = False
    stripped_dangerous: Dict[PermissionSource, List[str]] = field(default_factory=dict)
    # 图中断批准的一次性放行：放共享状态里，与 session 授权同级共享。
    # 中间件手里的 runtime 和工具体内联 check 用的未必是同一个 PermissionRuntime
    # 实例（contextvars 按轮切换），放实例上会导致批准对内联检查不可见 → 双弹窗。
    one_shot_grants: set = field(default_factory=set)

    def record_stripped(self, source: PermissionSource, tool_name: str) -> None:
        """记录一次危险工具 allow → deny 的强制降级。"""
        bucket = self.stripped_dangerous.setdefault(str(source or "session"), [])
        if tool_name not in bucket:
            bucket.append(tool_name)

    def clear_stripped(self) -> None:
        """清除危险工具降级记录。

        这些记录描述的是「已被剥离的 allow 规则」；会话授权被清空后它们不再
        对应任何生效规则，继续保留只会让 /permissions 打印过期提示。
        """
        self.stripped_dangerous = {}

    def has_stripped_dangerous(self) -> bool:
        """是否有危险工具规则被剥离。"""
        return any(tools for tools in self.stripped_dangerous.values() if tools)

    def get_stripped_summary(self) -> str:
        """危险规则剥离摘要。"""
        if not self.has_stripped_dangerous():
            return ""
        lines = ["危险工具已强制降级为 deny:"]
        for source, tools in self.stripped_dangerous.items():
            if tools:
                lines.append(f"  [{source}] {', '.join(sorted(tools))}")
        return "\n".join(lines)


# 进程级共享的会话权限状态：PermissionRuntime() 默认复用它，而不是新建私有快照。
# 私有快照会让 `with permission_runtime_session(PermissionRuntime())` 丢掉全部
# 补充 mode deny 会 fail-open，共享须为默认语义。
_SHARED_SESSION_STATE = SessionPermissionState()


class PermissionPolicy:
    """合并后的 user/project 工具权限策略。"""

    def __init__(
        self,
        workspace: Optional[Path] = None,
        default_action: PermissionAction = "ask",
        tool_rules: Optional[Dict[str, PermissionAction]] = None,
        path_rules: Optional[Dict[str, PermissionAction]] = None,
        command_rules: Optional[Dict[str, PermissionAction]] = None,
        sources: Optional[Dict[str, str]] = None,
        path_sources: Optional[Dict[str, str]] = None,
        command_sources: Optional[Dict[str, str]] = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve() if workspace else None
        self.default_action = _normalize_action(default_action, fallback="ask")
        self.tool_rules = dict(DEFAULT_TOOL_RULES)
        # 策略文件里手写的危险工具 allow 同样强制降级为 deny。
        # 否则「任何能写 ~/.sayacode/permissions.json 的东西都能静默提权」。
        self.stripped_dangerous: List[str] = []
        if tool_rules:
            for tool_name, action in tool_rules.items():
                normalized_action = _normalize_action(action, fallback="")
                if not normalized_action:
                    continue
                if normalized_action == "allow" and str(tool_name) in DANGEROUS_TOOLS:
                    self.tool_rules[str(tool_name)] = "deny"
                    self.stripped_dangerous.append(str(tool_name))
                    continue
                self.tool_rules[str(tool_name)] = normalized_action
        self.path_rules: Dict[str, PermissionAction] = {}
        if path_rules:
            for pattern, action in path_rules.items():
                normalized_action = _normalize_action(action, fallback="")
                if normalized_action:
                    self.path_rules[str(pattern)] = normalized_action
        self.command_rules: Dict[str, PermissionAction] = dict(DEFAULT_COMMAND_RULES)
        if command_rules:
            for pattern, action in command_rules.items():
                normalized_action = _normalize_action(action, fallback="")
                if normalized_action:
                    self.command_rules[str(pattern)] = normalized_action
        self.sources = sources or {}
        self.path_sources = path_sources or {}
        self.command_sources = {
            **{pattern: "built-in" for pattern in DEFAULT_COMMAND_RULES},
            **(command_sources or {}),
        }
        # 说明策略文件 tools 键优先于内置精确键，避免通配被抵消。
        # 补充 delete_file 等精确键不能抢占通配键前面，
        # 否则 `git_*: deny` 会被 built-in 的 `git_push: allow` 抵消 ——
        # 摘要显示 deny，实际行为却是 allow（F9）。
        self._policy_rule_keys = {
            str(name) for name, source in self.sources.items()
            if str(source) != "built-in"
        }

    def _match_tool_rule(
        self,
        tool_name: str,
    ) -> Optional[tuple[str, PermissionAction]]:
        """匹配 tools 规则，返回命中的 (规则键, 动作)。

        策略文件的键（精确或 ``prefix*`` 通配）优先于 built-in 默认键；
        同一层内精确优先于通配。
        """
        if self._policy_rule_keys:
            explicit = _match_rule_with_key(
                {
                    name: action
                    for name, action in self.tool_rules.items()
                    if name in self._policy_rule_keys
                },
                tool_name,
            )
            if explicit:
                return explicit
        return _match_rule_with_key(self.tool_rules, tool_name)

    @classmethod
    def load(cls, workspace: Optional[Path] = None) -> "PermissionPolicy":
        """加载 built-in、user、project 三层策略。"""
        merged_rules: Dict[str, PermissionAction] = {}
        merged_path_rules: Dict[str, PermissionAction] = {}
        merged_command_rules: Dict[str, PermissionAction] = {}
        sources: Dict[str, str] = {name: "built-in" for name in DEFAULT_TOOL_RULES}
        path_sources: Dict[str, str] = {}
        command_sources: Dict[str, str] = {}
        default_action: PermissionAction = "ask"

        for label, path in _policy_paths(workspace):
            data = _read_policy_file(path)
            if not data:
                continue

            candidate_default = _normalize_action(data.get("default"), fallback="")
            if candidate_default:
                default_action = candidate_default

            for tool_name, action in (data.get("tools") or {}).items():
                normalized_action = _normalize_action(action, fallback="")
                if normalized_action:
                    merged_rules[str(tool_name)] = normalized_action
                    sources[str(tool_name)] = label

            for pattern, action in (data.get("paths") or {}).items():
                normalized_action = _normalize_action(action, fallback="")
                if normalized_action:
                    merged_path_rules[str(pattern)] = normalized_action
                    path_sources[str(pattern)] = label

            for pattern, action in (data.get("commands") or {}).items():
                normalized_action = _normalize_action(action, fallback="")
                if normalized_action:
                    merged_command_rules[str(pattern)] = normalized_action
                    command_sources[str(pattern)] = label

        return cls(
            workspace=workspace,
            default_action=default_action,
            tool_rules=merged_rules,
            path_rules=merged_path_rules,
            command_rules=merged_command_rules,
            sources=sources,
            path_sources=path_sources,
            command_sources=command_sources,
        )

    def decide(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> PermissionDecision:
        """返回该工具按当前配置应执行的动作。

        危险工具的最后一道地板在这里统一兜底：无论 allow 来自 tools 规则、
        paths 规则还是 commands 规则，都会被强制降级为 deny。
        （只在 __init__ 里过滤 tools 规则是不够的 —— path 规则会在
        _decide_path_rule 阶段提前返回 allow，从而绕过工具级检查。）
        """
        decision = self._decide_raw(tool_name, arguments or {})
        if decision.action == "allow" and tool_name in DANGEROUS_TOOLS:
            if tool_name not in self.stripped_dangerous:
                self.stripped_dangerous.append(tool_name)
            return PermissionDecision(
                allowed=False,
                action="deny",
                reason=f"{tool_name}: deny (危险工具不允许自动放行)",
                source=decision.source,
            )
        return decision

    def _decide_raw(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> PermissionDecision:
        """解析策略文件的动作，优先级（高 → 低）：

        1. **显式 ``tools: deny``** —— 策略文件里手写的拒绝是硬约束：不得被
           ``paths: {"**": "allow"}``（例如 shell 工具传入的 ``cwd``）或
           ``commands`` 规则放宽，否则一条宽泛的 path allow 就能让显式工具的
           deny 形同虚设。
        2. ``commands`` 规则 —— 针对命令内容，比宽泛的 path 规则更具体。
        3. ``paths`` 规则。
        4. ``tools`` 规则（精确优先，其次 ``prefix*`` 通配）。
        5. ``default``。
        """
        explicit_deny = self._decide_explicit_tool_deny(tool_name)
        if explicit_deny:
            return explicit_deny

        command_decision = self._decide_command_rule(tool_name, arguments)
        if command_decision:
            return command_decision

        path_decision = self._decide_path_rule(tool_name, arguments)
        if path_decision:
            return path_decision

        matched = self._match_tool_rule(tool_name)
        if matched:
            pattern, action = matched
            source = self.sources.get(pattern, "default")
        else:
            action = self.default_action
            source = "default"
        return PermissionDecision(
            allowed=action == "allow",
            action=action,
            reason=f"{tool_name}: {action}",
            source=source,
        )

    def _decide_explicit_tool_deny(
        self,
        tool_name: str,
    ) -> Optional[PermissionDecision]:
        """策略文件里显式写下的 ``tools: deny``（不含 built-in 默认值）。"""
        matched = self._match_tool_rule(tool_name)
        if not matched:
            return None
        pattern, action = matched
        if action != "deny" or self.sources.get(pattern) == "built-in":
            return None
        return PermissionDecision(
            allowed=False,
            action="deny",
            reason=f"{tool_name}: deny",
            source=self.sources.get(pattern, "policy"),
        )

    def to_dict(self) -> Dict[str, Any]:
        """序列化生效后的策略。"""
        return {
            "default": self.default_action,
            "tools": dict(sorted(self.tool_rules.items())),
            "paths": dict(sorted(self.path_rules.items())),
            "commands": dict(sorted(self.command_rules.items())),
        }

    def _decide_path_rule(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Optional[PermissionDecision]:
        if not self.path_rules:
            return None

        for value in _extract_argument_paths(arguments):
            normalized_path = _normalize_path_for_match(value)
            for pattern, action in self.path_rules.items():
                if _path_pattern_matches(pattern, normalized_path):
                    source = self.path_sources.get(pattern, "path")
                    return PermissionDecision(
                        allowed=action == "allow",
                        action=action,
                        reason=f"{tool_name}: {action} for path {pattern}",
                        source=source,
                    )
        return None

    def _decide_command_rule(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Optional[PermissionDecision]:
        if not self.command_rules:
            return None

        command = str(arguments.get("command") or "").strip()
        if not command:
            return None

        segments = [s.strip().lower() for s in re.split(r"(?:&&|\|\||;|\|)", command) if s.strip()]
        if not segments:
            return None
        for segment in segments:
            for pattern, action in self.command_rules.items():
                if fnmatch(segment, pattern.lower()):
                    source = self.command_sources.get(pattern, "command")
                    return PermissionDecision(
                        allowed=action == "allow",
                        action=action,
                        reason=f"{tool_name}: {action} for command {pattern}",
                        source=source,
                    )
        return None


class PermissionRuntime:
    """进程级运行时策略，以及可选的交互式确认回调。

    判定优先级见模块 docstring：mode deny > session 授权 > mode allow/ask > 策略文件。

    会话级状态（session 授权 / mode 规则 / 回退态）由 SessionPermissionState 承载，
    在同一进程的所有 runtime 之间共享；本类只额外持有工作区相关的策略与审计日志。
    """

    def __init__(self, session: Optional[SessionPermissionState] = None) -> None:
        self.workspace: Optional[Path] = None
        self.policy = PermissionPolicy.load(None)
        self.confirm_callback: Optional[Callable[[PermissionRequest], bool]] = None
        self.audit_log: list[Dict[str, Any]] = []
        # 会话状态在所有 runtime 之间共享：工作区切换不重置用户授权，
        # 也不依赖「先设模式还是先建 runtime」的顺序。默认（不传 session）时
        # 说明新 runtime 会丢掉全部 mode deny，故复用共享状态。
        self.session = session if session is not None else _SHARED_SESSION_STATE

    # 划分会话兼容访问器。
    # 历史代码直接读写 runtime.session_rules / session_rule_source / is_in_fallback，
    # 这里保留同名属性并转发到共享状态，避免调用点散落改动。

    @property
    def session_rules(self) -> Dict[str, PermissionAction]:
        """返回会话授权规则。"""
        return self.session.session_rules

    @session_rules.setter
    def session_rules(self, value: Optional[Dict[str, PermissionAction]]) -> None:
        """整体替换会话授权并归一化。"""
        # 直接赋值同样要过一遍归一化：否则 `runtime.session_rules = {...}` 会绕过
        # 危险工具降级，成为一条静默提权路径（F2）。
        self.session.session_rules = self._normalize_rules(
            value, self.session.session_rule_source or "session"
        )

    @property
    def session_rule_source(self) -> str:
        """返回会话授权来源。"""
        return self.session.session_rule_source

    @session_rule_source.setter
    def session_rule_source(self, value: str) -> None:
        """设置会话授权来源。"""
        self.session.session_rule_source = value

    @property
    def is_in_fallback(self) -> bool:
        """返回是否处于回退询问模式。"""
        return self.session.is_in_fallback

    @is_in_fallback.setter
    def is_in_fallback(self, value: bool) -> None:
        """设置回退询问模式开关。"""
        self.session.is_in_fallback = value

    def configure_workspace(self, workspace: str | Path) -> None:
        """加载指定工作区的权限策略。"""
        self.workspace = Path(workspace).expanduser().resolve()
        self.policy = PermissionPolicy.load(self.workspace)

    def set_confirm_callback(self, callback: Optional[Callable[[PermissionRequest], bool]]) -> None:
        """设置权限确认弹窗回调。"""
        self.confirm_callback = callback

    def set_session_rules(
        self,
        rules: Optional[Dict[str, PermissionAction]],
        source: str = "session",
    ) -> None:
        """**整体替换** session 授权。

        本方法只服务于「确认弹窗授予的会话授权」。mode 规则请用
        set_mode_rules()，否则会清空用户已授予的会话权限（历史 B1/B3 缺陷）。
        """
        self.session.session_rule_source = str(source or "session")
        self.session_rules = rules

    def clear_session_rules(self) -> None:
        """清除用户授予的会话授权（保留 mode 规则与工作区策略文件）。

        会话授权此前只能靠进程重启撤销：一次误点的「会话始终允许」
        会一直生效，且没有任何 CLI 入口可以清掉（F5）。

        这是 ``/permissions reset`` 的实现。若需要「连 mode 规则一起清空」的
        彻底重置（仅测试/隔离场景），请用模块级
        :func:`reset_session_permission_rules` —— 用它替代本方法会把
        plan/review 模式的只读约束一并抹掉。
        """
        self.session.session_rules = {}
        self.session.session_rule_source = "session"
        # 降级记录描述的是被剥离的 allow 规则；授权清空后继续保留就是过期提示（F8）。
        self.session.clear_stripped()
        # 未消耗的一次性批准同样是会话级授权：reset 后继续保留等于留了一个后门。
        self.session.one_shot_grants.clear()

    def update_session_rules(
        self,
        rules: Optional[Dict[str, PermissionAction]],
        source: str = "",
    ) -> None:
        """**合并** session 授权，保留已有规则。"""
        merged = dict(self.session.session_rules)
        merged.update(rules or {})
        self.session.session_rules = self._normalize_rules(
            merged, source or self.session.session_rule_source or "session"
        )
        if source:
            self.session.session_rule_source = str(source)

    def set_mode_rules(
        self,
        rules: Optional[Dict[str, PermissionAction]],
        source: str = "mode",
    ) -> None:
        """**整体替换** mode 规则，不影响 session 授权。"""
        self.session.mode_rules = self._normalize_rules(rules, source)
        self.session.mode_rule_source = str(source or "mode")

    def clear_mode_rules(self) -> None:
        """清除 mode 规则（回到无模式约束）。"""
        self.session.mode_rules = {}
        self.session.mode_rule_source = ""

    def _normalize_rules(
        self,
        rules: Optional[Dict[str, PermissionAction]],
        source: str,
    ) -> Dict[str, PermissionAction]:
        """归一化规则，并把危险工具的 allow 强制降级为 deny。"""
        normalized: Dict[str, PermissionAction] = {}
        for tool_name, action in (rules or {}).items():
            normalized_action = _normalize_action(action, fallback="")
            if not normalized_action:
                continue
            if normalized_action == "allow" and str(tool_name) in DANGEROUS_TOOLS:
                normalized[str(tool_name)] = "deny"
                self.session.record_stripped(source or "session", str(tool_name))
                continue
            normalized[str(tool_name)] = normalized_action
        return normalized

    def check(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> PermissionDecision:
        """判定工具调用并处理确认与回退。"""
        decision = self._decide(tool_name, arguments or {})

        # 回退模式：连续拒绝达阈值后，逐项询问。把 allow 升级为 ask，
        # 使确认回调必须介入；无回调时 check() 后续逻辑会 fail closed 拒绝。
        decision = self._apply_fallback(decision, tool_name)

        if decision.action == "allow":
            self._record(tool_name, decision, arguments, allowed=True)
            return decision

        if decision.action == "deny":
            blocked = self.record_blocked(tool_name, arguments, decision.source)
            return blocked

        if is_parallel_batch():
            denied = PermissionDecision(
                allowed=False,
                action="deny",
                reason=f"Permission denied for tool '{tool_name}' by {decision.source} policy (并行批内禁止弹窗，ask fail-closed)。",
                source=decision.source,
            )
            self._record(tool_name, denied, arguments, allowed=False)
            return denied

        request = PermissionRequest(
            tool_name=tool_name,
            action=decision.action,
            arguments_preview=summarize_arguments(arguments or {}),
            source=decision.source,
        )

        if self.confirm_callback and self.confirm_callback(request):
            allowed = PermissionDecision(
                allowed=True,
                action="ask",
                reason=f"Permission granted for tool '{tool_name}' by user confirmation.",
                source=decision.source,
            )
            self._record(tool_name, allowed, arguments, allowed=True)
            return allowed

        denied = PermissionDecision(
            allowed=False,
            action="ask",
            reason=(
                f"Permission required for tool '{tool_name}'. "
                "Run /permissions to inspect or change tool policy."
            ),
            source=decision.source,
        )
        self._record(tool_name, denied, arguments, allowed=False)
        return denied

    def _apply_fallback(
        self,
        decision: PermissionDecision,
        tool_name: str,
    ) -> PermissionDecision:
        """回退模式下把 allow 升级为 ask，使该操作必须逐项确认。

        deny 保持不变（回退模式只收紧、不放宽）。
        """
        if not self.is_in_fallback or decision.action != "allow":
            return decision
        return PermissionDecision(
            allowed=False,
            action="ask",
            reason=f"{tool_name}: ask (连续拒绝回退模式)",
            source=decision.source,
        )

    def _decide(self, tool_name: str, arguments: Dict[str, Any]) -> PermissionDecision:
        """按文档化优先级判定，并在唯一出口施加危险工具地板。"""
        decision = self._decide_by_priority(tool_name, arguments)
        if decision.action != "deny" and str(tool_name) in self.session.one_shot_grants:
            # 一次性批准：用户在图中断里显式放过这一次。用后即焚。
            # 放共享状态里——中间件手里的 runtime 和工具体内联 check 用的未必是同一个
            # 强调 mode deny 照样赢，确认从不覆盖 deny。
            self.session.one_shot_grants.discard(str(tool_name))
            return PermissionDecision(
                allowed=True,
                action="allow",
                reason=f"{tool_name}: allow (one-shot interrupt grant)",
                source="interrupt",
            )
        return self._apply_dangerous_floor(tool_name, decision)

    def peek(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> PermissionDecision:
        """只判定、不弹窗：给图中间件用的只读决策。

        等价于 ``check()`` 去掉确认回调的那一半（decide + 回退，不调 confirm_callback）。
        中间件 ask 时走框架 ``interrupt()``，而不是在这里同步弹窗 —— 同步弹窗会卡住
        图执行，还会和恢复后的内联 check 形成双弹窗。
        """
        decision = self._decide(tool_name, arguments or {})
        return self._apply_fallback(decision, tool_name)

    def grant_once(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> None:
        """记录一次图中断批准，下次判定直接放行并消耗（共享状态，跨实例可见）。"""
        self.session.one_shot_grants.add(str(tool_name))
        try:
            self._record(
                tool_name,
                PermissionDecision(
                    allowed=True,
                    action="allow",
                    reason=f"{tool_name}: allow (one-shot interrupt grant)",
                    source="interrupt",
                ),
                arguments,
                allowed=True,
            )
        except Exception:
            pass

    def record_blocked(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]],
        source: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> PermissionDecision:
        """构造与 ``check()`` deny 分支完全一致的拒绝决策并记审计。

        给中间件短路用：handler 没跑、工具体内联 check 没跑，审计不能丢。
        """
        blocked = PermissionDecision(
            allowed=False,
            action="deny",
            reason=f"Permission denied for tool '{tool_name}' by {source} policy.",
            source=source,
        )
        self._record(tool_name, blocked, arguments, allowed=False, extra=extra)
        return blocked

    def _apply_dangerous_floor(
        self,
        tool_name: str,
        decision: PermissionDecision,
    ) -> PermissionDecision:
        """危险工具地板：任何来源解析出的 ``allow`` 一律降级为 ``deny``。

        地板必须收敛在**唯一决策出口**、并按**解析后的工具名**判定：
        ``_normalize_rules`` 只能看到规则键，``delete_*`` / ``*`` 这类通配键
        会绕开它；``session.session_rules`` / ``session.mode_rules`` 是普通
        dict，可以被直接改写；策略文件更是进程外输入。逐个来源设防必然漏，
        所以只在出口兜一次。
        """
        if decision.action != "allow" or str(tool_name) not in DANGEROUS_TOOLS:
            return decision
        self.session.record_stripped(decision.source or "session", str(tool_name))
        return PermissionDecision(
            allowed=False,
            action="deny",
            reason=f"{tool_name}: deny (危险工具不允许自动放行)",
            source=decision.source,
        )

    def _decide_by_priority(self, tool_name: str, arguments: Dict[str, Any]) -> PermissionDecision:
        """按文档化优先级判定：mode deny > session 授权 > mode allow/ask > 策略文件。"""
        mode_action = _match_rule(self.session.mode_rules, tool_name)
        mode_source = self.session.mode_rule_source or "mode"

        # 列举 1：mode deny 为硬约束，不可被会话绕开。
        if mode_action == "deny":
            return PermissionDecision(
                allowed=False,
                action="deny",
                reason=f"{tool_name}: deny",
                source=mode_source,
            )

        # 列举 2：session 授权来自用户确认选择。
        session_action = _match_rule(self.session.session_rules, tool_name)
        if session_action:
            return PermissionDecision(
                allowed=session_action == "allow",
                action=session_action,
                reason=f"{tool_name}: {session_action}",
                source=self.session.session_rule_source,
            )

        # 列举 3：mode allow 与 ask 次之。
        if mode_action:
            return PermissionDecision(
                allowed=mode_action == "allow",
                action=mode_action,
                reason=f"{tool_name}: {mode_action}",
                source=mode_source,
            )

        # 列举 4：user 与 project 策略及默认兜底。
        return self.policy.decide(tool_name, arguments)

    def _record(
        self,
        tool_name: str,
        decision: PermissionDecision,
        arguments: Optional[Dict[str, Any]],
        allowed: bool,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        entry = {
            "tool": tool_name,
            "action": decision.action,
            "allowed": allowed,
            "source": decision.source,
            "arguments": summarize_arguments(arguments or {}),
        }
        if extra:
            entry.update(dict(extra))
        self.audit_log.append(entry)
        if len(self.audit_log) > 200:
            self.audit_log = self.audit_log[-100:]
        append_audit_event(
            "permission",
            tool_name,
            workspace=self.workspace,
            allowed=allowed,
            details=entry,
        )


def summarize_arguments(arguments: Dict[str, Any]) -> str:
    """生成紧凑且已脱敏的参数预览。"""
    if not arguments:
        return "{}"

    redacted: Dict[str, Any] = {}
    for key, value in arguments.items():
        key_text = str(key)
        if _is_sensitive_key(key_text):
            redacted[key_text] = "***"
            continue
        value_text = str(value)
        redacted[key_text] = value_text[:160] + ("..." if len(value_text) > 160 else "")

    return json.dumps(redacted, ensure_ascii=False, sort_keys=True)


def _policy_paths(workspace: Optional[Path]) -> list[tuple[str, Path]]:
    sayacode_paths = SayacodePaths.resolve(create=False)
    paths = [("user", sayacode_paths.user_permissions)]
    if workspace:
        paths.append(("project", sayacode_paths.project_permissions(workspace)))
    return paths


def _read_policy_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _normalize_action(value: Any, fallback: PermissionAction) -> PermissionAction:
    action = str(value or "").strip().lower()
    return action if action in VALID_ACTIONS else fallback


def _match_rule_with_key(
    rules: Dict[str, PermissionAction],
    tool_name: str,
) -> Optional[tuple[str, PermissionAction]]:
    """在规则字典中查找工具动作，返回命中的 (规则键, 动作)。

    精确匹配优先，其次 `prefix*` 通配。策略文件的 tools 规则和 session/mode
    规则共用同一套匹配语义，避免「摘要里显示 mcp_*: deny，实际 decide 却是 allow」。
    """
    if tool_name in rules:
        return tool_name, rules[tool_name]
    for pattern, action in rules.items():
        if pattern.endswith("*") and tool_name.startswith(pattern[:-1]):
            return pattern, action
    return None


def _match_rule(
    rules: Dict[str, PermissionAction],
    tool_name: str,
) -> Optional[PermissionAction]:
    """在规则字典中查找工具动作（精确匹配或 `prefix*` 通配）的结果。"""
    matched = _match_rule_with_key(rules, tool_name)
    return matched[1] if matched else None


def _is_sensitive_key(key: str) -> bool:
    normalized = key.upper()
    return any(marker in normalized for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH"))


def _extract_argument_paths(arguments: Dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key, value in arguments.items():
        if str(key) not in PATH_ARGUMENT_KEYS:
            continue
        if isinstance(value, (list, tuple, set)):
            paths.extend(str(item) for item in value if item)
        elif value:
            paths.append(str(value))
    return paths


def _normalize_path_for_match(path: str) -> str:
    return str(path).replace("\\", "/").strip()


def _path_pattern_matches(pattern: str, path: str) -> bool:
    normalized_pattern = _normalize_path_for_match(pattern)
    candidates = {path, path.lower()}
    pattern_candidates = {normalized_pattern, normalized_pattern.lower()}
    return any(
        fnmatch(candidate, pattern_candidate)
        for candidate in candidates
        for pattern_candidate in pattern_candidates
    )


_RUNTIME = PermissionRuntime()
_RUNTIME_CONTEXT: ContextVar[PermissionRuntime | None] = ContextVar(
    "sayacode_permission_runtime",
    default=None,
)
_PARALLEL_BATCH: ContextVar[bool] = ContextVar("sayacode_parallel_batch", default=False)


def is_parallel_batch() -> bool:
    """是否处于并行批内（后台线程弹不了确认窗，ask 一律 fail-closed）。"""
    return bool(_PARALLEL_BATCH.get())


@contextmanager
def parallel_batch_session() -> Iterator[None]:
    """进入并行批：ask 不弹窗直接拒绝。"""
    token = _PARALLEL_BATCH.set(True)
    try:
        yield
    finally:
        _PARALLEL_BATCH.reset(token)


def _active_runtime() -> PermissionRuntime:
    return _RUNTIME_CONTEXT.get() or _RUNTIME


def create_permission_runtime(workspace: str | Path) -> PermissionRuntime:
    """为单个工作区创建运行时级权限引擎。

    会话状态（session 授权 / mode 规则 / 回退态）是**共享引用**而非快照拷贝：
    工作区只影响 user/project 策略文件，不应重置用户授权；共享也消除了
    「先设模式还是先建 runtime」的顺序耦合（历史 B4 脆弱耦合）。
    """
    base_runtime = _active_runtime()
    runtime = PermissionRuntime(session=base_runtime.session)
    runtime.configure_workspace(workspace)
    runtime.confirm_callback = base_runtime.confirm_callback
    return runtime


@contextmanager
def permission_runtime_session(runtime: PermissionRuntime) -> Iterator[PermissionRuntime]:
    """在当前执行上下文中使用指定的权限运行时。"""
    token = _RUNTIME_CONTEXT.set(runtime)
    try:
        yield runtime
    finally:
        _RUNTIME_CONTEXT.reset(token)


@contextmanager
def permission_workspace_session(workspace: str | Path) -> Iterator[PermissionRuntime]:
    """把权限检查绑定到某个工作区，作用于当前执行上下文。"""
    base_runtime = _active_runtime()
    runtime = create_permission_runtime(workspace)
    runtime.audit_log = base_runtime.audit_log
    token = _RUNTIME_CONTEXT.set(runtime)
    try:
        yield runtime
    finally:
        _RUNTIME_CONTEXT.reset(token)


def configure_permission_workspace(workspace: str | Path) -> None:
    """为工作区重新加载权限策略。"""
    _active_runtime().configure_workspace(workspace)


def get_permission_workspace() -> Optional[Path]:
    """返回当前生效的权限工作区。"""
    return _active_runtime().workspace


def restore_permission_workspace(workspace: Optional[str | Path]) -> None:
    """把权限运行时恢复到先前的工作区。"""
    runtime = _active_runtime()
    if workspace is None:
        runtime.workspace = None
        runtime.policy = PermissionPolicy.load(None)
        return
    runtime.configure_workspace(workspace)


def set_permission_confirm_callback(
    callback: Optional[Callable[[PermissionRequest], bool]]
) -> None:
    """设置交互式确认回调。"""
    _active_runtime().set_confirm_callback(callback)


def set_session_permission_rules(
    rules: Optional[Dict[str, PermissionAction]],
    source: str = "session",
) -> None:
    """**整体替换**进程内 session 授权（最高优先级）。"""
    _active_runtime().set_session_rules(rules, source=source)


def set_mode_permission_rules(
    rules: Optional[Dict[str, PermissionAction]],
    source: str = "mode",
    runtime: Optional[PermissionRuntime] = None,
) -> None:
    """设置 mode 规则（整体替换），不影响 session 授权。

    可显式指定目标 runtime；用于「设置模式时上下文运行时与全局运行时不同」的场景，
    避免依赖调用顺序。
    """
    target = runtime if runtime is not None else _active_runtime()
    target.set_mode_rules(rules, source=source)


def clear_mode_permission_rules(runtime: Optional[PermissionRuntime] = None) -> None:
    """清除 mode 规则。"""
    target = runtime if runtime is not None else _active_runtime()
    target.clear_mode_rules()


def reset_session_permission_rules() -> None:
    """清空进程内 session 授权、mode 规则、回退态与危险工具降级记录。

    会话状态不随工作区切换自动清除（configure_workspace 只重载策略文件），
    因此测试、子 Agent 隔离或切换工作区的场景需要显式调用本函数。
    危险工具降级记录（stripped_dangerous）必须一起清掉，否则 /permissions
    会继续打印「已强制降级为 deny」的过期行（F8）。

    **不要与 :meth:`PermissionRuntime.clear_session_rules` 混淆** —— 两者契约不同：

    * 本函数 = 「把进程彻底擦干净」（**连 mode 规则一起清**），供测试与隔离使用；
    * ``clear_session_rules()`` = 「撤销用户授予的会话授权」（**保留 mode 规则**），
      是 ``/permissions reset`` 的实现，也是面向用户的撤销入口。

    对用户使用本函数会顺手清掉 plan/review 模式的只读约束，因此 CLI 不应调用它。
    """
    runtime = _active_runtime()
    runtime.clear_session_rules()
    runtime.clear_mode_rules()
    runtime.is_in_fallback = False


def update_session_permission_rules(
    rules: Optional[Dict[str, PermissionAction]],
    source: str = "",
) -> None:
    """**合并**进程内 session 授权，不丢弃已有规则。"""
    _active_runtime().update_session_rules(rules, source=source)


def enforce_tool_permission(tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """允许时返回 None，否则返回面向用户的拒绝信息。"""
    decision = _active_runtime().check(tool_name, arguments)
    if decision.allowed:
        return None
    return f"⚠️ {decision.reason}"


def get_permission_policy_summary() -> str:
    """渲染生效策略，供 CLI 展示。"""
    runtime = _active_runtime()
    policy = runtime.policy
    lines = [
        tr("permission_policy.title"),
        tr("permission_policy.workspace", workspace=policy.workspace or "none"),
        tr("permission_policy.default", action=policy.default_action),
    ]
    if runtime.session.mode_rules:
        lines.extend([
            "",
            f"模式规则 ({runtime.session.mode_rule_source or 'mode'}):",
        ])
        for tool_name, action in sorted(runtime.session.mode_rules.items()):
            lines.append(f"  {tool_name}: {action}")

    if runtime.session_rules:
        lines.extend([
            "",
            tr("permission_policy.session_overrides", source=runtime.session_rule_source),
        ])
        for tool_name, action in sorted(runtime.session_rules.items()):
            lines.append(f"  {tool_name}: {action}")

    # 把「被强制降级的危险工具 allow」显式展示出来，避免降级本身又是静默的。
    stripped_summary = runtime.session.get_stripped_summary()
    if stripped_summary:
        lines.extend(["", stripped_summary])
    if policy.stripped_dangerous:
        lines.extend([
            "",
            "策略文件中危险工具的 allow 已强制降级为 deny: "
            + ", ".join(sorted(policy.stripped_dangerous)),
        ])

    lines.extend([
        "",
        tr("permission_policy.tools"),
    ])
    for tool_name, action in sorted(policy.tool_rules.items()):
        source = policy.sources.get(tool_name, "built-in")
        lines.append(f"  {tool_name}: {action} ({source})")
    if policy.path_rules:
        lines.extend(["", tr("permission_policy.paths")])
        for pattern, action in sorted(policy.path_rules.items()):
            source = policy.path_sources.get(pattern, "policy")
            lines.append(f"  {pattern}: {action} ({source})")
    if policy.command_rules:
        lines.extend(["", tr("permission_policy.commands")])
        for pattern, action in sorted(policy.command_rules.items()):
            source = policy.command_sources.get(pattern, "policy")
            lines.append(f"  {pattern}: {action} ({source})")
    return "\n".join(lines)


def set_tool_permission(tool_name: str, action: PermissionAction, scope: str = "user") -> Path:
    """把单个工具的权限持久化到 user 或 project 作用域。"""
    runtime = _active_runtime()
    normalized_action = _normalize_action(action, fallback="")
    if not normalized_action:
        raise ValueError("action must be one of: allow, ask, deny")

    if scope not in {"user", "project"}:
        raise ValueError("scope must be user or project")

    # 危险工具永不自动放行：拒绝把 allow 写入策略文件。
    # 补充与 set_session_rules 同策略，手工绕行由构造兜底。
    if normalized_action == "allow" and str(tool_name) in DANGEROUS_TOOLS:
        raise ValueError(
            f"{tool_name} 属于危险工具，不允许设为 allow；"
            "请使用 ask 或 deny。"
        )

    if scope == "project":
        if runtime.workspace is None:
            raise ValueError("project scope requires a workspace")
        path = SayacodePaths.resolve(create=False).project_permissions(runtime.workspace)
    else:
        path = SayacodePaths.resolve(create=True).user_permissions

    data = _read_policy_file(path) or {"default": "ask", "tools": {}}
    tools = data.setdefault("tools", {})
    tools[str(tool_name)] = normalized_action
    write_private_json(path, data)
    if runtime.workspace is not None:
        runtime.configure_workspace(runtime.workspace)
    else:
        runtime.policy = PermissionPolicy.load(None)
    append_audit_event(
        "permission_policy",
        "set_tool_permission",
        workspace=runtime.workspace,
        allowed=True,
        details={"tool": tool_name, "action": normalized_action, "scope": scope, "path": str(path)},
    )
    return path


def get_permission_audit_log() -> list[Dict[str, Any]]:
    """返回进程内最近的权限判定记录。"""
    return list(_active_runtime().audit_log)


__all__ = [
    "DANGEROUS_TOOLS",
    "DEFAULT_COMMAND_RULES",
    "MUTATING_TOOLS",
    "PermissionDecision",
    "PermissionPolicy",
    "PermissionRequest",
    "PermissionRuntime",
    "SessionPermissionState",
    "SOURCE_BUILTIN",
    "SOURCE_PROJECT",
    "SOURCE_SESSION",
    "SOURCE_USER",
    "clear_mode_permission_rules",
    "configure_permission_workspace",
    "create_permission_runtime",
    "RESTRICTED_TOOLS",
    "enforce_tool_permission",
    "get_permission_audit_log",
    "get_permission_policy_summary",
    "get_permission_workspace",
    "is_parallel_batch",
    "parallel_batch_session",
    "permission_runtime_session",
    "permission_workspace_session",
    "reset_session_permission_rules",
    "restore_permission_workspace",
    "set_mode_permission_rules",
    "set_permission_confirm_callback",
    "set_session_permission_rules",
    "set_tool_permission",
    "summarize_arguments",
    "update_session_permission_rules",
]
