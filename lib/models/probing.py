"""上下文窗口探测，按协议声明探测策略。

窗口信息不在模型接口上，各家查询方式也不同。
本模块把问法按协议收敛成四个函数，由一张表按协议名分派，
协议类只需声明协议名。
探测永远不抛异常，失败返回空表示未知，调用方据此要求用户显式输入，
而不是编造默认窗口，编错会让压缩策略在错误前提下工作。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from ..i18n import tr
from .vocabulary import parse_context_window


_PROBE_TIMEOUT = 15

# 开放兼容端点在不同字段名中暴露上下文长度，按优先级尝试。
# 覆盖多种推理引擎与嵌套规格场景。
_OPENAI_COMPATIBLE_FIELDS: list[str] = [
    "max_model_len",           # vLLM / 多数开源推理引擎
    "max_context_length",
    "max_sequence_length",     # 覆盖 TGI 推理服务字段。
    "max_total_tokens",
    "context_length",
    "context_window",
    "max_position_embeddings",
    "n_positions",
    "model_max_length",
    "max_length",              # 模型规格嵌套场景
]


def _report_failure(error: Exception) -> None:
    print(tr("model.context_probe_failed", error=str(error)))


def _search_nested(
    data: object,
    fields: list[str],
    depth: int = 0,
) -> Optional[int]:
    # 首参用 object，输入是各家接口返回的未知 JSON 结构，函数内已做类型分支
    """递归搜索嵌套结构中的上下文窗口字段（最多 3 层）。"""
    if depth > 3:
        return None
    if isinstance(data, dict):
        for field in fields:
            parsed = parse_context_window(data.get(field))
            if parsed:
                return parsed
        for value in data.values():
            found = _search_nested(value, fields, depth + 1)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _search_nested(item, fields, depth + 1)
            if found:
                return found
    return None


def _find_model_entry(payload: object, model_name: str) -> Optional[dict[str, Any]]:
    # 首参用 object，输入是模型列表接口的未知承载形态，函数内已做类型分支
    """在模型列表响应里按标识找到目标条目。

    列表承载形态各家不一，可能是字典套列表，也可能直接是列表。
    找不到返回空表示不知道，不编造。
    """
    if isinstance(payload, dict):
        for key in ("data", "models", "items"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                entries: list[Any] = candidate
                break
        else:
            entries = []
    elif isinstance(payload, list):
        entries = payload
    else:
        return None

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for key in ("id", "model", "name"):
            if str(entry.get(key) or "") == model_name:
                return entry
    return None


def probe_openai_compatible(
    base_url: str,
    model_name: str,
    api_key: Optional[str],
) -> Optional[int]:
    """开放兼容端点的上下文窗口探测。

    两条路由按顺序尝试，先按模型直查，条目自带窗口字段。
    第一条拿不到结果才走列表查询。
    第二条不可少，部分网关没有按模型路由，只在列表里给窗口长度。
    只试第一条会让这类网关的自动探测永远失效，而探测失败按设计只会提示手动输入。
    """
    try:
        import httpx
        from urllib.parse import quote

        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # 模型名可能含斜杠，必须按单一路径段编码，否则会被当成多级路径。
        response = httpx.get(
            f"{base_url}/models/{quote(str(model_name), safe='')}",
            headers=headers,
            timeout=_PROBE_TIMEOUT,
        )
        if response.status_code == 200:
            detected = _search_nested(response.json(), _OPENAI_COMPATIBLE_FIELDS)
            if detected:
                return detected

        listing = httpx.get(f"{base_url}/models", headers=headers, timeout=_PROBE_TIMEOUT)
        if listing.status_code != 200:
            return None

        entry = _find_model_entry(listing.json(), model_name)
        if entry is None:
            return None
        return _search_nested(entry, _OPENAI_COMPATIBLE_FIELDS)
    except Exception as error:
        _report_failure(error)
        return None


def probe_anthropic(
    base_url: str,
    model_name: str,
    api_key: Optional[str],
) -> Optional[int]:
    """对话模型接口，按模型直查取最大输入长度。"""
    try:
        import httpx
        from urllib.parse import quote

        headers = {
            "x-api-key": api_key or "",
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
        }

        response = httpx.get(
            f"{base_url}/models/{quote(str(model_name), safe='')}",
            headers=headers,
            timeout=_PROBE_TIMEOUT,
        )
        if response.status_code != 200:
            return None

        data = response.json()
        detected = parse_context_window(data.get("max_input_tokens"))
        if detected:
            return detected

        # 部分版本把模型信息放在嵌套结构中
        nested = data.get("model", data)
        if isinstance(nested, dict):
            return parse_context_window(nested.get("max_input_tokens"))
        return None
    except Exception as error:
        _report_failure(error)
        return None


def probe_gemini(
    base_url: str,
    model_name: str,
    api_key: Optional[str],
) -> Optional[int]:
    """生成模型接口，按模型直查取输入长度上限。"""
    try:
        import httpx
        from urllib.parse import quote

        if not api_key:
            return None

        response = httpx.get(
            f"{base_url}/models/{quote(str(model_name), safe='')}",
            headers={"x-goog-api-key": api_key, "Accept": "application/json"},
            timeout=_PROBE_TIMEOUT,
        )
        if response.status_code != 200:
            return None

        return parse_context_window(response.json().get("inputTokenLimit"))
    except Exception as error:
        _report_failure(error)
        return None


def probe_ollama(
    base_url: str,
    model_name: str,
    api_key: Optional[str] = None,
) -> Optional[int]:
    """本地模型接口，查询展示接口。

    有效上下文取两者较小值，原生上下文长度与运行时配置。
    """
    try:
        import httpx

        response = httpx.post(
            f"{base_url}/api/show",
            json={"name": model_name},
            timeout=_PROBE_TIMEOUT,
        )
        if response.status_code != 200:
            return None

        data = response.json()

        native_context: Optional[int] = None
        for key, value in (data.get("model_info") or {}).items():
            if "context_length" not in key.lower() and "context_len" not in key.lower():
                continue
            native_context = parse_context_window(value)
            if native_context:
                break

        runtime_context: Optional[int] = None
        for line in str(data.get("modelfile") or "").split("\n"):
            stripped = line.strip()
            if not stripped.lower().startswith("num_ctx"):
                continue
            parts = stripped.split()
            if len(parts) >= 2:
                runtime_context = parse_context_window(parts[1])
            break

        if native_context is not None and runtime_context is not None:
            return min(native_context, runtime_context)
        return native_context if native_context is not None else runtime_context
    except Exception as error:
        _report_failure(error)
        return None


# 协议名到探测函数，未列出的协议表示无法探测，返回 None 由调用方处理
PROBES: dict[str, Callable[[str, str, Optional[str]], Optional[int]]] = {
    "openai": probe_openai_compatible,
    "azure_openai": probe_openai_compatible,
    "deepseek": probe_openai_compatible,
    "anthropic": probe_anthropic,
    "gemini": probe_gemini,
    "ollama": probe_ollama,
}


def probe_context_window(
    protocol: str,
    base_url: str,
    model_name: str,
    api_key: Optional[str] = None,
) -> Optional[int]:
    """按协议分派上下文窗口探测，未知协议返回空。"""
    probe = PROBES.get(protocol)
    if probe is None:
        return None
    return probe(base_url, model_name, api_key)


__all__ = [
    "PROBES",
    "probe_anthropic",
    "probe_context_window",
    "probe_gemini",
    "probe_ollama",
    "probe_openai_compatible",
]
