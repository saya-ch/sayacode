"""Subprocess environment helpers shared by hooks, MCP, shell, and Git tools."""

from __future__ import annotations

import os
from typing import Dict
from urllib.parse import urlsplit, urlunsplit


SENSITIVE_ENV_MARKERS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASS",
    "CREDENTIAL",
    "AUTH",
    "COOKIE",
)


def _is_sensitive_env_key(key: str) -> bool:
    normalized = str(key or "").upper()
    return any(marker in normalized for marker in SENSITIVE_ENV_MARKERS)


def _strip_url_credentials(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if not (parsed.username or parsed.password):
            return value
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit(parsed._replace(netloc=host))
    except Exception:
        return value


def build_process_env() -> Dict[str, str]:
    """Return a subprocess environment with common secret variables removed."""
    env = {
        key: _strip_url_credentials(value)
        for key, value in os.environ.items()
        if not _is_sensitive_env_key(key)
    }
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "PIP_NO_INPUT": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
        "POWERSHELL_TELEMETRY_OPTOUT": "1",
    })
    return env


__all__ = ["build_process_env"]
