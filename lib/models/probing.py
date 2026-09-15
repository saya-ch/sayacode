"""上下文窗口探测 —— 按协议声明的探测策略。

上下文窗口是 LangChain **不提供**的信息：它既不在模型接口上，各家的查询方式也
各不相同。本模块把「怎么问」按协议收敛成四个函数，由一个 ``PROBES`` 表按协议名
分派，因此 :mod:`lib.models.providers` 里的协议类只需声明 ``_PROTOCOL``。

探测永远**不抛异常**：失败就返回 ``None``（表示未知）。调用方据此要求用户显式输入，
而不是编造一个默认窗口 —— 编错窗口会让压缩策略在错误的前提下工作。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ..i18n import tr
from .vocabulary import parse_context_window


_PROBE_TIMEOUT = 15

# OpenAI 兼容端点在不同字段名中暴露上下文长度，按优先级尝试。
# 覆盖 vLLM / TGI / 通用命名 / 嵌套模型规格等场景。
_OPENAI_COMPATIBLE_FIELDS: List[str] = [
    "max_model_len",           # vLLM / 多数开源推理引擎
    "max_context_length",
    "max_sequence_length",     # TGI
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
    data: Any,
    fields: List[str],
    depth: int = 0,
) -> Optional[int]:
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


def _find_model_entry(payload: Any, model_name: str) -> Optional[dict]:
    """在 ``GET /models`` 的列表响应里按 id/model/name 找到目标条目。

    列表的承载形态各家不一：可能是 ``{"data": [...]}``、``{"models": [...]}``，
    也可能直接是列表。找不到就返回 None（表示「不知道」，不编造）。
    """
    if isinstance(payload, dict):
        for key in ("data", "models", "items"):
            candidate = payload.get(key)
            if isinstance(candidate, list):
                entries: List[Any] = candidate
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
    """OpenAI 兼容端点的上下文窗口探测。

    两条路由，按顺序尝试：

    1. ``GET {base_url}/models/{model}`` —— 标准写法，条目本身就带窗口字段；
    2. ``GET {base_url}/models`` —— **只在第 1 条拿不到结果时才走**。

    第 2 条是真实链路逼出来的：部分网关（实测 Command Code）根本没有 per-model
    路由（返回 404），只在**列表**里给出 ``context_length``。只试第 1 条的话，
    这类网关的自动探测永远失效，而且因为「探测失败 = 未知」是设计行为，用户
    只会看到「请手动输入」而不知道原因。
    """
    try:
        import requests
        from urllib.parse import quote

        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # 模型名可能含 ``/``（如 ``deepseek/deepseek-v4.1-flash``），必须按单一路径段编码，
        # 否则会被当成多级路径。
        response = requests.get(
            f"{base_url}/models/{quote(str(model_name), safe='')}",
            headers=headers,
            timeout=_PROBE_TIMEOUT,
        )
        if response.status_code == 200:
            detected = _search_nested(response.json(), _OPENAI_COMPATIBLE_FIELDS)
            if detected:
                return detected

        listing = requests.get(f"{base_url}/models", headers=headers, timeout=_PROBE_TIMEOUT)
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
    """Anthropic Models API：``GET {base_url}/models/{model}`` → ``max_input_tokens``。"""
    try:
        import requests

        headers = {
            "x-api-key": api_key or "",
            "anthropic-version": "2023-06-01",
            "Accept": "application/json",
        }

        response = requests.get(
            f"{base_url}/models/{model_name}",
            headers=headers,
            timeout=_PROBE_TIMEOUT,
        )
        if response.status_code != 200:
            return None

        data = response.json()
        detected = parse_context_window(data.get("max_input_tokens"))
        if detected:
            return detected

        # 部分旧版本把模型信息放在嵌套结构中
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
    """Gemini Models API：``GET {base_url}/models/{model}`` → ``inputTokenLimit``。"""
    try:
        import requests

        if not api_key:
            return None

        response = requests.get(
            f"{base_url}/models/{model_name}",
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
    """Ollama Show API：``POST {base_url}/api/show``。

    有效上下文取两者较小值：

    1. ``model_info`` 里的原生上下文长度（Ollama 0.3+，键名形如 ``llama.context_length``）；
    2. ``modelfile`` 里的运行时 ``num_ctx``。
    """
    try:
        import requests

        response = requests.post(
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


# 协议名 → 探测函数。未列出的协议表示「无法探测」，返回 None 由调用方处理。
PROBES: Dict[str, Callable[[str, str, Optional[str]], Optional[int]]] = {
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
    """按协议分派上下文窗口探测；未知协议返回 None。"""
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
