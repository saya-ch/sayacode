"""命令行参数、应用生命周期与退出码。"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable

from ..prompts import normalize_language
from .events import JsonlWriter, _redact, _run_ok
from .headless import _format_result, _headless
from .preferences import _package_version, load_preferences, save_preferences


def _token_count(value: str) -> int:
    """解析无头模式的 token 数，接受整数以及 k、m 后缀。"""
    match = re.fullmatch(r"([1-9][0-9]*)([kKmM]?)", value.strip())
    if match is None:
        raise argparse.ArgumentTypeError(
            "token count must be a positive number, e.g. 128000 or 256k"
        )
    amount = int(match.group(1)) * {"": 1, "k": 1000, "m": 1000000}[match.group(2).lower()]
    return amount


def _port_number(raw: str) -> int:
    try:
        port = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("端口必须是 0 到 65535 的整数") from error
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须是 0 到 65535 的整数")
    return port


def build_parser() -> argparse.ArgumentParser:
    """构建 Web 入口、诊断和无头运行的参数解析器。
    无参数，返回配好的解析器对象。
    互斥的认证开关与会话开关在此约束，调用方只管解析。"""
    parser = argparse.ArgumentParser(
        prog="sayacode", description="Local Web coding agent", allow_abbrev=False
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
    parser.add_argument("--trust", choices=("read_only", "ask", "jev", "full"))
    parser.add_argument("--lang", choices=("auto", "zh", "en"))
    parser.add_argument("--skill", help="Activate a Skill for this run")
    parser.add_argument("-p", "--prompt", help="Run one prompt and exit; '-' reads stdin")
    parser.add_argument("--output-format", choices=("text", "json", "jsonl"), default="text")
    parser.add_argument("--no-stream", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    parser.add_argument("--json", action="store_true", help="JSON output for --doctor")
    parser.add_argument("--bundle", type=Path, help="Write a support bundle with --doctor")
    parser.add_argument("--port", type=_port_number, help="本机 Web 端口；0 自动选择")
    parser.add_argument("--no-open", action="store_true", help="启动 WebUI 但不打开浏览器")
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
    """普通入口启动 WebUI；`-p` 和 `--doctor` 保留无头执行。"""
    args = build_parser().parse_args(argv)
    args.workspace = args.workspace.expanduser().resolve()
    if not args.workspace.is_dir():
        print(f"Workspace does not exist: {args.workspace}", file=sys.stderr)
        return 2
    if args.prompt is not None and args.doctor:
        print("-p/--prompt and --doctor cannot be used together", file=sys.stderr)
        return 2
    if args.skill and args.prompt is None:
        print("--skill requires -p/--prompt", file=sys.stderr)
        return 2
    if args.prompt is None and not args.doctor:
        headless_options = {
            "--profile": args.profile,
            "--protocol": args.protocol,
            "--model-id": args.model_id,
            "--base-url": args.base_url,
            "--api-key": args.api_key,
            "--no-api-key": args.no_api_key,
            "--context-length": args.context_length,
            "--max-output-tokens": args.max_output_tokens,
            "--session": args.session,
            "--new-session": args.new_session,
            "--trust": args.trust,
            "--lang": args.lang,
            "--no-stream": args.no_stream,
            "--json": args.json,
            "--bundle": args.bundle,
            "--output-format": args.output_format != "text",
        }
        unsupported = [
            name for name, value in headless_options.items()
            if value is not None and value is not False
        ]
        if unsupported:
            print(
                f"{', '.join(unsupported)} 仅用于 -p/--doctor；WebUI 设置请在浏览器中修改",
                file=sys.stderr,
            )
            return 2
        from .web import serve_web

        try:
            return await serve_web(
                args.workspace, port=args.port if args.port is not None else 0,
                open_browser=not args.no_open,
            )
        except (ValueError, KeyError, FileNotFoundError) as exc:
            print(f"Web 配置错误：{exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"Web 启动失败：{exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"Web 运行失败：{exc}", file=sys.stderr)
            return 1
    if args.no_open or args.port is not None:
        print("--no-open and --port only apply to the WebUI", file=sys.stderr)
        return 2
    preferences = load_preferences()
    explicit_preferences = args.lang is not None
    try:
        if args.lang:
            preferences.language = normalize_language(args.lang)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    args.lang = preferences.language
    if args.prompt is not None and args.new_session and args.session:
        print("--session and --new-session cannot be used together", file=sys.stderr)
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
                result = app._doctor(str(args.bundle) if args.bundle else "")
                if inspect.isawaitable(result):
                    result = await result
                if args.json:
                    print(json.dumps(_redact(result), ensure_ascii=False, default=str))
                else:
                    print(_format_result(result))
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
        raise AssertionError("无头运行入口缺少任务或诊断参数")
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
