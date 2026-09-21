"""汇总运行状态和环境自检结果。

供斜杠命令查询会话线程任务用量和只读工具调用。
只做只读汇总不改任何运行状态。"""

from __future__ import annotations

import inspect
import json
import shlex
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain.tools import ToolRuntime

from .tools import build_tools

if TYPE_CHECKING:
    from .application import SayacodeApp


async def _status(app: SayacodeApp) -> dict[str, Any]:
    """汇总当前会话和工作区的运行状态。
    参数是已初始化的应用实例，返回可直接展示的状态字典。
    调用前应用运行时和任务管理器须可用，消息读取失败时会静默降级为零。
    """
    # 先拉全量线程和当前线程，再筛出未完结的后台任务
    threads = await app.runtime.list_threads(workspace=app.workspace)
    current = await app.runtime.get_thread(app.session_id)
    active_tasks = [
        record.to_dict()
        for record in await app.tasks.list(workspace=app.workspace)
        if record.status in {"pending", "running", "stopping", "paused"}
    ]
    # 再取当前会话消息，读不到就保持空列表不报错
    messages: list[Any] = []
    try:
        handle, _ = await app._context_for_thread(app.session_id)
        state = await app.runtime.get_state(handle, app.session_id)
        messages = list(state.values.get("messages", [])) if state.values else []
    except (KeyError, RuntimeError):
        pass
    # 最后累加各条消息的用量，没有用量信息就返回空
    usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    has_usage = False
    for message in messages:
        raw = getattr(message, "usage_metadata", None)
        if not isinstance(raw, dict):
            continue
        has_usage = True
        for key in usage:
            usage[key] += int(raw.get(key, 0) or 0)
    return {
        "ok": True,
        "workspace": str(app.workspace),
        "session_id": app.session_id,
        "trust_level": app.trust_level,
        "profile": app.profile_name,
        "model": app.model,
        "protocol": app.protocol,
        "reviewer": {
            "configured": app.config.jev is not None,
            "model": app.config.jev.model_id if app.config.jev is not None else None,
        },
        "base_url": app._profile().base_url if app.model is not None else None,
        "context_length": app._profile().context_length if app.model is not None else None,
        "max_output_tokens": app._profile().max_output_tokens if app.model is not None else None,
        "thread": current,
        "sessions": len([item for item in threads if not item.get("is_background")]),
        "active_tasks": active_tasks,
        "mcp_tools": [item.name for item in app.mcp.tools],
        "mcp_error": app.mcp.error,
        "message_count": len(messages),
        "usage": usage if has_usage else None,
    }


async def _doctor(app: SayacodeApp, bundle: Any = "") -> dict[str, Any]:
    """检查运行环境是否齐备，可选落盘一份诊断包。
    参数是应用实例和可选的落盘路径，返回检查项和总体是否通过。
    路径为空就不落盘，给了路径会自动建父目录并追加审计记录。
    """
    import shutil
    import sys

    # 一次查清解释器工作区工具链配置和存储目录
    checks = {
        "python": sys.version.split()[0],
        "workspace_exists": app.workspace.is_dir(),
        "git": shutil.which("git") is not None,
        "powershell": shutil.which("pwsh") is not None or shutil.which("powershell") is not None,
        "profile_configured": app.profile_name is not None
        or app.config.default_profile is not None,
        "mcp": app.mcp.error is None,
        "checkpoints": app.paths.checkpoints.exists(),
        "store": app.paths.store.exists(),
        "reviewer": app.trust_level != "jev" or app.config.jev is not None,
    }
    result = {"ok": all(checks.values()), "checks": checks, "mcp_error": app.mcp.error}
    # 有落盘路径才写文件，无路径只返回检查结果
    target = Path(str(bundle)).expanduser() if str(bundle).strip() else None
    if target is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    **result,
                    "workspace": str(app.workspace),
                    "profile": app.profile_name,
                    "recent_audit": await app.audit.list(thread_id=app.session_id, limit=20),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        result["bundle"] = str(target.resolve())
    return result


async def _git_command(app: SayacodeApp, args: Any) -> Any:
    """解析斜杠命令后的参数并转调只读查询。
    参数是应用实例和原始参数串，返回底层工具的结果字典。
    只放行六种只读动作，其余一律要求走审批，非数字步数会直接报错。
    """
    # 先切分参数并定动作，空参数默认看状态
    tokens = shlex.split(str(args or ""))
    action = tokens[0] if tokens else "status"
    # 名单外的动作不执行，转成待审批结果交上层处理
    if action not in {"status", "diff", "log", "branch", "remote", "show"}:
        return {
            "ok": False,
            "action": "ask",
            "reason": "Git changes require approval through the agent tool call",
        }
    if action in {"status", "branch", "remote"} and len(tokens) != 1:
        raise ValueError(f"/git {action} accepts no arguments")
    # 按动作组装查询条件，差异和详情可带引用和路径，日志可带条数
    options: dict[str, Any] = {"action": action}
    if action in {"diff", "show"} and len(tokens) > 1:
        options["ref"] = tokens[1]
        if len(tokens) > 2:
            options["paths"] = tokens[2:]
    if action == "log" and len(tokens) > 1:
        options["limit"] = int(tokens[1])
    return await app._invoke_native_tool("git", **options)


async def _invoke_native_tool(app: SayacodeApp, tool_name: str, **arguments: Any) -> Any:
    """运行只读斜杠命令助手。走同样策略和上下文。
    参数是应用实例加工具名和透传参数，返回工具的原始结果。
    策略不放行就直接返回未通过结果，工具名不存在会按键缺失报错。
    """
    # 先过策略关，不放行就不构造工具
    context = app._context(app.session_id, app.trust_level)
    decision = context.policy.decide(tool_name, arguments, context)
    if decision.action != "allow":
        return {"ok": False, "action": decision.action, "reason": decision.reason}
    # 再按名挑工具，配一个无流式的最小运行环境
    tools = {item.name: item for item in build_tools(context)}
    selected = tools[tool_name]
    runtime: ToolRuntime[Any] = ToolRuntime(
        state={},
        context=context,
        config={},
        stream_writer=lambda _data: None,
        tool_call_id=None,
        store=app.runtime.store,
        tools=list(tools.values()),
    )
    fn = getattr(selected, "coroutine", None) or getattr(selected, "func", None)
    if not callable(fn):
        raise RuntimeError(f"Tool has no callable implementation: {tool_name}")
    # 兼容同步和异步两种实现，异步就等结果再返回
    result = fn(runtime=runtime, **arguments)
    return await result if inspect.isawaitable(result) else result
