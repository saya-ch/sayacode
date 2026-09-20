"""Async terminal and one-shot entry points for SAYACODE 2.0."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import locale
import os
import re
import shutil
import sys
import warnings
from html import escape
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, TextIO
from urllib.parse import urlsplit
from uuid import uuid4

from .commands import BUILTIN_COMMANDS, CommandRouter, format_result
from .config import SUPPORTED_MODEL_PROTOCOLS
from .custom_commands import discover_custom_commands
from .prompts import PromptPreferences, normalize_language, normalize_mode, normalize_style
from .terminal_ui import MODEL_PROTOCOL_LABELS, TerminalPresenter

EVENT_SCHEMA_VERSION = 1
_PRIVATE_KEYS = {
    "reasoning",
    "reasoning_content",
    "thinking",
    "chain_of_thought",
    "hidden",
    "private",
    "raw_response",
}
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|secret|password|credential|authorization|^token$|access[_-]?token|refresh[_-]?token|id[_-]?token)$",
    re.I,
)
_BEARER = re.compile(r"\bBearer\s+[^\s,;]+", re.I)
_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
_TOOL_EVENT_FIELDS = {
    "tool_name",
    "tool_call_id",
    "is_error",
    "duration_ms",
    "outcome",
    "error_type",
}


def _package_version() -> str:
    try:
        return version("sayacode")
    except PackageNotFoundError:
        return "2.0.0"


def _token_count(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([kKmM]?)", value.strip())
    if match is None:
        raise argparse.ArgumentTypeError(
            "token count must be a positive number, e.g. 128000 or 256k"
        )
    amount = int(match.group(1)) * {"": 1, "k": 1000, "m": 1000000}[match.group(2).lower()]
    return amount


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sayacode", description="Async terminal coding agent", allow_abbrev=False
    )
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--profile", help="Use a named saved model profile")
    parser.add_argument("--protocol", help="Model API protocol, such as openai_chat_completions")
    parser.add_argument("--model-id", help="Model identifier accepted by the endpoint")
    parser.add_argument("--base-url")
    authentication = parser.add_mutually_exclusive_group()
    authentication.add_argument("--api-key", help="API key for the selected endpoint")
    authentication.add_argument(
        "--no-api-key", action="store_true",
        help="Explicitly use an unauthenticated endpoint",
    )
    parser.add_argument("--context-length", type=_token_count)
    parser.add_argument("--max-output-tokens", type=_token_count)
    parser.add_argument("--session")
    parser.add_argument("--new-session", action="store_true")
    parser.add_argument("--mode", choices=("build", "plan", "review"))
    parser.add_argument("--lang", choices=("auto", "zh", "en"))
    parser.add_argument("--style")
    parser.add_argument("-p", "--prompt", help="Run one prompt and exit; '-' reads stdin")
    parser.add_argument("--output-format", choices=("text", "json", "jsonl"), default="text")
    parser.add_argument("--no-stream", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--json", action="store_true", help="JSON output for --doctor")
    parser.add_argument("--bundle", type=Path, help="Write a support bundle with --doctor")
    parser.add_argument("--no-clear", action="store_true")
    parser.add_argument("--version", action="version", version=f"SAYACODE {_package_version()}")
    return parser


def _state_home() -> Path:
    return Path(os.environ.get("SAYACODE_HOME") or Path.home() / ".sayacode").expanduser()


def load_preferences() -> PromptPreferences:
    try:
        document = json.loads((_state_home() / "config.json").read_text(encoding="utf-8"))
        data = document.get("preferences", {}) if isinstance(document, dict) else {}
        if not isinstance(data, dict):
            return PromptPreferences()
        return PromptPreferences(
            style=normalize_style(data.get("style")),
            language=normalize_language(data.get("language")),
            mode=normalize_mode(data.get("mode")),
        )
    except (OSError, ValueError):
        return PromptPreferences()


def save_preferences(preferences: PromptPreferences) -> None:
    home = _state_home()
    home.mkdir(parents=True, exist_ok=True)
    target = home / "config.json"
    try:
        document = json.loads(target.read_text(encoding="utf-8")) if target.is_file() else {}
    except (OSError, ValueError):
        document = {}
    if not isinstance(document, dict):
        document = {}
    document["preferences"] = {
        "style": preferences.style,
        "language": preferences.language,
        "mode": preferences.mode,
    }
    temporary = home / "config.json.tmp"
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)


def _redact(value: Any, *, key: str = "") -> Any:
    if key.lower() in _PRIVATE_KEYS:
        return None
    if _SECRET_KEY.search(key):
        return "***"
    if isinstance(value, dict):
        return {
            str(k): _redact(v, key=str(k))
            for k, v in value.items()
            if str(k).lower() not in _PRIVATE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _OPENAI_KEY.sub("***", _BEARER.sub("Bearer ***", value))
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    kind = str(event.get("type") or "graph.event")
    if kind == "event":
        return {"type": "graph.event", "method": str(event.get("method") or "")}
    if kind.startswith("tool."):
        return {
            "type": kind,
            **{key: _redact(event[key], key=key) for key in _TOOL_EVENT_FIELDS if key in event},
        }
    return {
        "type": kind,
        **{
            str(k): _redact(v, key=str(k))
            for k, v in event.items()
            if k not in {"type", "schema_version", "sequence", "run_id"}
            and str(k).lower() not in _PRIVATE_KEYS
        },
    }


class JsonlWriter:
    def __init__(self, stream: TextIO, *, run_id: str | None = None) -> None:
        self.stream = stream
        self.run_id = run_id or uuid4().hex
        self.sequence = 0

    def emit(self, event: dict[str, Any]) -> None:
        self.sequence += 1
        public = _public_event(event)
        envelope = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "sequence": self.sequence,
            "run_id": self.run_id,
            **public,
        }
        self.stream.write(json.dumps(envelope, ensure_ascii=False, default=str) + "\n")
        self.stream.flush()


def _run_ok(result: Any) -> bool:
    return not isinstance(result, dict) or (
        result.get("ok", True) is not False
        and result.get("status") not in {"failed", "error", "paused", "interrupted", "stopped"}
    )


def _exit_code(result: Any) -> int:
    if isinstance(result, dict) and result.get("status") in {"paused", "interrupted"}:
        return 3
    return 0 if _run_ok(result) else 1


def _terminal_type(result: Any) -> str:
    code = _exit_code(result)
    return "run.paused" if code == 3 else "run.completed" if code == 0 else "run.failed"


async def _wait_for_tasks(app: Any) -> list[dict[str, Any]]:
    waiter = getattr(app, "wait_for_tasks", None)
    if not callable(waiter):
        return []
    value = waiter()
    if inspect.isawaitable(value):
        value = await value
    if not isinstance(value, list):
        raise TypeError("wait_for_tasks must return a list of task records")
    return [dict(item) for item in value if isinstance(item, dict)]


def _with_task_outcome(result: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    if not tasks:
        return result
    merged = {**result, "tasks": tasks}
    wakes = [
        task["parent_wake"] for task in tasks
        if isinstance(task.get("parent_wake"), dict)
    ]
    if wakes:
        merged["parent_wakes"] = wakes
        responses = [str(wake.get("response") or "") for wake in wakes]
        parts = [str(result.get("response") or ""), *responses]
        merged["response"] = "\n\n".join(part for part in parts if part)
    if not _run_ok(merged):
        return merged
    paused = [task for task in tasks if task.get("status") in {"paused", "interrupted"}]
    paused.extend(wake for wake in wakes if wake.get("type") == "agent.wake.paused")
    failed = [task for task in tasks if task.get("status") in {"failed", "error", "stopped"}]
    failed.extend(
        wake for wake in wakes
        if wake.get("type") in {"agent.wake.failed", "agent.wake.stopped"}
    )
    if paused:
        merged.update(ok=False, status="paused", error="Background task requires attention")
    elif failed:
        merged.update(ok=False, status="failed", error="Background task failed or stopped")
    elif any(task.get("status") not in {"completed"} for task in tasks):
        merged.update(ok=False, status="failed", error="Background task did not finish")
    return merged


def _response_text(result: Any) -> str:
    if isinstance(result, dict):
        return str(result.get("response") or result.get("text") or "")
    return str(result or "")


def _needs_profile_setup(app: Any) -> bool:
    if not hasattr(app, "config"):
        return False
    try:
        return getattr(app, "model", None) is None
    except (KeyError, RuntimeError):
        return True


_TEAM_APPROVAL = re.compile(r"^/team\s+(approve|reject)\s+(\S+)\s*$", re.I)


def _terminal_language(preference: str) -> str:
    if preference in {"zh", "en"}:
        return preference
    current = (locale.getlocale()[0] or "").lower()
    return "zh" if current.startswith(("zh", "chinese")) else "en"


async def _terminal_prompt(session: Any, label: str, **kwargs: Any) -> str:
    """Keep prompt-toolkit's edited line intact when a task prints a notification."""
    if sys.stdout.isatty():
        from prompt_toolkit.patch_stdout import patch_stdout

        with patch_stdout():
            return str(await session.prompt_async(label, **kwargs))
    return str(await session.prompt_async(label, **kwargs))


async def _pending_team_approval(app: Any, task_id: str) -> dict[str, Any]:
    pending = app.command("team", f"pending {task_id}")
    if inspect.isawaitable(pending):
        pending = await pending
    if not isinstance(pending, dict) or pending.get("status") != "paused":
        raise ValueError(f"Task {task_id} has no pending approval")
    if not isinstance(pending.get("action_requests"), list) or not pending["action_requests"]:
        raise ValueError(f"Task {task_id} has no pending actions")
    return pending


async def _resume_approval_from_terminal(
    app: Any,
    pending: dict[str, Any],
    prompt_session: Any,
    *,
    language: str = "auto",
    reject_all: bool = False,
    presenter: TerminalPresenter | None = None,
) -> Any:
    actions = pending.get("action_requests")
    action_requests = actions if isinstance(actions, list) else []
    count = len(action_requests) or 1
    decisions: list[dict[str, str]] = []
    grants: list[dict[str, Any]] = []
    for index in range(count):
        action_request = (
            action_requests[index]
            if index < len(action_requests) and isinstance(action_requests[index], dict)
            else {}
        )
        name = str(action_request.get("name") or "tool")
        if presenter is not None:
            presenter.approval_action(index, count, action_request)
        if reject_all:
            answer = "n"
        else:
            label = (
                f"批准 {name} ({index + 1}/{count})？[y 仅本次 / s 本会话 / p 长期 / N 拒绝] "
                if language == "zh"
                else f"Approve {name} ({index + 1}/{count})? "
                "[y once / s session / p permanent / N] "
            )
            answer = (await _terminal_prompt(prompt_session, label)).strip().lower()
        approved = answer in {"y", "yes", "s", "p"}
        decisions.append(
            {"type": "approve"}
            if approved
            else {"type": "reject", "message": "Declined in terminal"}
        )
        if approved and answer in {"s", "p"} and index < len(action_requests):
            tool_name = action_requests[index].get("name")
            if isinstance(tool_name, str) and tool_name:
                grants.append(
                    {"index": index, "scope": "session" if answer == "s" else "user",
                     "tool_name": tool_name}
                )
    action = "reject" if all(item["type"] == "reject" for item in decisions) else "approve"
    reply = app.command(
        action,
        {"thread_id": pending.get("thread_id") or getattr(app, "session_id", None),
         "decisions": decisions, "grants": grants},
    )
    return await reply if inspect.isawaitable(reply) else reply


async def _make_app(
    args: argparse.Namespace, factory: Callable[[argparse.Namespace], Any] | None
) -> Any:
    if factory is None:
        from .app import create_app  # supplied by the application layer

        factory = create_app
    app = factory(args)
    return await app if inspect.isawaitable(app) else app


async def _close_app(app: Any) -> None:
    for name in ("aclose", "close"):
        closer = getattr(app, name, None)
        if callable(closer):
            result = closer()
            if inspect.isawaitable(result):
                await result
            return


async def _headless(app: Any, args: argparse.Namespace) -> int:
    output_format = args.output_format
    try:
        prompt = sys.stdin.read().strip() if args.prompt == "-" else str(args.prompt).strip()
        if not prompt:
            raise ValueError("Prompt must not be empty")
        team_approval = _TEAM_APPROVAL.fullmatch(prompt)
        if team_approval is not None:
            _, task_id = team_approval.groups()
            pending = await _pending_team_approval(app, task_id)
            payload = {
                "ok": False,
                "status": "paused",
                "thread_id": pending.get("thread_id"),
                "task_id": task_id,
                "action_requests": pending["action_requests"],
                "error": "Interactive approval is required",
            }
            if output_format == "jsonl":
                writer = JsonlWriter(sys.stdout)
                writer.emit({"type": "run.started", "thread_id": pending.get("thread_id")})
                writer.emit({"type": "approval.requested", **payload})
                writer.emit({"type": "run.paused", **payload})
            elif output_format == "json":
                print(json.dumps(_redact(payload), ensure_ascii=False, default=str))
            else:
                print(format_result(_redact(payload)), file=sys.stderr)
            return 3
        if output_format == "jsonl":
            writer = JsonlWriter(sys.stdout)
            writer.emit(
                {
                    "type": "run.started",
                    "session_id": getattr(app, "session_id", None),
                    "workspace": str(args.workspace),
                }
            )
            if args.no_stream:
                result = await app.run(
                    prompt, session_id=args.session, mode=args.mode, input_format="headless"
                )
                payload = (
                    dict(result) if isinstance(result, dict) else {"ok": True, "response": str(result)}
                )
                payload = _with_task_outcome(payload, await _wait_for_tasks(app))
                for wake in payload.get("parent_wakes", []):
                    writer.emit(wake)
                writer.emit({"type": _terminal_type(payload), **payload})
                return _exit_code(payload)
            terminal: dict[str, Any] | None = None
            response_parts: list[str] = []
            emitted_task_events: set[tuple[str, str]] = set()
            observed_tasks: dict[str, dict[str, Any]] = {}
            stream = app.stream(
                prompt, session_id=args.session, mode=args.mode, input_format="headless"
            )
            if inspect.isawaitable(stream):
                stream = await stream
            try:
                async for event in stream:
                    if not isinstance(event, dict):
                        event = {"type": "assistant.delta", "delta": str(event)}
                    public = _public_event(event)
                    if public["type"] == "assistant.delta":
                        response_parts.append(str(public.get("delta") or ""))
                    if public["type"].startswith("task."):
                        task_id = str(public.get("task_id") or "")
                        emitted_task_events.add((task_id, public["type"]))
                        if task_id:
                            observed_tasks[task_id] = {
                                "task_id": task_id,
                                "status": str(public.get("status") or public["type"].removeprefix("task.")),
                                "error": public.get("error"),
                            }
                    if public["type"] in {"run.completed", "run.failed", "run.paused"}:
                        terminal = public
                    else:
                        writer.emit(public)
            finally:
                closer = getattr(stream, "aclose", None)
                if callable(closer):
                    await closer()
            tasks = await _wait_for_tasks(app)
            returned_ids = {str(task.get("task_id") or "") for task in tasks}
            tasks.extend(task for task_id, task in observed_tasks.items() if task_id not in returned_ids)
            for task in tasks:
                status = str(task.get("status") or "unknown")
                kind = f"task.{status}"
                identity = (str(task.get("task_id") or ""), kind)
                if identity not in emitted_task_events:
                    writer.emit(
                        {"type": kind, "task_id": task.get("task_id"), "status": status,
                         "error": task.get("error")}
                    )
                wake = task.get("parent_wake")
                if isinstance(wake, dict):
                    writer.emit(wake)
            if terminal is None:
                payload = {
                    "ok": False,
                    "status": "failed",
                    "error": "Stream ended without a terminal event",
                    "partial_response": "".join(response_parts),
                }
            else:
                payload = {key: value for key, value in terminal.items() if key != "type"}
                payload.setdefault("status", terminal["type"].removeprefix("run."))
                payload.setdefault("response", "".join(response_parts))
            payload = _with_task_outcome(payload, tasks)
            writer.emit({"type": _terminal_type(payload), **payload})
            return _exit_code(payload)
        result = await app.run(
            prompt, session_id=args.session, mode=args.mode, input_format="headless"
        )
        payload = dict(result) if isinstance(result, dict) else {"ok": True, "response": str(result)}
        payload = _with_task_outcome(payload, await _wait_for_tasks(app))
        code = _exit_code(payload)
        if output_format == "json":
            payload.setdefault("ok", code == 0)
            print(json.dumps(_redact(payload), ensure_ascii=False, default=str))
        elif code == 0:
            print(_response_text(payload))
        else:
            print(_response_text(payload) or format_result(payload), file=sys.stderr)
        return code
    except Exception as exc:
        payload = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
        if output_format == "jsonl":
            if "writer" not in locals():
                writer = JsonlWriter(sys.stdout)
            writer.emit({"type": "run.failed", **payload})
        elif output_format == "json":
            print(json.dumps(_redact(payload), ensure_ascii=False))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1


async def _interactive(app: Any, args: argparse.Namespace, preferences: PromptPreferences) -> int:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"^The v3 streaming protocol on Pregel is experimental\.$",
            category=Warning,
        )
        return await _interactive_body(app, args, preferences)


async def _interactive_body(
    app: Any, args: argparse.Namespace, preferences: PromptPreferences
) -> int:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.styles import Style
    from rich.console import Console

    console = Console(
        width=shutil.get_terminal_size((80, 24)).columns
        if not sys.stdout.isatty() else None
    )
    language = _terminal_language(preferences.language)
    presenter = TerminalPresenter(console, language=language, redact=_redact)
    history_path = _state_home() / "input_history"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history = FileHistory(str(history_path))
    append_history = history.append_string

    def safe_append_history(string: str) -> None:
        command = string.lstrip().lower()
        if command.startswith(("/config add ", "/model add ", "/mcp add ")):
            return
        if re.match(r"^/(?:model|config)\s+key(?:\s|$)", command):
            return
        append_history(string)

    history.append_string = safe_append_history  # type: ignore[method-assign]
    def completion_words() -> list[str]:
        commands = [f"/{name}" for name in BUILTIN_COMMANDS]
        commands.extend((
            "/team list", "/team pending", "/team approve", "/team reject",
            "/session list", "/mode build", "/mode plan", "/mode review",
        ))
        commands.extend(
            item.invocation
            for item in discover_custom_commands(
                Path(getattr(app, "workspace", args.workspace))
            ).values()
        )
        return sorted(set(commands))

    def toolbar() -> HTML:
        mode = escape(str(getattr(app, "mode", preferences.mode)).upper())
        model = escape(str(getattr(app, "model", None) or "—"))
        return HTML(
            f" <b>{mode}</b>  {model}  "
            + ("/help 命令  /quit 退出" if language == "zh" else "/help commands  /quit exit")
        )

    prompt_session: PromptSession[str] = PromptSession(
        history=history,
        completer=WordCompleter(completion_words, sentence=True, ignore_case=True),
        complete_while_typing=True,
        enable_history_search=True,
        bottom_toolbar=toolbar,
        style=Style.from_dict({
            "bottom-toolbar": "bg:#202532 #bfc6d4",
            "prompt": "bold #71d3e8",
        }),
    )
    router = CommandRouter(
        app,
        args.workspace,
        preferences,
        save_preferences=save_preferences,
        hooks=getattr(app, "hooks", None),
    )
    if not args.no_clear and sys.stdout.isatty():
        console.clear()
    presenter.header(
        version=_package_version(),
        workspace=Path(getattr(app, "workspace", args.workspace)).resolve(),
        model=getattr(app, "model", None),
        protocol=getattr(app, "protocol", None),
        mode=str(getattr(app, "mode", preferences.mode)),
        session_id=str(getattr(app, "session_id", "—")),
    )
    if _needs_profile_setup(app):
        presenter.notice(
            "尚未配置模型，请完成首次设置。"
            if language == "zh" else "No model profile is configured. Complete setup.",
            level="warning",
        )
        await _first_profile_wizard(
            app, prompt_session, console, language=language, presenter=presenter
        )
    watcher = getattr(app, "watch_notifications", None)
    if callable(watcher):
        from prompt_toolkit.application import run_in_terminal

        def show_notification(event: dict[str, Any]) -> Any:
            def render() -> None:
                if str(event.get("type") or "").startswith("agent.wake."):
                    presenter.agent_event(event)
                else:
                    presenter.task_event(event)

            prompt_app = getattr(prompt_session, "app", None)
            if (
                prompt_app is not None
                and prompt_app.is_running
                and prompt_app.context is not None
            ):
                return prompt_app.context.copy().run(
                    run_in_terminal, render
                )
            render()
            return None

        watcher(show_notification)
    while True:
        try:
            line = (await _terminal_prompt(prompt_session, "❯ ")).strip()
        except EOFError:
            return 0
        except KeyboardInterrupt:
            presenter.notice("已取消输入" if language == "zh" else "Input cancelled")
            continue
        if not line:
            continue
        if line.casefold() == "/model add":
            await _first_profile_wizard(
                app, prompt_session, console, language=language, presenter=presenter
            )
            continue
        if re.match(r"^/model\s+key(?:\s|$)", line, re.I):
            await _model_key_wizard(app, line, language=language, presenter=presenter)
            continue
        if re.fullmatch(r"/(?:approve|reject)(?:\s+\S+)?", line, re.I):
            parts = line.split()
            selected = parts[1] if len(parts) == 2 else getattr(app, "session_id", None)
            try:
                pending = app.pending_approval(selected)
                if inspect.isawaitable(pending):
                    pending = await pending
                if not pending.get("action_requests"):
                    presenter.notice(
                        "主会话没有待批准操作" if language == "zh"
                        else "No pending main-agent approval",
                        level="warning",
                    )
                    continue
                presenter.approval_intro(len(pending["action_requests"]))
                reply = await _resume_approval_from_terminal(
                    app, pending, prompt_session, language=language,
                    reject_all=parts[0].lower() == "/reject", presenter=presenter,
                )
                response = _response_text(reply)
                if response:
                    presenter.write_answer(response)
                    presenter.end_turn()
                elif isinstance(reply, dict) and reply.get("status") == "paused":
                    presenter.notice(
                        "仍有待批准操作" if language == "zh" else "Approval is still pending",
                        level="warning",
                    )
            except Exception as exc:
                presenter.notice(
                    f"审批失败：{exc}" if language == "zh" else f"Approval failed: {exc}",
                    level="error",
                )
            continue
        team_approval = _TEAM_APPROVAL.fullmatch(line)
        if team_approval is not None:
            action, task_id = team_approval.groups()
            try:
                pending = await _pending_team_approval(app, task_id)
                presenter.approval_intro(len(pending["action_requests"]))
                reply = await _resume_approval_from_terminal(
                    app,
                    pending,
                    prompt_session,
                    language=language,
                    reject_all=action.lower() == "reject",
                    presenter=presenter,
                )
                if isinstance(reply, dict) and _run_ok(reply):
                    presenter.notice(
                        "后台任务已继续" if language == "zh" else "Task resumed",
                        level="success",
                    )
                    if response := _response_text(reply):
                        presenter.write_answer(response)
                        presenter.end_turn()
                else:
                    presenter.command_result(line, format_result(reply))
            except Exception as exc:
                presenter.notice(
                    f"审批失败：{exc}" if language == "zh" else f"Approval failed: {exc}",
                    level="error",
                )
            continue
        try:
            command = await router.dispatch(line)
        except Exception as exc:
            presenter.notice(
                f"命令错误：{exc}" if language == "zh" else f"Command error: {exc}",
                level="error",
            )
            continue
        if command.exit:
            return 0
        if command.clear:
            console.clear()
            continue
        if line.split(maxsplit=1)[0].lower() == "/lang":
            language = _terminal_language(preferences.language)
            presenter.zh = language == "zh"
        if command.display:
            presenter.command_result(line, command.display)
        if command.prompt is None:
            continue
        try:
            stream = app.stream(
                command.prompt,
                session_id=getattr(app, "session_id", None),
                mode=getattr(app, "mode", preferences.mode),
                input_format="interactive",
            )
            if inspect.isawaitable(stream):
                stream = await stream
            pending_approval: dict[str, Any] | None = None
            paused = False
            printed_text = ""
            tool_started: dict[str, float] = {}
            presenter.start_wait()
            try:
                async for event in stream:
                    if not isinstance(event, dict):
                        event = {"type": "assistant.delta", "delta": str(event)}
                    public = _public_event(event)
                    kind = public["type"]
                    if kind == "assistant.delta":
                        delta = str(public.get("delta") or "")
                        printed_text += delta
                        presenter.write_answer(delta)
                    elif kind == "tool.started":
                        tool_name = str(public.get("tool_name") or "tool")
                        tool_id = str(public.get("tool_call_id") or tool_name)
                        tool_started[tool_id] = perf_counter()
                        presenter.tool_event(tool_name, "started")
                        presenter.start_wait(
                            f"正在执行 {tool_name}…" if language == "zh"
                            else f"Running {tool_name}…"
                        )
                    elif kind == "tool.completed":
                        tool_name = str(public.get("tool_name") or "tool")
                        tool_id = str(public.get("tool_call_id") or tool_name)
                        started = tool_started.pop(tool_id, None)
                        presenter.tool_event(
                            tool_name, "completed",
                            duration=perf_counter() - started if started is not None else None,
                        )
                        presenter.start_wait()
                    elif kind == "tool.failed":
                        tool_name = str(public.get("tool_name") or "tool")
                        tool_id = str(public.get("tool_call_id") or tool_name)
                        started = tool_started.pop(tool_id, None)
                        presenter.tool_event(
                            tool_name, "failed",
                            duration=perf_counter() - started if started is not None else None,
                        )
                        if public.get("error"):
                            presenter.notice(str(public["error"]), level="error")
                        presenter.start_wait()
                    elif kind.startswith("task."):
                        presenter.task_event(public)
                        presenter.start_wait()
                    elif kind.startswith("agent.wake."):
                        presenter.agent_event(public)
                    elif kind == "approval.requested":
                        pending_approval = public
                        actions = public.get("action_requests")
                        presenter.approval_intro(len(actions) if isinstance(actions, list) else 1)
                    elif kind == "run.failed":
                        error = str(public.get("error") or "")
                        presenter.notice(
                            f"运行失败：{error}" if language == "zh"
                            else f"Run failed: {error}",
                            level="error",
                        )
                    elif kind == "run.completed":
                        response = _response_text(public)
                        remaining = (
                            response[len(printed_text) :]
                            if response.startswith(printed_text)
                            else response
                        )
                        if remaining:
                            presenter.write_answer(remaining)
                            printed_text += remaining
                    elif kind == "run.paused":
                        paused = True
            finally:
                presenter.stop_wait()
                closer = getattr(stream, "aclose", None)
                if callable(closer):
                    await closer()
            if paused and pending_approval is not None:
                reply = await _resume_approval_from_terminal(
                    app, pending_approval, prompt_session,
                    language=language, presenter=presenter,
                )
                if isinstance(reply, dict):
                    response = _response_text(reply)
                    remaining = (
                        response[len(printed_text) :]
                        if response.startswith(printed_text)
                        else response
                    )
                    if remaining:
                        presenter.write_answer(remaining)
                    if not _run_ok(reply):
                        presenter.notice(
                            f"运行未完成：{reply.get('error') or reply.get('status')}"
                            if language == "zh" else
                            f"Run not completed: {reply.get('error') or reply.get('status')}",
                            level="error",
                        )
                elif reply is not None:
                    presenter.command_result("/approve", format_result(reply))
            elif paused:
                presenter.notice("运行已暂停" if language == "zh" else "Run paused", level="warning")
            presenter.end_turn()
        except (KeyboardInterrupt, asyncio.CancelledError):
            presenter.notice("已中断" if language == "zh" else "Interrupted", level="warning")
        except Exception as exc:
            presenter.notice(
                f"错误：{exc}" if language == "zh" else f"Error: {exc}", level="error"
            )


async def _first_profile_wizard(
    app: Any, prompt_session: Any, console: Any, *, language: str = "auto",
    presenter: TerminalPresenter | None = None,
) -> None:
    """Collect an explicit API protocol without putting credentials in input history."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory

    zh = language == "zh"
    protocols = tuple(
        (protocol, MODEL_PROTOCOL_LABELS.get(protocol, protocol))
        for protocol in SUPPORTED_MODEL_PROTOCOLS
    )

    def notice(message: str, *, level: str = "warning") -> None:
        if presenter is not None:
            presenter.notice(message, level=level)
        else:
            console.print(message, style="red" if level == "error" else "yellow")

    async def required(label: str) -> str:
        while True:
            value = (await _terminal_prompt(prompt_session, label)).strip()
            if value:
                return value
            notice("此项必填。" if zh else "This field is required.")

    async def positive_tokens(label: str) -> int:
        while True:
            value = await required(label)
            try:
                return _token_count(value)
            except argparse.ArgumentTypeError as exc:
                notice(str(exc))

    try:
        menu = "\n".join(f"  {index}. {label}" for index, (_, label) in enumerate(protocols, 1))
        console.print(("接口协议：\n" if zh else "API protocol:\n") + menu)
        while True:
            chosen = await required(
                "[1/6] 协议编号：" if zh else "[1/6] Protocol number: "
            )
            if chosen.isdecimal() and 1 <= int(chosen) <= len(protocols):
                protocol = protocols[int(chosen) - 1][0]
                break
            notice("请选择列表中的编号。" if zh else "Choose a number from the list.")
        while True:
            base_url = await required(
                "[2/6] 接口地址 (https://...)：" if zh
                else "[2/6] Base URL (https://...): "
            )
            parsed = urlsplit(base_url)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                break
            notice("请输入完整的 http(s) 地址。" if zh else "Enter a complete http(s) URL.")
        notice(
            "API Key 必填。仅无认证接口可输入 none；留空不会使用环境变量。"
            if zh else
            "An API key is required. Enter none only for an unauthenticated endpoint; blank never uses an environment variable."
        )
        secret_session: PromptSession[str] = PromptSession(history=InMemoryHistory())
        while True:
            entered_key = (
                await _terminal_prompt(
                    secret_session,
                    "[3/6] API Key（无认证输入 none）：" if zh
                    else "[3/6] API key (enter none for no authentication): ",
                    is_password=True,
                )
            ).strip()
            if not entered_key:
                notice("请输入 API Key；仅无认证接口输入 none。" if zh else
                       "Enter an API key, or none for an unauthenticated endpoint.")
                continue
            if entered_key.lower().startswith("env:"):
                notice("不支持环境变量引用，请输入真实密钥。" if zh else
                       "Environment-variable references are not supported; enter the actual key.")
                continue
            api_key = None if entered_key.casefold() == "none" else entered_key
            break
        model_id = await required(
            "[4/6] 模型 ID：" if zh else "[4/6] Model ID: "
        )
        context_length = await positive_tokens(
            "[5/6] 上下文长度（token）：" if zh
            else "[5/6] Context length (tokens): "
        )
        max_output_tokens = await positive_tokens(
            "[6/6] 最大输出（token）：" if zh
            else "[6/6] Maximum output (tokens): "
        )
        result = app.command(
            "model",
            {
                "action": "add",
                "profile": {
                    "protocol": protocol,
                    "base_url": base_url,
                    "api_key": api_key,
                    "model_id": model_id,
                    "context_length": context_length,
                    "max_output_tokens": max_output_tokens,
                },
            },
        )
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, dict) and result.get("ok") is False:
            error = str(result.get("error") or "Model profile was not saved.")
            if api_key:
                error = error.replace(api_key, "***")
            notice(error, level="error")
            return
        name = result.get("added") if isinstance(result, dict) else None
        if presenter is not None:
            presenter.notice(
                "模型配置已保存" if zh else "Model profile saved", level="success"
            )
            if name:
                presenter.notice(
                    f"配置名：{name}。用 /models 查看列表；/model use {name} 切换。"
                    if zh else
                    f"Profile: {name}. Use /models to list; /model use {name} to switch.",
                )
        else:
            console.print(format_result(result), markup=False)
    except (EOFError, KeyboardInterrupt):
        notice("已跳过设置；稍后可使用 /model add。" if zh
               else "Setup skipped; use /model add later.")
    except (ValueError, TypeError) as exc:
        error = str(exc)
        if "api_key" in locals() and api_key:
            error = error.replace(api_key, "***")
        notice(f"模型配置未保存：{error}" if zh else f"Model profile not saved: {error}",
               level="error")


async def _model_key_wizard(
    app: Any, command: str, *, language: str, presenter: TerminalPresenter,
) -> None:
    """Update a profile key without accepting the key on the visible command line."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory

    zh = language == "zh"
    parts = command.split()
    if len(parts) != 3:
        presenter.notice(
            "用法：/model key <配置名>。密钥将在隐藏输入框中填写，不要写在命令后面。"
            if zh else
            "Usage: /model key <profile>. Enter the key in the hidden prompt, not in the command.",
            level="warning",
        )
        return
    name = parts[2]
    presenter.notice(
        "请输入真实 API Key；仅无认证接口输入 none 清除密钥。"
        if zh else
        "Enter the actual API key; enter none only to clear it for an unauthenticated endpoint."
    )
    secret_session: PromptSession[str] = PromptSession(history=InMemoryHistory())
    try:
        while True:
            api_key = (
                await _terminal_prompt(
                    secret_session,
                    "API Key（隐藏输入；无认证输入 none）：" if zh
                    else "API key (hidden; enter none for no authentication): ",
                    is_password=True,
                )
            ).strip()
            if not api_key:
                presenter.notice(
                    "请输入 API Key；仅无认证接口输入 none。" if zh else
                    "Enter an API key, or none for an unauthenticated endpoint."
                )
                continue
            if api_key.lower().startswith("env:"):
                presenter.notice(
                    "不支持环境变量引用，请输入真实密钥。" if zh else
                    "Environment-variable references are not supported; enter the actual key."
                )
                continue
            break
    except (EOFError, KeyboardInterrupt):
        presenter.notice("已取消" if zh else "Cancelled")
        return
    submitted_key = None if api_key.casefold() == "none" else api_key
    try:
        result = app.command(
            "model", {"action": "set_key", "name": name, "api_key": submitted_key}
        )
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, dict) and result.get("ok") is False:
            error = str(result.get("error") or "unknown error")
            if submitted_key:
                error = error.replace(submitted_key, "***")
            presenter.notice(
                f"密钥未保存：{error}" if zh else f"Key not saved: {error}",
                level="error",
            )
            return
        presenter.notice(
            f"{name} 的密钥已更新。可用 /model test {name} 验证。" if zh else
            f"Key updated for {name}. Use /model test {name} to verify.",
            level="success",
        )
    except Exception as exc:
        error = str(exc)
        if submitted_key:
            error = error.replace(submitted_key, "***")
        presenter.notice(
            f"密钥未保存：{error}" if zh else f"Key not saved: {error}",
            level="error",
        )


async def amain(
    argv: list[str] | None = None,
    *,
    app_factory: Callable[[argparse.Namespace], Any] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    args.workspace = args.workspace.expanduser().resolve()
    if not args.workspace.is_dir():
        print(f"Workspace does not exist: {args.workspace}", file=sys.stderr)
        return 2
    preferences = load_preferences()
    explicit_preferences = any(value is not None for value in (args.lang, args.style, args.mode))
    try:
        if args.lang:
            preferences.language = normalize_language(args.lang)
        if args.style:
            preferences.style = normalize_style(args.style)
        if args.mode:
            preferences.mode = normalize_mode(args.mode)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    args.lang = preferences.language
    args.style = preferences.style
    # Leave the CLI mode unset unless the user explicitly chose one. A selected
    # session may carry its own mode; the application restores that mode.
    if args.prompt is not None and args.new_session and args.session:
        print("--session and --new-session cannot be used together", file=sys.stderr)
        return 2
    if args.prompt is None and not args.doctor and not sys.stdin.isatty():
        print("Use -p for a non-interactive run.", file=sys.stderr)
        return 2
    if explicit_preferences:
        save_preferences(preferences)
    try:
        app = await _make_app(args, app_factory)
    except Exception as exc:
        config_error = isinstance(exc, (ValueError, KeyError))
        if args.output_format == "json" or args.json:
            print(
                json.dumps(
                    {
                        "ok": False, "error": str(exc),
                        "error_type": type(exc).__name__,
                        "status": "config_error" if config_error else "failed",
                    },
                    ensure_ascii=False,
                )
            )
        elif args.output_format == "jsonl":
            JsonlWriter(sys.stdout).emit(
                {
                    "type": "run.failed",
                    "ok": False,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
            )
        else:
            print(f"Startup error: {exc}", file=sys.stderr)
        return 2 if config_error else 1
    try:
        if args.doctor:
            try:
                result = app.command("doctor", str(args.bundle) if args.bundle else "")
                if inspect.isawaitable(result):
                    result = await result
                if args.json:
                    print(json.dumps(_redact(result), ensure_ascii=False, default=str))
                else:
                    print(format_result(result))
                return 0 if _run_ok(result) else 1
            except Exception as exc:
                payload = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
                if args.json:
                    print(json.dumps(_redact(payload), ensure_ascii=False))
                else:
                    print(f"Doctor error: {exc}", file=sys.stderr)
                return 1
        if args.prompt is not None:
            return await _headless(app, args)
        return await _interactive(app, args, preferences)
    finally:
        await _close_app(app)


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(amain(argv))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
