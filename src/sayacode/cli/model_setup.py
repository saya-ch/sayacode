"""模型协议向导和隐藏凭据输入。"""

from __future__ import annotations

import argparse
import inspect
import re
from typing import Any
from urllib.parse import urlsplit

from ..config import SUPPORTED_MODEL_PROTOCOLS
from .commands import format_result
from .display import MODEL_PROTOCOL_LABELS, TerminalPresenter
from .input import _terminal_prompt


def _token_count(value: str) -> int:
    match = re.fullmatch(r"([1-9][0-9]*)([kKmM]?)", value.strip())
    if match is None:
        raise argparse.ArgumentTypeError(
            "token count must be a positive number, e.g. 128000 or 256k"
        )
    amount = int(match.group(1)) * {"": 1, "k": 1000, "m": 1000000}[match.group(2).lower()]
    return amount


async def _first_profile_wizard(
    app: Any,
    prompt_session: Any,
    console: Any,
    *,
    language: str = "auto",
    presenter: TerminalPresenter | None = None,
) -> None:
    """收集接口协议且不让密钥进入输入历史。"""
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
            chosen = await required("[1/6] 协议编号：" if zh else "[1/6] Protocol number: ")
            if chosen.isdecimal() and 1 <= int(chosen) <= len(protocols):
                protocol = protocols[int(chosen) - 1][0]
                break
            notice("请选择列表中的编号。" if zh else "Choose a number from the list.")
        while True:
            base_url = await required(
                "[2/6] 接口地址 (https://...)：" if zh else "[2/6] Base URL (https://...): "
            )
            parsed = urlsplit(base_url)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                break
            notice("请输入完整的 http(s) 地址。" if zh else "Enter a complete http(s) URL.")
        notice(
            "API Key 必填。仅无认证接口可输入 none；留空不会使用环境变量。"
            if zh
            else "An API key is required. Enter none only for an unauthenticated endpoint; blank never uses an environment variable."
        )
        secret_session: PromptSession[str] = PromptSession(history=InMemoryHistory())
        while True:
            entered_key = (
                await _terminal_prompt(
                    secret_session,
                    "[3/6] API Key（无认证输入 none）："
                    if zh
                    else "[3/6] API key (enter none for no authentication): ",
                    is_password=True,
                )
            ).strip()
            if not entered_key:
                notice(
                    "请输入 API Key；仅无认证接口输入 none。"
                    if zh
                    else "Enter an API key, or none for an unauthenticated endpoint."
                )
                continue
            if entered_key.lower().startswith("env:"):
                notice(
                    "不支持环境变量引用，请输入真实密钥。"
                    if zh
                    else "Environment-variable references are not supported; enter the actual key."
                )
                continue
            api_key = None if entered_key.casefold() == "none" else entered_key
            break
        model_id = await required("[4/6] 模型 ID：" if zh else "[4/6] Model ID: ")
        context_length = await positive_tokens(
            "[5/6] 上下文长度（token）：" if zh else "[5/6] Context length (tokens): "
        )
        max_output_tokens = await positive_tokens(
            "[6/6] 最大输出（token）：" if zh else "[6/6] Maximum output (tokens): "
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
            presenter.notice("模型配置已保存" if zh else "Model profile saved", level="success")
            if name:
                presenter.notice(
                    f"配置名：{name}。用 /models 查看列表；/model use {name} 切换。"
                    if zh
                    else f"Profile: {name}. Use /models to list; /model use {name} to switch.",
                )
        else:
            console.print(format_result(result), markup=False)
    except (EOFError, KeyboardInterrupt):
        notice(
            "已跳过设置；稍后可使用 /model add。" if zh else "Setup skipped; use /model add later."
        )
    except (ValueError, TypeError) as exc:
        error = str(exc)
        if "api_key" in locals() and api_key:
            error = error.replace(api_key, "***")
        notice(
            f"模型配置未保存：{error}" if zh else f"Model profile not saved: {error}", level="error"
        )


async def _model_key_wizard(
    app: Any,
    command: str,
    *,
    language: str,
    presenter: TerminalPresenter,
) -> None:
    """在隐藏输入中更新密钥而不经可见命令行。"""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory

    zh = language == "zh"
    parts = command.split()
    if len(parts) != 3:
        presenter.notice(
            "用法：/model key <配置名>。密钥将在隐藏输入框中填写，不要写在命令后面。"
            if zh
            else "Usage: /model key <profile>. Enter the key in the hidden prompt, not in the command.",
            level="warning",
        )
        return
    name = parts[2]
    presenter.notice(
        "请输入真实 API Key；仅无认证接口输入 none 清除密钥。"
        if zh
        else "Enter the actual API key; enter none only to clear it for an unauthenticated endpoint."
    )
    secret_session: PromptSession[str] = PromptSession(history=InMemoryHistory())
    try:
        while True:
            api_key = (
                await _terminal_prompt(
                    secret_session,
                    "API Key（隐藏输入；无认证输入 none）："
                    if zh
                    else "API key (hidden; enter none for no authentication): ",
                    is_password=True,
                )
            ).strip()
            if not api_key:
                presenter.notice(
                    "请输入 API Key；仅无认证接口输入 none。"
                    if zh
                    else "Enter an API key, or none for an unauthenticated endpoint."
                )
                continue
            if api_key.lower().startswith("env:"):
                presenter.notice(
                    "不支持环境变量引用，请输入真实密钥。"
                    if zh
                    else "Environment-variable references are not supported; enter the actual key."
                )
                continue
            break
    except (EOFError, KeyboardInterrupt):
        presenter.notice("已取消" if zh else "Cancelled")
        return
    submitted_key = None if api_key.casefold() == "none" else api_key
    try:
        result = app.command("model", {"action": "set_key", "name": name, "api_key": submitted_key})
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
            f"{name} 的密钥已更新。可用 /model test {name} 验证。"
            if zh
            else f"Key updated for {name}. Use /model test {name} to verify.",
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
