"""URL 校验：把损坏或非 HTTP(S) 的值挡在配置之外。

负责清洗 base URL 并对非法值 fail-closed 返回 None。
核心函数：sanitize_base_url。
调用链：配置加载→sanitize_base_url→provider。"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse


def sanitize_base_url(value: Optional[Any]) -> Optional[str]:
    """返回合法的 HTTP(S) base URL；损坏、空缺或非 HTTP(S) 的值返回 None。"""
    if value is None:
        return None

    # 去 BOM 并禁换行制表符，防配置注入拆行。
    cleaned = str(value).replace("\ufeff", "").strip()
    if not cleaned or any(char in cleaned for char in ("\r", "\n", "\t")):
        return None

    parsed = urlparse(cleaned)
    # 仅放行 http/https 且带 host 的值，其余 fail-closed 返回 None。
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    return cleaned
