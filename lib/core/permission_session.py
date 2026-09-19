"""会话状态模块：共享会话态、运行时主体与上下文绑定。

会话授权与模式规则属于会话不属于工作区。
同一进程所有运行时共享同一份状态，不依赖调用顺序。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional

from .audit import append_audit_event
from .permission_interrupt import (
    apply_dangerous_floor,
    apply_fallback_decision,
    build_one_shot_grant,
    build_parallel_batch_deny,
    consume_one_shot_grant,
    grant_fingerprint,
    is_parallel_batch,
)
from .permission_policy import (
    DANGEROUS_TOOLS,
    PermissionAction,
    PermissionDecision,
    PermissionPolicy,
    PermissionRequest,
    PermissionSource,
    _match_rule,
    _normalize_action,
    summarize_arguments,
)


@dataclass
class SessionPermissionState:
    """进程级共享的会话权限状态，会话授权加模式规则加回退态。"""

    session_rules: Dict[str, PermissionAction] = field(default_factory=dict)
    session_rule_source: str = "session"
    mode_rules: Dict[str, PermissionAction] = field(default_factory=dict)
    mode_rule_source: str = ""
    is_in_fallback: bool = False
    stripped_dangerous: Dict[PermissionSource, List[str]] = field(default_factory=dict)
    # 图中断批准的一次性放行，放共享状态里跨实例可见，避免双弹窗
    one_shot_grants: set = field(default_factory=set)

    def record_stripped(self, source: PermissionSource, tool_name: str) -> None:
        """记录一次危险工具放行转拒绝的强制降级。"""
        bucket = self.stripped_dangerous.setdefault(str(source or "session"), [])
        if tool_name not in bucket:
            bucket.append(tool_name)

    def clear_stripped(self) -> None:
        """清除降级记录，授权清空后旧记录不再对应生效规则。"""
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


# 进程级共享态，默认复用它，不建私有快照，避免丢模式拒绝导致放行
_SHARED_SESSION_STATE = SessionPermissionState()


class PermissionRuntime:
    """进程级运行时策略，以及可选的交互式确认回调。

    会话级状态由共享态承载，本类只额外持有工作区策略与审计日志。
    """

    def __init__(self, session: Optional[SessionPermissionState] = None) -> None:
        self.workspace: Optional[Path] = None
        self.policy = PermissionPolicy.load(None)
        self.confirm_callback: Optional[Callable[[PermissionRequest], bool]] = None
        self.audit_log: list[Dict[str, Any]] = []
        # 会话状态跨运行时共享，工作区切换不重置用户授权
        self.session = session if session is not None else _SHARED_SESSION_STATE

    @property
    def session_rules(self) -> Dict[str, PermissionAction]:
        """返回会话授权规则。"""
        return self.session.session_rules

    @session_rules.setter
    def session_rules(self, value: Optional[Dict[str, PermissionAction]]) -> None:
        """整体替换会话授权并归一化，直接赋值也要过降级。"""
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
        """整体替换会话授权，模式规则请用对应方法，避免清空用户授权。"""
        self.session.session_rule_source = str(source or "session")
        self.session_rules = rules

    def clear_session_rules(self) -> None:
        """清除会话授权，保留模式规则，用于用户撤销入口。"""
        self.session.session_rules = {}
        self.session.session_rule_source = "session"
        # 降级记录与一次性批准同属会话授权，一并清除，不留后门
        self.session.clear_stripped()
        self.session.one_shot_grants.clear()

    def update_session_rules(
        self,
        rules: Optional[Dict[str, PermissionAction]],
        source: str = "",
    ) -> None:
        """合并会话授权，保留已有规则。"""
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
        """整体替换模式规则，不影响会话授权，但清掉未消耗的一次性批准。

        一次性批准绑定批准时的上下文，切模式后旧批准一律作废。
        """
        self.session.mode_rules = self._normalize_rules(rules, source)
        self.session.mode_rule_source = str(source or "mode")
        self.session.one_shot_grants.clear()

    def clear_mode_rules(self) -> None:
        """清除模式规则，回到无模式约束。"""
        self.session.mode_rules = {}
        self.session.mode_rule_source = ""

    def _normalize_rules(
        self,
        rules: Optional[Dict[str, PermissionAction]],
        source: str,
    ) -> Dict[str, PermissionAction]:
        """归一化规则，危险工具放行强制降级为拒绝。"""
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
        """判定工具调用并处理确认与回退，无回调的询问一律拒绝。"""
        decision = self._decide(tool_name, arguments or {})

        # 回退模式把放行升级为询问，无回调时后续逻辑会拒绝
        decision = self._apply_fallback(decision, tool_name)

        if decision.action == "allow":
            self._record(tool_name, decision, arguments, allowed=True)
            return decision

        if decision.action == "deny":
            blocked = self.record_blocked(tool_name, arguments, decision.source)
            return blocked

        if is_parallel_batch():
            denied = build_parallel_batch_deny(tool_name, decision.source)
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
        """回退模式把放行升级为询问，拒绝保持不变。"""
        return apply_fallback_decision(decision, tool_name, self.is_in_fallback)

    def _decide(self, tool_name: str, arguments: Dict[str, Any]) -> PermissionDecision:
        """按优先级判定，唯一出口施加危险工具地板。"""
        decision = self._decide_by_priority(tool_name, arguments)
        if decision.action != "deny" and consume_one_shot_grant(
            self.session.one_shot_grants, tool_name, arguments
        ):
            # 一次性批准用后即焚：只认同名同参的那次调用，模式拒绝照样优先
            return build_one_shot_grant(tool_name)
        return self._apply_dangerous_floor(tool_name, decision)

    def peek(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> PermissionDecision:
        """只判定不弹窗，给图中间件用的只读决策。"""
        decision = self._decide(tool_name, arguments or {})
        return self._apply_fallback(decision, tool_name)

    def grant_once(self, tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> None:
        """记录一次图中断批准，同名同参的下次判定放行并消耗，跨实例可见。"""
        self.session.one_shot_grants.add(grant_fingerprint(tool_name, arguments))
        try:
            self._record(
                tool_name,
                build_one_shot_grant(tool_name),
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
        """构造与拒绝分支一致的决策并记审计，给中间件短路用。"""
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
        """危险工具地板，通配改写一视同仁，只在出口兜一次。"""
        return apply_dangerous_floor(
            tool_name, decision, self.session.record_stripped
        )

    def _decide_by_priority(self, tool_name: str, arguments: Dict[str, Any]) -> PermissionDecision:
        """按优先级判定，模式拒绝优先，其次会话授权模式放行与策略文件。"""
        mode_action = _match_rule(self.session.mode_rules, tool_name)
        mode_source = self.session.mode_rule_source or "mode"

        # 模式拒绝是硬约束，不可被会话绕开
        if mode_action == "deny":
            return PermissionDecision(
                allowed=False,
                action="deny",
                reason=f"{tool_name}: deny",
                source=mode_source,
            )

        # 会话授权来自用户确认选择
        session_action = _match_rule(self.session.session_rules, tool_name)
        if session_action:
            return PermissionDecision(
                allowed=session_action == "allow",
                action=session_action,
                reason=f"{tool_name}: {session_action}",
                source=self.session.session_rule_source,
            )

        # 模式放行与询问次之
        if mode_action:
            return PermissionDecision(
                allowed=mode_action == "allow",
                action=mode_action,
                reason=f"{tool_name}: {mode_action}",
                source=mode_source,
            )

        # 用户与项目策略及默认兜底
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


_RUNTIME = PermissionRuntime()
_RUNTIME_CONTEXT: ContextVar[PermissionRuntime | None] = ContextVar(
    "sayacode_permission_runtime",
    default=None,
)


def _active_runtime() -> PermissionRuntime:
    return _RUNTIME_CONTEXT.get() or _RUNTIME


@contextmanager
def permission_runtime_session(runtime: PermissionRuntime) -> Iterator[PermissionRuntime]:
    """在当前执行上下文中使用指定的权限运行时。"""
    token = _RUNTIME_CONTEXT.set(runtime)
    try:
        yield runtime
    finally:
        _RUNTIME_CONTEXT.reset(token)
