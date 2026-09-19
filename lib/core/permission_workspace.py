"""工作区配置模块：运行时装配、策略落盘与摘要展示。

会话状态跨工作区共享，工作区切换只重载策略文件。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional

from .audit import append_audit_event
from ..i18n import tr
from .paths import SayacodePaths
from .private_io import write_private_json
from .permission_policy import (
    DANGEROUS_TOOLS,
    PermissionAction,
    PermissionPolicy,
    PermissionRequest,
    _normalize_action,
    _read_policy_file,
)
from .permission_session import (
    PermissionRuntime,
    _active_runtime,
    permission_runtime_session,
)


def create_permission_runtime(workspace: str | Path) -> PermissionRuntime:
    """为单个工作区创建运行时，会话状态为共享引用而非快照。"""
    base_runtime = _active_runtime()
    runtime = PermissionRuntime(session=base_runtime.session)
    runtime.configure_workspace(workspace)
    runtime.confirm_callback = base_runtime.confirm_callback
    return runtime


@contextmanager
def permission_workspace_session(workspace: str | Path) -> Iterator[PermissionRuntime]:
    """把权限检查绑定到某个工作区，作用于当前执行上下文。"""
    base_runtime = _active_runtime()
    runtime = create_permission_runtime(workspace)
    runtime.audit_log = base_runtime.audit_log
    token_handle = permission_runtime_session(runtime)
    with token_handle:
        yield runtime


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
    """整体替换进程内会话授权，最高优先级。"""
    _active_runtime().set_session_rules(rules, source=source)


def set_mode_permission_rules(
    rules: Optional[Dict[str, PermissionAction]],
    source: str = "mode",
    runtime: Optional[PermissionRuntime] = None,
) -> None:
    """设置模式规则整体替换，不影响会话授权，可指定目标运行时。"""
    target = runtime if runtime is not None else _active_runtime()
    target.set_mode_rules(rules, source=source)


def clear_mode_permission_rules(runtime: Optional[PermissionRuntime] = None) -> None:
    """清除模式规则。"""
    target = runtime if runtime is not None else _active_runtime()
    target.clear_mode_rules()


def reset_session_permission_rules() -> None:
    """彻底擦干净进程会话态，供测试与隔离用，用户入口勿调。"""
    runtime = _active_runtime()
    runtime.clear_session_rules()
    runtime.clear_mode_rules()
    runtime.is_in_fallback = False


def update_session_permission_rules(
    rules: Optional[Dict[str, PermissionAction]],
    source: str = "",
) -> None:
    """合并进程内会话授权，不丢弃已有规则。"""
    _active_runtime().update_session_rules(rules, source=source)


def enforce_tool_permission(tool_name: str, arguments: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """允许时返回空，否则返回面向用户的拒绝信息。"""
    decision = _active_runtime().check(tool_name, arguments)
    if decision.allowed:
        return None
    return f"⚠️ {decision.reason}"


def get_permission_policy_summary() -> str:
    """渲染生效策略，供命令行展示。"""
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

    # 被强制降级的危险放行要显式展示，避免降级本身静默
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
    """把单个工具权限持久化到用户或项目作用域。"""
    runtime = _active_runtime()
    normalized_action = _normalize_action(action, fallback="")
    if not normalized_action:
        raise ValueError("action must be one of: allow, ask, deny")

    if scope not in {"user", "project"}:
        raise ValueError("scope must be user or project")

    # 危险工具永不自动放行，拒绝把放行写入策略文件
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
