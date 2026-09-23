"""Jev 自动审理的独立配置向导。"""

from __future__ import annotations

import inspect
import shlex
from dataclasses import asdict
from typing import Any

from ..approvals import JevReviewer
from ..config import JevConfig
from .input import _terminal_prompt


async def reviewer_command(app: Any, args: Any) -> dict[str, Any]:
    """管理独立的 Jev 审理配置，密钥从不回显。"""
    if isinstance(args, dict):
        if args.get("action") != "setup" or not isinstance(args.get("config"), dict):
            raise ValueError("Reviewer command object must contain action=setup and config")
        raw = args["config"]
        expected = {"base_url", "api_key", "model_id"}
        if set(raw) != expected:
            raise ValueError(f"Reviewer config requires exactly: {', '.join(sorted(expected))}")
        app.config.jev = JevConfig(**raw)
        app._handles.clear()
        await app._save_config()
        item = asdict(app.config.jev)
        item["api_key"] = "***"
        return {"configured": True, **item}
    tokens = shlex.split(str(args or ""))
    action = tokens[0].lower() if tokens else "status"
    if action in {"status", "show"}:
        if app.config.jev is None:
            return {"configured": False, "trust_level": app.trust_level}
        item = asdict(app.config.jev)
        item["api_key"] = "***"
        return {"configured": True, **item, "trust_level": app.trust_level}
    if action == "test":
        if app.config.jev is None:
            raise ValueError("Jev reviewer is not configured; use /reviewer setup")
        return {
            "ok": True,
            "configured": True,
            **await JevReviewer(app.config.jev).test(),
        }
    if action in {"remove", "delete"}:
        app.config.jev = None
        if app.config.default_trust == "jev":
            app.config.default_trust = "ask"
        policy = await app._load_thread_policy(app.session_id)
        if policy.trust_level == "jev":
            policy.trust_level = "ask"
            app.trust_level = "ask"
            await app._save_thread_policy(app.session_id, trust_level="ask")
        app._handles.clear()
        await app._save_config()
        return {"removed": True, "trust_level": app.trust_level}
    if action == "setup":
        raise ValueError("Use interactive /reviewer setup to enter the API key")
    raise ValueError("Usage: /reviewer [status|setup|test|remove]")


async def reviewer_setup_wizard(
    app: Any, prompt_session: Any, *, language: str, presenter: Any
) -> None:
    """通过隐藏输入收集 Jev 端点、密钥和版本化模型编号。"""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import InMemoryHistory

    zh = language == "zh"
    presenter.wizard(
        "配置 Jev 自动审理" if zh else "Configure Jev review",
        "Jev 会接收用户目标、当前计划和待审理工具参数；已知密钥字段会脱敏。"
        if zh
        else "Jev receives the user goal, current plan, and proposed tool arguments; known secret fields are redacted.",
    )
    try:
        base_url = (
            await _terminal_prompt(
                prompt_session,
                "[1/3] Jev 接口地址 [https://api.typesafe.ai]："
                if zh
                else "[1/3] Jev API URL [https://api.typesafe.ai]: ",
            )
        ).strip() or "https://api.typesafe.ai"
        secret_session: PromptSession[str] = PromptSession(history=InMemoryHistory())
        while True:
            api_key = (
                await _terminal_prompt(
                    secret_session,
                    "[2/3] TypeSafe API Key：" if zh else "[2/3] TypeSafe API key: ",
                    is_password=True,
                )
            ).strip()
            if not api_key:
                presenter.notice("API Key 必填。" if zh else "The API key is required.")
                continue
            if api_key.casefold().startswith("env:"):
                presenter.notice(
                    "不支持环境变量引用，请输入真实密钥。"
                    if zh
                    else "Environment references are unsupported; enter the actual key."
                )
                continue
            break
        model_id = (
            await _terminal_prompt(
                prompt_session,
                "[3/3] Jev 模型 ID [jev-1.13.0]：" if zh else "[3/3] Jev model ID [jev-1.13.0]: ",
            )
        ).strip() or "jev-1.13.0"
        result = app.command(
            "reviewer",
            {
                "action": "setup",
                "config": {"base_url": base_url, "api_key": api_key, "model_id": model_id},
            },
        )
        if inspect.isawaitable(result):
            result = await result
        presenter.notice(
            "Jev 自动审理已配置。使用 /reviewer test 验证，再用 /trust jev 启用。"
            if zh
            else "Jev review is configured. Run /reviewer test, then enable it with /trust jev.",
            level="success",
        )
    except (EOFError, KeyboardInterrupt):
        presenter.notice("已取消" if zh else "Cancelled")
    except Exception as exc:
        error = str(exc).replace(api_key, "***") if "api_key" in locals() else str(exc)
        presenter.notice(
            f"Jev 配置未保存：{error}" if zh else f"Jev configuration was not saved: {error}",
            level="error",
        )


__all__ = ["reviewer_command", "reviewer_setup_wizard"]
