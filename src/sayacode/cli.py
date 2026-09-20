"""Async terminal and one-shot entry points for SAYACODE 2.0."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import re
import shlex
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable, TextIO
from uuid import uuid4

from .commands import CommandRouter, format_result
from .prompts import PromptPreferences, normalize_language, normalize_mode, normalize_style

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


def _context_window(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([kKmM]?)", value.strip())
    if match is None:
        raise argparse.ArgumentTypeError(
            "context window must be a positive number, e.g. 128000 or 256k"
        )
    amount = int(match.group(1)) * {"": 1, "k": 1000, "m": 1000000}[match.group(2).lower()]
    return amount


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sayacode", description="Async terminal coding agent")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--profile", help="Use a named saved model profile")
    parser.add_argument("--model", help="Model identifier, for example openai:gpt-5.4-mini")
    parser.add_argument("--model-type")
    parser.add_argument("--model-name")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key")
    parser.add_argument("--context-window", type=_context_window)
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
    if not _run_ok(merged):
        return merged
    paused = [task for task in tasks if task.get("status") in {"paused", "interrupted"}]
    failed = [task for task in tasks if task.get("status") in {"failed", "error", "stopped"}]
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
) -> Any:
    actions = pending.get("action_requests")
    action_requests = actions if isinstance(actions, list) else []
    count = len(action_requests) or 1
    decisions: list[dict[str, str]] = []
    grants: list[dict[str, Any]] = []
    for index in range(count):
        if reject_all:
            answer = "n"
        else:
            label = (
                f"批准操作 {index + 1}/{count}？[y 仅本次 / s 本会话 / p 长期 / N 拒绝] "
                if language == "zh"
                else f"Approve action {index + 1}/{count}? "
                "[y once / s session / p permanent / N] "
            )
            answer = (await prompt_session.prompt_async(label)).strip().lower()
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
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory
    from rich.console import Console

    console = Console()
    history_path = _state_home() / "input_history"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history = FileHistory(str(history_path))
    append_history = history.append_string

    def safe_append_history(string: str) -> None:
        command = string.lstrip().lower()
        if command.startswith(("/config add ", "/model add ", "/mcp add ")):
            return
        append_history(string)

    history.append_string = safe_append_history  # type: ignore[method-assign]
    prompt_session: PromptSession[str] = PromptSession(history=history)
    router = CommandRouter(
        app,
        args.workspace,
        preferences,
        save_preferences=save_preferences,
        hooks=getattr(app, "hooks", None),
    )
    if not args.no_clear and sys.stdout.isatty():
        console.clear()
    console.print(f"[bold magenta]SAYACODE {_package_version()}[/]  {args.workspace}")
    console.print(
        "输入 /help 查看命令；输入 /quit 退出。"
        if preferences.language == "zh"
        else "Type /help for commands; /quit to exit.",
        style="dim",
    )
    if _needs_profile_setup(app):
        console.print(
            "尚未配置模型，请完成首次设置。"
            if preferences.language == "zh"
            else "No model profile is configured. Complete the first-run setup.",
            style="yellow",
        )
        await _first_profile_wizard(app, prompt_session, console, language=preferences.language)
    watcher = getattr(app, "watch_notifications", None)
    if callable(watcher):
        watcher(
            lambda event: console.print(
                f"\n[{event.get('type', 'task.event')}] {event.get('task_id', '')} "
                f"{event.get('status', '')}",
                style="dim",
            )
        )
    while True:
        try:
            line = (await prompt_session.prompt_async("❯ ")).strip()
        except EOFError:
            return 0
        except KeyboardInterrupt:
            console.print("^C", style="dim")
            continue
        if not line:
            continue
        team_approval = _TEAM_APPROVAL.fullmatch(line)
        if team_approval is not None:
            action, task_id = team_approval.groups()
            try:
                pending = await _pending_team_approval(app, task_id)
                console.print(format_result(_redact(pending)), markup=False)
                reply = await _resume_approval_from_terminal(
                    app,
                    pending,
                    prompt_session,
                    language=preferences.language,
                    reject_all=action.lower() == "reject",
                )
                console.print(format_result(reply), markup=False)
            except Exception as exc:
                console.print(f"Approval error: {exc}", style="red")
            continue
        try:
            command = await router.dispatch(line)
        except Exception as exc:
            console.print(f"Command error: {exc}", style="red")
            continue
        if command.exit:
            return 0
        if command.clear:
            console.clear()
            continue
        if command.display:
            console.print(command.display, markup=False)
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
            try:
                async for event in stream:
                    if not isinstance(event, dict):
                        event = {"type": "assistant.delta", "delta": str(event)}
                    public = _public_event(event)
                    kind = public["type"]
                    if kind == "assistant.delta":
                        delta = str(public.get("delta") or "")
                        printed_text += delta
                        console.print(delta, end="", markup=False)
                    elif kind == "tool.started":
                        console.print(f"\n↳ {public.get('tool_name', 'tool')}…", style="dim")
                    elif kind == "tool.failed":
                        console.print(
                            f"\nTool failed: {public.get('tool_name', 'tool')}", style="red"
                        )
                    elif kind == "approval.requested":
                        pending_approval = public
                        console.print("\nApproval requested:", style="yellow")
                        console.print(format_result(public), markup=False)
                    elif kind == "run.failed":
                        console.print(f"\n{public.get('error', 'Run failed')}", style="red")
                    elif kind == "run.completed":
                        response = _response_text(public)
                        remaining = (
                            response[len(printed_text) :]
                            if response.startswith(printed_text)
                            else response
                        )
                        if remaining:
                            console.print(remaining, end="", markup=False)
                            printed_text += remaining
                    elif kind == "run.paused":
                        paused = True
            finally:
                closer = getattr(stream, "aclose", None)
                if callable(closer):
                    await closer()
            if paused and pending_approval is not None:
                reply = await _resume_approval_from_terminal(
                    app, pending_approval, prompt_session, language=preferences.language
                )
                if isinstance(reply, dict):
                    response = _response_text(reply)
                    remaining = (
                        response[len(printed_text) :]
                        if response.startswith(printed_text)
                        else response
                    )
                    if remaining:
                        console.print(remaining, end="", markup=False)
                    if not _run_ok(reply):
                        console.print(f"\n{reply.get('error') or reply.get('status')}", style="red")
                elif reply is not None:
                    console.print(format_result(reply), markup=False)
            elif paused:
                console.print("\nRun paused.", style="yellow")
            console.print()
        except (KeyboardInterrupt, asyncio.CancelledError):
            console.print("\nInterrupted.", style="yellow")
        except Exception as exc:
            console.print(f"\nError: {exc}", style="red")


async def _first_profile_wizard(
    app: Any, prompt_session: Any, console: Any, *, language: str = "auto"
) -> None:
    """Create the first profile without reintroducing a second config system."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory

    zh = language == "zh"
    try:
        name = (
            await prompt_session.prompt_async("配置名称 [default]：" if zh else "Profile name [default]: ")
        ).strip() or "default"
        provider = (
            await prompt_session.prompt_async("模型接口 [openai]：" if zh else "Provider [openai]: ")
        ).strip() or "openai"
        model = (await prompt_session.prompt_async("模型名称：" if zh else "Model: ")).strip()
        if not model:
            console.print(
                "已跳过设置；准备好模型后可使用 /config add。"
                if zh else "Setup skipped; use /config add when a model is available.",
                style="yellow",
            )
            return
        base_url = (
            await prompt_session.prompt_async(
                "基础地址 [接口默认]：" if zh else "Base URL [provider default]: "
            )
        ).strip()
        secret_session: PromptSession[str] = PromptSession(history=InMemoryHistory())
        api_key = (
            await secret_session.prompt_async(
                "API Key [环境变量/默认]：" if zh else "API key [environment/default]: ",
                is_password=True,
            )
        ).strip()
        pieces = ["add", name, provider, model]
        if base_url or api_key:
            pieces.append(base_url)
        if api_key:
            pieces.append(api_key)
        command = " ".join(shlex.quote(piece) for piece in pieces)
        result = app.command("config", command)
        if inspect.isawaitable(result):
            result = await result
        console.print(format_result(result), markup=False)
    except (EOFError, KeyboardInterrupt):
        console.print(
            "已跳过设置；稍后可使用 /config add。" if zh else "Setup skipped; use /config add later.",
            style="yellow",
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
        if args.output_format == "json" or args.json:
            print(
                json.dumps(
                    {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
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
        return 1
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
