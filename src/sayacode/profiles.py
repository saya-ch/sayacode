"""模型配置、能力验证和终端运行参数。"""

from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from langchain.tools import tool

from .agent import models
from .agent.events import _message_text
from .agent.models import _model_error_message
from .config import Profile

if TYPE_CHECKING:
    from .application import SayacodeApp


def _new_profile_name(model_id: str, existing: dict[str, Profile]) -> str:
    base = re.sub(r"[^a-z0-9_-]+", "-", model_id.lower()).strip("-")[:48] or "model"
    if base not in existing:
        return base
    index = 2
    while f"{base}-{index}" in existing:
        index += 1
    return f"{base}-{index}"


def _profile(app: SayacodeApp) -> Profile:
    if app.profile_override is not None:
        return app.profile_override
    if app.profile_name is not None:
        return app.config.profile(app.profile_name)
    return app.config.profile()


async def _settings_command(app: SayacodeApp, args: Any) -> dict[str, Any]:
    tokens = shlex.split(str(args or ""))
    if not tokens or tokens[0] == "show":
        return {
            "preferences": dict(app.config.preferences),
            "profile": app.profile_name,
            "trust_level": app.trust_level,
            "default_trust": app.config.default_trust,
            "output_limit_bytes": app._output_limit_bytes(),
            "shutdown_grace_seconds": app._shutdown_grace_seconds(),
        }
    if len(tokens) != 3 or tokens[0] != "set":
        raise ValueError(
            "Usage: /settings [show|set output_limit_bytes <bytes>|"
            "set shutdown_grace_seconds <seconds>]"
        )
    key, raw = tokens[1:]
    value: int | float
    if key == "output_limit_bytes":
        value = int(raw)
    elif key == "shutdown_grace_seconds":
        value = float(raw)
    else:
        raise ValueError(f"Unknown setting: {key}")
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    app.config.preferences[key] = str(value)
    await app._save_config()
    app._handles.clear()
    return {key: value}


async def _config_command(app: SayacodeApp, args: Any) -> Any:
    if isinstance(args, dict):
        if args.get("action") == "set_key":
            if set(args) != {"action", "name", "api_key"}:
                raise ValueError("Model key update requires action, name, and api_key")
            name = args.get("name")
            api_key = args.get("api_key")
            if not isinstance(name, str) or not name:
                raise ValueError("Model key update requires a profile name")
            if api_key is not None and not isinstance(api_key, str):
                raise ValueError("API key must be text or null")
            profile = app.config.profile(name)
            app.config.profiles[name] = replace(profile, api_key=api_key)
            app._handles.clear()
            await app._save_config()
            return {"updated": name}
        if args.get("action") != "add" or not isinstance(args.get("profile"), dict):
            raise ValueError("Model command object must contain action=add and a profile")
        raw = args["profile"]
        expected = {
            "protocol",
            "base_url",
            "api_key",
            "model_id",
            "context_length",
            "max_output_tokens",
        }
        if set(raw) != expected:
            raise ValueError(f"Model profile requires exactly: {', '.join(sorted(expected))}")
        model_id = raw["model_id"]
        if not isinstance(model_id, str):
            raise ValueError("model_id must be a string")
        name = _new_profile_name(model_id, app.config.profiles)
        profile = Profile(name=name, **raw)
        app.config.profiles[name] = profile
        app.config.default_profile = app.config.default_profile or name
        app.profile_name = app.config.default_profile
        app.profile_override = None
        app._handles.clear()
        await app._save_config()
        return {"added": name, "default_profile": app.config.default_profile}
    tokens = shlex.split(str(args or ""))
    action = tokens[0].lower() if tokens else "list"
    if action in {"list", "profiles"}:
        return {
            "default_profile": app.config.default_profile,
            "profiles": {
                name: asdict(profile) | {"api_key": "***" if profile.api_key else None}
                for name, profile in app.config.profiles.items()
            },
        }
    if action in {"show", "current"}:
        profile = app._profile()
        item = asdict(profile)
        if item.get("api_key"):
            item["api_key"] = "***"
        return item
    if action in {"use", "switch"}:
        if len(tokens) != 2:
            raise ValueError("Usage: /config use <profile>")
        app.config.profile(tokens[1])
        app.config.default_profile = tokens[1]
        app.profile_name = tokens[1]
        app.profile_override = None
        app._handles.clear()
        await app._save_config()
        return {"default_profile": tokens[1]}
    if action in {"remove", "delete"}:
        if len(tokens) != 2:
            raise ValueError("Usage: /config remove <profile>")
        name = tokens[1]
        if name not in app.config.profiles:
            raise KeyError(name)
        del app.config.profiles[name]
        if app.config.default_profile == name:
            app.config.default_profile = next(iter(app.config.profiles), None)
        app.profile_name = app.config.default_profile
        app._handles.clear()
        await app._save_config()
        return {"removed": name, "default_profile": app.config.default_profile}
    if action == "add":
        raise ValueError("Use interactive /model add to enter the six protocol fields")
    if action == "test":
        profile = app.config.profile(tokens[1]) if len(tokens) > 1 else app._profile()
        model = models.model_for(profile, app.model_override if len(tokens) == 1 else None)
        report: dict[str, Any] = {
            "profile": profile.name,
            "protocol": profile.protocol,
            "text": False,
            "tool_calling": False,
            "stream": False,
            "errors": {},
        }
        try:
            response = await asyncio.wait_for(model.ainvoke("Reply with exactly: OK"), timeout=60)
            report["text"] = bool(_message_text(response).strip())
            report["response"] = _message_text(response)[:200]
        except Exception as exc:
            report["errors"]["text"] = _model_error_message(exc, profile)
            report["ok"] = False
            return report

        @tool
        def sayacode_capability_probe(value: str) -> str:
            """回传给定值，用于验证模型工具调用能力。"""
            return value

        try:
            bound = model.bind_tools([sayacode_capability_probe])
            tool_response = await asyncio.wait_for(
                bound.ainvoke("Call sayacode_capability_probe with value 'ping'."),
                timeout=60,
            )
            calls = getattr(tool_response, "tool_calls", [])
            report["tool_calling"] = any(
                call.get("name") == "sayacode_capability_probe" for call in calls
            )
        except Exception as exc:
            report["errors"]["tool_calling"] = _model_error_message(exc, profile)
        try:
            chunks = []
            async for chunk in model.astream("Reply with exactly: OK"):
                chunks.append(_message_text(chunk))
            report["stream"] = bool("".join(chunks).strip())
        except Exception as exc:
            report["errors"]["stream"] = _model_error_message(exc, profile)
        report["ok"] = all(report[key] for key in ("text", "tool_calling", "stream"))
        return report
    raise ValueError("Usage: /config [list|show|add|use|remove|test]")


async def _save_config(app: SayacodeApp) -> None:
    await app.repository.save(app.config)
