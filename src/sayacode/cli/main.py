"""命令行参数、应用生命周期与退出码。"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from pathlib import Path
from typing import Any, Callable

from ..prompts import normalize_language, normalize_style
from .commands import format_result
from .events import JsonlWriter, _redact, _run_ok
from .headless import _headless
from .interactive import _interactive
from .model_setup import _token_count
from .preferences import _package_version, load_preferences, save_preferences


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器，覆盖交互与无头常用开关。
    无参数，返回配好的解析器对象。
    互斥的认证开关与会话开关在此约束，调用方只管解析。"""
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
        "--no-api-key",
        action="store_true",
        help="Explicitly use an unauthenticated endpoint",
    )
    parser.add_argument("--context-length", type=_token_count)
    parser.add_argument("--max-output-tokens", type=_token_count)
    parser.add_argument("--session")
    parser.add_argument("--new-session", action="store_true")
    parser.add_argument("--trust", choices=("read_only", "ask", "full"))
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


async def _make_app(
    args: argparse.Namespace, factory: Callable[[argparse.Namespace], Any] | None
) -> Any:
    """按参数建出应用对象，兼容同步与异步工厂。
    参数是解析后参数与可选工厂，返回建好的应用。
    缺省工厂走应用层创建，测试可注入假对象。"""
    if factory is None:
        from ..application import create_app  # 由应用层提供

        factory = create_app
    app = factory(args)
    return await app if inspect.isawaitable(app) else app


async def _close_app(app: Any) -> None:
    """收尾应用对象，优先调异步关闭再试同步关闭。
    参数是应用对象，返回无。
    两种关闭都不存在时直接返回，不抛错。"""
    for name in ("aclose", "close"):
        closer = getattr(app, name, None)
        if callable(closer):
            result = closer()
            if inspect.isawaitable(result):
                await result
            return


async def amain(
    argv: list[str] | None = None,
    *,
    app_factory: Callable[[argparse.Namespace], Any] | None = None,
) -> int:
    """异步主入口，负责参数校验分发与退出码。
    参数是可选参数表与应用工厂，返回进程退出码。
    流程分四段，先校验工作区与语言风格，再拦非法组合与非交互误用，接着建应用并按诊断无头交互分流，最后统一关闭应用。
    坑点是配置错给二，启动错按类型给一或二，中断交由同步入口处理。"""
    args = build_parser().parse_args(argv)
    args.workspace = args.workspace.expanduser().resolve()
    if not args.workspace.is_dir():
        print(f"Workspace does not exist: {args.workspace}", file=sys.stderr)
        return 2
    preferences = load_preferences()
    explicit_preferences = any(value is not None for value in (args.lang, args.style))
    try:
        if args.lang:
            preferences.language = normalize_language(args.lang)
        if args.style:
            preferences.style = normalize_style(args.style)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    args.lang = preferences.language
    args.style = preferences.style
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
                        "ok": False,
                        "error": str(exc),
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
    """同步命令行入口，包一层事件循环并处理中断。
    参数是可选参数表，返回进程退出码。
    键盘中断固定返回一百三，方便脚本识别。"""
    try:
        return asyncio.run(amain(argv))
    except KeyboardInterrupt:
        return 130
