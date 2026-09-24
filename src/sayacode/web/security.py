"""本机浏览器入口的最小认证和脱敏规则。"""

from __future__ import annotations

import hmac
import ipaddress
import secrets
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from ..memory.privacy import redact_secrets

_SENSITIVE_KEYS = (
    "api_key",
    "secret",
    "password",
    "credential",
    "authorization",
    "access_token",
    "refresh_token",
    "id_token",
    "private_key",
)
_PRIVATE_KEYS = {"reasoning", "reasoning_content", "thinking", "chain_of_thought", "hidden", "private", "raw_response"}
_COOKIE_NAME = "sayacode_session"
_CSRF_HEADER = "x-csrf-token"


def redact_api(value: Any, key: str = "") -> Any:
    """响应递归脱敏，尤其阻止任务档案中的模型凭据外泄。"""
    normalized = key.lower().replace("-", "_")
    if normalized in _PRIVATE_KEYS:
        return None
    if normalized.startswith("has_") and isinstance(value, bool):
        return value
    if any(item in normalized for item in _SENSITIVE_KEYS) or normalized == "token":
        return "[已移除凭据]"
    if isinstance(value, dict):
        return {
            str(k): redact_api(v, str(k))
            for k, v in value.items()
            if str(k).lower().replace("-", "_") not in _PRIVATE_KEYS
        }
    if isinstance(value, (tuple, list)):
        return [redact_api(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_secrets(str(value))


def new_browser_credentials() -> tuple[str, str, str]:
    """启动令牌、会话 Cookie 和 CSRF 值只存在于当前进程。"""
    return secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(32)


def _host_header(request: Request) -> str:
    host = request.headers.get("host", "")
    try:
        parsed = urlsplit(f"http://{host}")
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as error:
        raise HTTPException(status_code=403, detail="Invalid local host") from error
    if hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username:
        raise HTTPException(status_code=403, detail="Local host required")
    return str(host)


def check_local_request(request: Request) -> None:
    """拒绝远端连接、DNS 重绑定主机名和跨来源浏览器请求。"""
    peer = request.client.host if request.client is not None else ""
    try:
        if not ipaddress.ip_address(peer).is_loopback:
            raise ValueError("not loopback")
    except ValueError as error:
        raise HTTPException(status_code=403, detail="Loopback connection required") from error
    host = _host_header(request)
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") != f"http://{host}":
        raise HTTPException(status_code=403, detail="Cross-origin request refused")


def require_session(request: Request) -> None:
    supplied = request.cookies.get(_COOKIE_NAME, "")
    expected = request.app.state.browser_session
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Browser session required")


def require_mutation(request: Request) -> None:
    require_session(request)
    supplied = request.headers.get(_CSRF_HEADER, "")
    expected = request.app.state.csrf_token
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="CSRF token required")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="JSON request required")


def verify_launch_token(request: Request, token: str) -> None:
    expected = request.app.state.launch_token
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid launch token")


__all__ = [
    "_COOKIE_NAME",
    "check_local_request",
    "new_browser_credentials",
    "redact_api",
    "require_mutation",
    "require_session",
    "verify_launch_token",
]
