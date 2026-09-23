"""跨会话记忆的命令解析与交互菜单。"""

from __future__ import annotations

import inspect
import re
import shlex
import sys
from dataclasses import asdict, is_dataclass
from typing import Any

from .input import _terminal_prompt
from .selection import choose_option


def _plain(value: Any) -> Any:
    """把记录对象转成终端与 JSONL 可序列化的数据。"""
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


async def _call(method: Any, *args: Any, **kwargs: Any) -> Any:
    result = method(*args, **kwargs)
    return _plain(await result if inspect.isawaitable(result) else result)


async def dispatch_memory_command(app: Any, args: Any = "") -> Any:
    """把 /memory 子命令交给应用记忆门面，不直接接触 Store。"""
    service = getattr(app, "memory", None)
    if service is None:
        raise RuntimeError("Memory service is unavailable")
    raw = str(args or "").strip()
    action_raw, _, body_raw = raw.partition(" ")
    action_raw = action_raw.casefold()

    def body_text(value: str) -> str:
        """自由正文保留反斜杠；菜单产生的带引号文本才解引用。"""
        stripped = value.strip()
        return " ".join(shlex.split(stripped)) if stripped.startswith(("'", '"')) else stripped

    if action_raw == "remember":
        scope = "user"
        match = re.match(r"^(user|project)(?:\s+|$)", body_raw)
        if match is not None:
            scope = match.group(1)
            body_raw = body_raw[match.end():]
        content = body_text(body_raw)
        if not content:
            raise ValueError("Usage: /memory remember [user|project] <text>")
        return await _call(service.remember, content, scope=scope)
    if action_raw == "correct":
        parts = body_raw.split(None, 1)
        if len(parts) != 2 or not body_text(parts[1]):
            raise ValueError("Usage: /memory correct <ID> <text>")
        return await _call(service.correct, parts[0], body_text(parts[1]))
    if action_raw == "search":
        content = body_text(body_raw)
        if not content:
            raise ValueError("Usage: /memory search <query>")
        return await _call(service.list, query=content)

    tokens = shlex.split(raw)
    action = tokens.pop(0).casefold() if tokens else "status"
    if action == "status":
        if tokens:
            raise ValueError("Usage: /memory status")
        return await _call(service.status)
    if action == "recent":
        if tokens:
            raise ValueError("Usage: /memory recent")
        return await _call(service.recent, getattr(app, "session_id", None))
    if action in {"enable", "disable"}:
        if tokens:
            raise ValueError(f"Usage: /memory {action}")
        return await _call(service.settings, {"enabled": action == "enable"})
    if action in {"use", "learn"}:
        if len(tokens) != 1:
            raise ValueError(f"Usage: /memory {action} <value>")
        selected = tokens[0].casefold()
        if action == "use" and selected in {"on", "off"}:
            return await _call(service.settings, {"use": selected == "on"})
        if action == "learn" and selected in {"off", "explicit", "auto"}:
            return await _call(service.settings, {"learn": selected})
        raise ValueError(
            "Usage: /memory use <on|off> or /memory learn <off|explicit|auto>"
        )
    if action == "settings":
        if tokens:
            raise ValueError("Usage: /memory settings")
        return await _call(service.settings)
    if action == "model":
        if len(tokens) != 1:
            raise ValueError("Usage: /memory model <profile|default>")
        return await _call(
            service.settings,
            {"model_profile": None if tokens[0].casefold() == "default" else tokens[0]},
        )
    if action == "timeout":
        if len(tokens) != 1:
            raise ValueError("Usage: /memory timeout <seconds>")
        try:
            seconds = float(tokens[0])
        except ValueError as exc:
            raise ValueError("Usage: /memory timeout <seconds>") from exc
        return await _call(service.settings, {"headless_timeout_seconds": seconds})
    if action == "session":
        thread_id = getattr(app, "session_id", None)
        if not isinstance(thread_id, str) or not thread_id:
            raise RuntimeError("当前没有活动会话")
        if not tokens:
            return await _call(service.session_settings, thread_id)
        if len(tokens) != 2:
            raise ValueError("Usage: /memory session [use <on|off|default>|learn <auto|explicit|off|default>]")
        field, selected = tokens[0].casefold(), tokens[1].casefold()
        if field == "use" and selected in {"on", "off", "default"}:
            return await _call(
                service.session_settings,
                thread_id,
                {"use": None if selected == "default" else selected == "on"},
            )
        if field == "learn" and selected in {"auto", "explicit", "off", "default"}:
            return await _call(
                service.session_settings,
                thread_id,
                {"learn": None if selected == "default" else selected},
            )
        raise ValueError("Usage: /memory session [use <on|off|default>|learn <auto|explicit|off|default>]")
    if action == "list":
        if len(tokens) > 1 or (tokens and tokens[0] not in {"all", "user", "project"}):
            raise ValueError("Usage: /memory list [all|user|project]")
        scope = tokens[0] if tokens else "all"
        return await _call(service.list, scope=None if scope == "all" else scope)
    if action in {"show", "get"}:
        if len(tokens) != 1:
            raise ValueError("Usage: /memory show <ID>")
        return await _call(service.get, tokens[0])
    if action == "confirm":
        if len(tokens) != 1:
            raise ValueError("Usage: /memory confirm <ID>")
        return await _call(service.confirm, tokens[0])
    if action in {"pin", "unpin"}:
        if len(tokens) != 1:
            raise ValueError(f"Usage: /memory {action} <ID>")
        return await _call(service.pin, tokens[0], pinned=action == "pin")
    if action == "forget":
        if len(tokens) != 1:
            raise ValueError("Usage: /memory forget <ID>")
        return await _call(service.forget, tokens[0])
    if action == "refresh":
        if len(tokens) > 1:
            raise ValueError("Usage: /memory refresh [ID]")
        return await _call(service.refresh, tokens[0] if tokens else None)
    raise ValueError("Usage: /memory [status|recent|settings|session|model|timeout|enable|disable|list|search|show|remember|correct|confirm|pin|unpin|forget|refresh]")


def _is_interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


async def _memory_input(label: str) -> str:
    """记忆正文和搜索词使用进程内历史，不写入主输入历史文件。"""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory

    session: PromptSession[str] = PromptSession(history=InMemoryHistory())
    return (await _terminal_prompt(session, label)).strip()


async def run_memory_menu(app: Any, prompt_session: Any, presenter: Any, language: str) -> bool:
    """在交互终端打开方向键菜单；管道输入继续使用纯文本命令。"""
    if not _is_interactive_terminal():
        return False
    zh = language == "zh"

    def label(chinese: str, english: str) -> str:
        return chinese if zh else english

    def show(command: str, value: Any) -> None:
        from .commands import format_result

        presenter.command_result(command, format_result(value))

    while True:
        status = await dispatch_memory_command(app, "status")
        show("/memory status", status)
        action = await choose_option(
            prompt_session,
            label=label("管理跨会话记忆", "Manage cross-session memory"),
            fallback_label=label("记忆操作：", "Memory action: "),
            options=(
                ("recent", label("本轮参考", "Provided this turn")),
                ("list", label("浏览记忆", "Browse memories")),
                ("search", label("搜索记忆", "Search memories")),
                ("remember", label("手动记住", "Remember explicitly")),
                ("settings", label("全局记忆默认设置", "Global memory defaults")),
                ("session", label("当前会话记忆设置", "Current session memory settings")),
                ("back", label("返回对话", "Back to chat")),
            ),
            invalid_message=label("请选择菜单中的操作。", "Choose an action from the menu."),
            notice=presenter.notice,
            language=language,
        )
        if action == "back":
            return True
        if action == "remember":
            scope = await choose_option(
                prompt_session,
                label=label("记忆适用范围", "Memory scope"),
                fallback_label=label("范围：", "Scope: "),
                options=(("user", label("我的偏好", "My preferences")), ("project", label("当前项目", "Current project"))),
                invalid_message=label("请选择范围。", "Choose a scope."),
                notice=presenter.notice,
                language=language,
            )
            body = await _memory_input(label("记住什么：", "Remember: "))
            if body:
                show("/memory remember", await dispatch_memory_command(app, f"remember {scope} {shlex.quote(body)}"))
            continue
        if action == "settings":
            setting = await choose_option(
                prompt_session,
                label=label("全局记忆默认设置", "Global memory defaults"),
                fallback_label=label("设置：", "Setting: "),
                options=(
                    ("enable", label("启用记忆", "Enable memory")),
                    ("disable", label("停用记忆", "Disable memory")),
                    ("use on", label("全局主 Agent 使用：开", "Global agent use: on")),
                    ("use off", label("全局主 Agent 使用：关", "Global agent use: off")),
                    ("learn auto", label("全局自动学习", "Global learning: automatic")),
                    ("learn explicit", label("全局仅手动记住", "Global learning: explicit")),
                    ("learn off", label("全局暂停学习", "Global learning: off")),
                    ("model", label("记忆整理模型", "Memory learning model")),
                    ("timeout", label("无交互等待时间", "Headless timeout")),
                    ("back", label("返回", "Back")),
                ),
                invalid_message=label("请选择设置。", "Choose a setting."),
                notice=presenter.notice,
                language=language,
            )
            if setting == "model":
                profiles = getattr(getattr(app, "config", None), "profiles", {})
                names = profiles if isinstance(profiles, dict) else {}
                selected = await choose_option(
                    prompt_session,
                    label=label("选择记忆整理模型", "Select memory learning model"),
                    fallback_label=label("模型画像：", "Model profile: "),
                    options=(
                        ("default", label("跟随主模型", "Use main model")),
                        *((name, name) for name in names),
                    ),
                    invalid_message=label("请选择已配置的模型。", "Choose a configured model."),
                    notice=presenter.notice,
                    language=language,
                )
                show("/memory model", await dispatch_memory_command(app, f"model {shlex.quote(selected)}"))
            elif setting == "timeout":
                seconds = await _memory_input(label("等待秒数：", "Timeout seconds: "))
                if seconds:
                    show("/memory timeout", await dispatch_memory_command(app, f"timeout {seconds}"))
            elif setting != "back":
                show("/memory settings", await dispatch_memory_command(app, setting))
            continue
        if action == "session":
            show("/memory session", await dispatch_memory_command(app, "session"))
            setting = await choose_option(
                prompt_session,
                label=label("当前会话记忆设置", "Current session memory settings"),
                fallback_label=label("会话设置：", "Session setting: "),
                options=(
                    ("session use on", label("此会话主 Agent 使用：开", "This agent use: on")),
                    ("session use off", label("此会话主 Agent 使用：关", "This agent use: off")),
                    ("session use default", label("此会话主 Agent 使用：继承全局", "Inherit global agent use")),
                    ("session learn auto", label("此会话自动学习", "Learn automatically here")),
                    ("session learn explicit", label("此会话仅手动记住", "Explicit learning here")),
                    ("session learn off", label("此会话暂停学习", "Pause learning here")),
                    ("session learn default", label("此会话学习：继承全局", "Use global learning default")),
                    ("back", label("返回", "Back")),
                ),
                invalid_message=label("请选择会话设置。", "Choose a session setting."),
                notice=presenter.notice,
                language=language,
            )
            if setting != "back":
                show("/memory session", await dispatch_memory_command(app, setting))
            continue
        query = None
        if action == "search":
            query = await _memory_input(label("搜索：", "Search: "))
            if not query:
                continue
        request = "recent" if action == "recent" else (
            f"search {shlex.quote(query)}" if query else "list"
        )
        records = await dispatch_memory_command(app, request)
        show(f"/memory {request.split(maxsplit=1)[0]}", records)
        if not isinstance(records, list) or not records:
            continue
        options: list[tuple[str, str]] = []
        for record in records:
            if not isinstance(record, dict) or not record.get("id"):
                continue
            scope = record.get("scope", "")
            scope_name = scope.get("kind", "") if isinstance(scope, dict) else scope
            subject = str(record.get("subject") or record.get("text") or "")[:50]
            options.append((str(record["id"]), f"{subject}  ·  {scope_name}"))
        if not options:
            continue
        selected = await choose_option(
            prompt_session,
            label=label("选择记忆", "Select a memory"),
            fallback_label=label("记忆 ID：", "Memory ID: "),
            options=(*options, ("back", label("返回", "Back"))),
            invalid_message=label("请选择一条记忆。", "Choose a memory."),
            notice=presenter.notice,
            language=language,
        )
        if selected == "back":
            continue
        show("/memory show", await dispatch_memory_command(app, f"show {shlex.quote(selected)}"))
        selected_record = next(
            (item for item in records if isinstance(item, dict) and item.get("id") == selected),
            {},
        )
        pinned = bool(selected_record.get("pinned"))
        operations = [
            (
                "unpin" if pinned else "pin",
                label("取消固定" if pinned else "固定记忆", "Unpin memory" if pinned else "Pin memory"),
            ),
            ("correct", label("纠正内容", "Correct content")),
            ("refresh", label("核验有效性", "Refresh validity")),
            ("forget", label("遗忘这条记忆", "Forget this memory")),
            ("back", label("返回", "Back")),
        ]
        if selected_record.get("state") == "candidate":
            operations.insert(0, ("confirm", label("确认这条候选", "Confirm candidate")))
        operation = await choose_option(
            prompt_session,
            label=label("处理这条记忆", "Manage this memory"),
            fallback_label=label("操作：", "Action: "),
            options=tuple(operations),
            invalid_message=label("请选择操作。", "Choose an action."),
            notice=presenter.notice,
            language=language,
        )
        if operation == "correct":
            body = await _memory_input(label("修正为：", "Correct to: "))
            if body:
                show("/memory correct", await dispatch_memory_command(app, f"correct {shlex.quote(selected)} {shlex.quote(body)}"))
        elif operation == "confirm":
            show("/memory confirm", await dispatch_memory_command(app, f"confirm {shlex.quote(selected)}"))
        elif operation in {"pin", "unpin"}:
            show(
                f"/memory {operation}",
                await dispatch_memory_command(app, f"{operation} {shlex.quote(selected)}"),
            )
        elif operation == "refresh":
            show("/memory refresh", await dispatch_memory_command(app, f"refresh {shlex.quote(selected)}"))
        elif operation == "forget":
            confirmed = await choose_option(
                prompt_session,
                label=label("确认遗忘？", "Forget this memory?"),
                fallback_label=label("确认：", "Confirm: "),
                options=((False, label("返回", "Cancel")), (True, label("确认遗忘", "Forget"))),
                invalid_message=label("请选择确认或返回。", "Choose confirm or cancel."),
                notice=presenter.notice,
                language=language,
            )
            if confirmed:
                show("/memory forget", await dispatch_memory_command(app, f"forget {shlex.quote(selected)}"))
