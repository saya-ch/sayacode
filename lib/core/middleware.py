"""SAIAgent 的 LangChain 中间件：政策进图，执行留给框架。

分层（洋葱模型，外层在前）：

1. ``SayaHookMiddleware`` — PreToolUse / PostToolUse / ToolFailure，与今天
   ``tools/__init__.py::_wrap_tool_with_hooks`` 完全同序（Pre 先于权限、deny 走
   ToolFailure 上报 ``tool_blocked``）。只搬位置，不改语义。
2. ``SayaPermissionMiddleware`` — mode deny / session / 策略 + 危险地板走
   ``PermissionRuntime.peek()``（只判定不弹窗）；ask 走框架 ``interrupt()``，
   恢复后 ``grant_once()`` 让行——否则工具体内联 check 会再弹一次窗。
3. ``SayaSafetyMiddleware`` — ``SafetyChecker`` 的 critical 否决外置。
   只否决、不升级 ask：ask 升级仍由工具体内联检查负责（与今天一致），
   等工具函数体拆内联检查时再搬（Phase B 后续）。
4. ``SayaPromptMiddleware`` — ``wrap_model_call`` + ``override(system_message)``，
   即官方 ``dynamic_prompt`` 的机制。外层每轮 ``refresh()`` 一次（与今天"每轮拼一次"
   同成本），而不是每步模型调用都重扫项目。

``UserPromptSubmit`` / ``SessionStart`` / ``SessionEnd`` 这类非工具事件**不动**，
仍由外层循环在原位置触发——搬进 ``wrap_model_call`` 会变成"每步都触发"，语义变了。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from langchain.agents.middleware.types import AgentMiddleware, ModelRequest, ToolCallRequest
from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.types import interrupt

from .hooks import trigger_hook_event
from .permissions import PermissionRuntime, summarize_arguments


# 中断载荷：目前只有工具询问一种；以后加种类时接收方按 kind 分发。
INTERRUPT_TOOL_ASK = "tool_ask"
APPROVAL_MALFORMED = "malformed"
APPROVAL_REJECTED = "rejected"
CONTEXT_PRUNE_KEEP = 3
CONTEXT_PRUNE_RATIO = 0.70
MAX_MODEL_CALLS_PER_RUN = 20
MAX_TOOL_CALLS_PER_RUN = 50


def build_context_editing_middleware(limit: int):
    """按上下文窗口构建工具结果剪枝中间件；未知窗口时禁用。"""
    try:
        size = int(limit or 0)
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None
    from langchain.agents.middleware import ContextEditingMiddleware
    from langchain.agents.middleware.context_editing import ClearToolUsesEdit

    trigger = max(1000, int(size * CONTEXT_PRUNE_RATIO))
    return ContextEditingMiddleware(edits=[ClearToolUsesEdit(trigger=trigger, keep=CONTEXT_PRUNE_KEEP)])


def build_guardrail_middlewares():
    """构建单轮调用护栏（只按 run 计数，不按 thread 累积）。"""
    from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware

    tool_middleware = ToolCallLimitMiddleware(run_limit=MAX_TOOL_CALLS_PER_RUN)
    model_middleware = ModelCallLimitMiddleware(run_limit=MAX_MODEL_CALLS_PER_RUN, exit_behavior="end")
    return tool_middleware, model_middleware


def _merge_tool_artifact(tool_name: str, tool_artifact: Any, outcome: str,
                          content: str) -> Dict[str, Any]:
    """合并工具自带 artifact 与默认底：工具原样覆盖，缺失字段用默认补齐。"""
    from .tool_result import build_tool_artifact

    merged = build_tool_artifact(tool_name, outcome, chars=len(content))
    if isinstance(tool_artifact, dict):
        merged.update(tool_artifact)
    return merged


def parse_approval(answer: Any) -> tuple[bool, str]:
    """显式布尔才算批准，其余一律拒绝。"""
    if answer is True:
        return True, "approved"
    if isinstance(answer, dict) and answer.get("approved") is True:
        return True, "approved"
    if answer is False:
        return False, APPROVAL_REJECTED
    if isinstance(answer, dict) and answer.get("approved") is False:
        return False, APPROVAL_REJECTED
    return False, APPROVAL_MALFORMED


def _tool_call_parts(request: ToolCallRequest) -> tuple[str, Dict[str, Any], Optional[str]]:
    """从请求里拿出（工具名，参数，调用 ID），形状不对时 fail-closed。"""
    tool_call = request.tool_call or {}
    name = str(tool_call.get("name") or "unknown")
    args = tool_call.get("args") or {}
    if not isinstance(args, dict):
        args = {}
    return name, args, tool_call.get("id")


def _deny_message(reason: str, name: str, call_id: Optional[str]) -> ToolMessage:
    """拒绝短路消息。

    内容必须以 ``⚠️`` 开头：``agent.py::_extract_tool_result`` 靠这个前缀把拒绝
    归类为"工具执行出错"而不是"工具结果"——与今天工具体内联拒绝
    （``enforce_tool_permission`` 返回 ``f"⚠️ {reason}"``）完全一致。
    """
    return ToolMessage(content=f"⚠️ {reason}", name=name, tool_call_id=call_id)


class SayaPromptMiddleware(AgentMiddleware):
    """每轮刷新一次的 system prompt（官方 dynamic_prompt 机制）。

    ``refresh()`` 由外层循环在每轮调用一次——与今天"每轮拼一次 system"同成本。
    不能每步都重拼：拼一次含一次项目扫描，每步都拼会把多步 turn 拖慢数倍。
    """

    def __init__(self, build_system: Optional[Callable[[], str]] = None) -> None:
        super().__init__()
        self._build_system = build_system
        self._current_system = ""

    def refresh(self, system_text: Optional[str] = None) -> None:
        """设置本轮的 system 文本；不传则用构造时的 builder 现拼一次。"""
        if system_text is not None:
            self._current_system = system_text
            return
        self._current_system = self._build_system() if self._build_system else ""

    def wrap_model_call(self, request: ModelRequest, handler: Callable) -> Any:
        """注入本轮 system 文本后调用模型。"""
        text = self._current_system
        if not text.strip() and self._build_system is not None:
            text = self._build_system()
        if text.strip():
            request = request.override(system_message=SystemMessage(content=text))
        return handler(request)


class SayaPermissionMiddleware(AgentMiddleware):
    """权限门：deny 直接短路，ask 走框架 interrupt。

    恢复约定（由 SAIAgent 的 interrupt_handler 履行，交互层用现有确认窗）：
    - ``{"approved": True}`` → ``grant_once()`` 后执行（"一次/会话/永久"三选的
      落规则副作用仍由确认窗内部完成，与今天一致；grant_once 只补"一次"的那份，
      且会话/永久批准下多一次一次性放行是无害的——规则本身也会放行）；
    - 否则 → 记拒绝审计并短路（确认窗内部已记 denial tracker，这里不再碰）。
    """

    def __init__(self, permissions: PermissionRuntime) -> None:
        super().__init__()
        self._permissions = permissions

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        """执行权限判定，deny 短路、ask 走 interrupt。"""
        name, args, call_id = _tool_call_parts(request)
        decision = self._permissions.peek(name, args)

        if decision.action == "deny":
            blocked = self._permissions.record_blocked(name, args, decision.source)
            return _deny_message(blocked.reason, name, call_id)

        if decision.action == "ask":
            answer = interrupt(
                {
                    "kind": INTERRUPT_TOOL_ASK,
                    "tool": name,
                    "args_preview": summarize_arguments(args),
                    "reason": decision.reason,
                    "source": decision.source,
                }
            )
            approved, outcome = parse_approval(answer)
            if approved:
                try:
                    self._permissions.grant_once(name, args)
                except TypeError:
                    self._permissions.grant_once(name)
                return handler(request)
            blocked = self._permissions.record_blocked(name, args, decision.source, extra={"approval": outcome})
            return _deny_message(blocked.reason, name, call_id)

        return handler(request)


def _safety_operation(tool_name: str) -> str:
    """工具名 → SafetyChecker 操作。猜不到就按最严的读处理（只读从不误拦写）。"""
    lowered = str(tool_name).lower()
    if "delete" in lowered or "remove" in lowered:
        return "delete"
    if "write" in lowered or "create" in lowered or "edit" in lowered or "apply" in lowered or "save" in lowered:
        return "write"
    if "execut" in lowered or lowered.startswith("run") or "shell" in lowered or "bash" in lowered:
        return "execute"
    return "read"


def _safety_target(tool_name: str, args: Dict[str, Any]) -> Optional[tuple[str, str]]:
    """参数 → ("command"|"file", 值)。拿不到就返回 None（交给工具体内联检查）。"""
    command = args.get("command")
    if isinstance(command, str) and command.strip():
        return ("command", command)
    for key in ("path", "file_path", "file", "directory", "dir", "target"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return ("file", value)
    return None


class SayaSafetyMiddleware(AgentMiddleware):
    """安全否决外置：只否决、不升级 ask（与今天一致）。

    判据必须用 ``lib/tools/safety.py`` 的原语（``check_file_danger`` /
    ``check_delete_danger`` / ``check_command_danger``），**不能**用
    ``SafetyChecker.check_file_operation``——实测后者在 Windows 下漏拦：
    ``check_file_operation("write", "C:\\Windows\\System32\\evil.txt")`` 返回安全，
    而工具体实际走的 ``check_file_danger`` 正确拒绝。以外层为准就必须用严的那套，
    否则中间件成了摆设，MCP/外部工具还没人替它们检查。
    """

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        """执行安全否决检查，不通过则短路。"""
        from ..tools.safety import (
            check_command_danger,
            check_delete_danger,
            check_file_danger,
        )

        name, args, call_id = _tool_call_parts(request)
        target = _safety_target(name, args)
        if target is None:
            return handler(request)
        kind, value = target
        if kind == "command":
            safe, reason = check_command_danger(value)
        else:
            safe, reason = check_file_danger(value)
            if safe and _safety_operation(name) == "delete":
                safe, reason = check_delete_danger(value)
        if not safe:
            # 前缀 "安全检查失败" 命中 _tool_result_was_blocked，上报走 ToolFailure。
            return ToolMessage(
                content=f"⚠️ 安全检查失败：{reason}", name=name, tool_call_id=call_id
            )
        return handler(request)


class SayaHookMiddleware(AgentMiddleware):
    """Hook 事件搬进图：与 ``_wrap_tool_with_hooks`` 同序同语义。

    顺序必须在最外层：今天 PreToolUse 先于权限触发，deny（无论哪一层否的）
    都走 ToolFailure 上报 ``tool_blocked``。判定"被拦"沿用
    ``_tool_result_was_blocked`` 的标记表，一个字都不改。

    ``trigger`` 可注入：生产用 ``trigger_hook_event``，单测传 recorder。
    """

    def __init__(self, trigger: Optional[Callable] = None) -> None:
        super().__init__()
        self._trigger = trigger or trigger_hook_event

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        """触发前后 hook 并透传中止状态。"""
        from ..tools.context import get_abort_controller

        name, args, call_id = _tool_call_parts(request)

        abort_ctrl = get_abort_controller()
        if abort_ctrl.is_aborted:
            return ToolMessage(
                content=f"⚠️ 操作已中止（同级工具失败: {abort_ctrl.reason}）",
                name=name,
                tool_call_id=call_id,
            )

        block_reason = self._trigger(
            "PreToolUse", {"tool_name": name, "arguments": args}
        )
        if block_reason:
            return ToolMessage(
                content=f"⚠️ {block_reason}", name=name, tool_call_id=call_id
            )

        try:
            result = handler(request)
        except Exception as exc:
            self._trigger(
                "ToolFailure",
                {
                    "tool_name": name,
                    "arguments": args,
                    "error": str(exc),
                    "exception_type": exc.__class__.__name__,
                },
            )
            self._audit(name, args, allowed=False, error=str(exc),
                        artifact=_merge_tool_artifact(name, None, "denied", str(exc)))
            self._maybe_sibling_abort(name)
            raise

        content = result.content if isinstance(result, ToolMessage) else str(result)
        merged_artifact = _merge_tool_artifact(name, getattr(result, "artifact", None),
                                               "ok", str(content))
        if self._was_blocked(content):
            self._trigger(
                "ToolFailure",
                {
                    "tool_name": name,
                    "arguments": args,
                    "error": "tool_blocked",
                    "result_preview": str(content)[:1000],
                },
            )
            self._audit(name, args, allowed=False, result_preview=str(content)[:1000],
                        artifact=_merge_tool_artifact(name, getattr(result, "artifact", None),
                                                      "denied", str(content)))
            return result

        self._trigger(
            "PostToolUse",
            {"tool_name": name, "arguments": args, "result_preview": str(content)[:1000]},
        )
        self._audit(name, args, allowed=True, result_preview=str(content)[:1000],
                    artifact=merged_artifact)
        return result

    @staticmethod
    def _was_blocked(content: Any) -> bool:
        from ..tools import _tool_result_was_blocked

        return bool(_tool_result_was_blocked(content))

    @staticmethod
    def _maybe_sibling_abort(tool_name: str) -> None:
        from ..tools.context import get_abort_controller

        if tool_name in {
            "execute_command_tool",
            "git_add",
            "git_commit",
            "git_push",
            "git_pull",
            "git_checkout",
            "git_stash",
        }:
            get_abort_controller().abort("sibling_error")

    @staticmethod
    def _audit(
        tool_name: str,
        args: Dict[str, Any],
        *,
        allowed: bool,
        error: str = "",
        result_preview: str = "",
        artifact: Optional[Dict[str, Any]] = None,
    ) -> None:
        from ..tools import (
            get_file_tools_workspace,
            get_git_tools_workspace,
            get_project_tools_workspace,
            get_shell_tools_workspace,
        )
        from .audit import append_audit_event

        workspace = (
            get_file_tools_workspace()
            or get_shell_tools_workspace()
            or get_git_tools_workspace()
            or get_project_tools_workspace()
        )
        details: Dict[str, Any] = {"arguments": args}
        if error:
            details["error"] = error
        if result_preview:
            details["result_preview"] = result_preview
        if artifact is not None:
            from .tool_result import validate_tool_artifact
            import logging

            problems = validate_tool_artifact(artifact)
            if problems:
                logging.getLogger(__name__).warning("artifact 不合契约: %s", problems)
            details["artifact"] = artifact
        append_audit_event("tool", tool_name, workspace=workspace, allowed=allowed, details=details)


__all__ = [
    "APPROVAL_MALFORMED",
    "APPROVAL_REJECTED",
    "CONTEXT_PRUNE_KEEP",
    "CONTEXT_PRUNE_RATIO",
    "INTERRUPT_TOOL_ASK",
    "MAX_MODEL_CALLS_PER_RUN",
    "MAX_TOOL_CALLS_PER_RUN",
    "SayaHookMiddleware",
    "SayaPermissionMiddleware",
    "SayaPromptMiddleware",
    "SayaSafetyMiddleware",
    "build_context_editing_middleware",
    "build_guardrail_middlewares",
    "parse_approval",
]
