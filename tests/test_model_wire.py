"""模型层的真实 HTTP 集成测试：本地假厂商 + 真实 LangChain 客户端。

与 `test_model_contract.py` 的分工：

* 契约冻结测试断言**对外接口与默认值**；
* 本文件断言**线上到底发了/收了什么** —— 请求体形状、SSE 解析、工具调用解析、
  用量提取、以及厂商特有字段的双向透传。

这些是 mock 测试与契约测试都覆盖不到的层面，三个真实缺陷正是靠它才暴露出来：

1. ``create_from_config`` 把 `azure_*: None` 之类的 harness 配置键透传进请求体，
   真实调用直接 `TypeError`。
2. ``chat_stream`` 把没有 content 的 chunk 对象 repr 当文本产出。
3. 流式用量取自最后一个块（那是空收尾块），静默退化成字符数估算。

只依赖回环地址，不需要任何外部服务或密钥。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from lib.models import TokenUsage
from lib.models.registry import get_model_provider_registry


def _sse(obj: Any) -> bytes:
    return f"data: {json.dumps(obj)}\n\n".encode()


# 本 fixture 使用的密钥。假厂商会**校验**它 —— 见下方 ``_authorized``。
_API_KEY = "dummy"
_EXPECTED_AUTHORIZATION = f"Bearer {_API_KEY}"

# 只提供列表端点（per-model 路由 404）的网关用的路径前缀。
LIST_ONLY_PREFIX = "/listonly"

# per-model 返回 200 但**不含**窗口字段的网关用的路径前缀。
NO_WINDOW_FIELD_PREFIX = "/nofield"

# 假厂商收到的 GET 路径，按顺序记录。
# 用模块级列表而不是 server 属性，是为了不改变 fixture 的返回值形状
# （多个测试按 ``base_url, received = fake_provider`` 解包）。
_GET_PATHS: list[str] = []


class _FakeProviderHandler(BaseHTTPRequestHandler):
    """最小 OpenAI 兼容端点：补全 / SSE 流式 / 模型信息。

    **它会校验 ``Authorization``，这是刻意的。** 早期版本对任何请求都回 200，
    于是「凭据有没有正确传下去」这条链路完全没被覆盖 —— 一个让上下文窗口探测
    在所有 provider 上失效的回归（密钥字段是 pydantic ``SecretStr``，
    ``str(SecretStr(...))`` 得到的是 ``'**********'`` 掩码而不是密钥）就是这样
    溜过去的：真实端点回 401，而探测函数对非 200 一律返回 None。

    教训写在 ``CODE_REVIEW_FINDINGS.md``：假服务器至少要校验真服务器校验的输入，
    否则它只是一个绕了远路的 mock。
    """

    def log_message(self, *args: Any) -> None:  # 静音测试输出
        pass

    def _json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == _EXPECTED_AUTHORIZATION:
            return True
        self._json(
            {"error": {"message": "Invalid API key", "type": "authentication_error"}},
            status=401,
        )
        return False

    def do_GET(self) -> None:
        if not self._authorized():
            return

        _GET_PATHS.append(self.path)

        # ``/listonly/...`` 模拟「只有列表端点、没有 per-model 路由」的网关。
        # 实测 Command Code 就是这种形状：``GET /models/{id}`` 返回 404，
        # 但 ``GET /models`` 的每个条目都带 ``context_length``。
        if self.path.startswith(f"{LIST_ONLY_PREFIX}/v1/models/"):
            self._json({"success": False, "status": 404, "message": "404 Not found."}, 404)
            return
        if self.path.startswith(f"{NO_WINDOW_FIELD_PREFIX}/v1/models/"):
            # 200，但条目里没有任何窗口字段
            self._json({"id": "fake-model", "object": "model", "owned_by": "fake"})
            return
        if self.path.startswith("/v1/models/"):
            self._json({"id": "fake-model", "max_model_len": 128000})
            return
        if self.path in (
            "/v1/models",
            f"{LIST_ONLY_PREFIX}/v1/models",
            f"{NO_WINDOW_FIELD_PREFIX}/v1/models",
        ):
            self._json({
                "object": "list",
                "data": [{"id": "fake-model", "context_length": 128000}],
            })
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self._authorized():
            return

        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.server.received.append(payload)  # type: ignore[attr-defined]

        if not self.path.endswith("/chat/completions"):
            self._json({"error": "not found"}, 404)
            return

        last_user = ""
        for message in payload.get("messages", []):
            if message.get("role") == "user":
                last_user = str(message.get("content") or "")

        if payload.get("stream"):
            self._stream_response()
            return

        message: dict[str, Any] = {
            "role": "assistant",
            "content": "PONG",
            "reasoning_content": "I thought about it.",
            # 一个**不在**内置已知集合里的厂商字段，用于验证
            # ``CompatSwitches.extra_passthrough_fields`` 这条声明式扩展路径。
            "x_vendor_note": "vendor-specific",
        }
        finish = "stop"
        if payload.get("tools") and "weather" in last_user.lower():
            message = {
                "role": "assistant",
                "content": None,
                "reasoning_content": "Need the tool.",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
                }],
            }
            finish = "tool_calls"

        self._json({
            "id": "1",
            "object": "chat.completion",
            "created": 0,
            "model": payload.get("model", "fake-model"),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
        })

    def _stream_response(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        for piece in ("Hel", "lo", "!"):
            self.wfile.write(_sse({
                "id": "1", "object": "chat.completion.chunk", "created": 0,
                "model": "fake-model",
                "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
            }))
            self.wfile.flush()

        # 用量块之后还会有一个空的收尾块 —— 真实链路就是这个形状，
        # 「只看最后一个块」会因此丢掉用量。
        self.wfile.write(_sse({
            "id": "1", "object": "chat.completion.chunk", "created": 0,
            "model": "fake-model",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        }))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@pytest.fixture(scope="module")
def fake_provider() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeProviderHandler)
    server.received = []  # type: ignore[attr-defined]
    _GET_PATHS.clear()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1", server.received  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def make_model(fake_provider):
    base_url, _ = fake_provider
    registry = get_model_provider_registry()

    def _make(**overrides):
        kwargs = {"model_name": "fake-model", "base_url": base_url, "api_key": _API_KEY}
        kwargs.update(overrides)
        return registry.create_model("generic", **kwargs)

    return _make


# ── 补全 ─────────────────────────────────────────────────────────────────────


def test_chat_returns_text_and_records_usage(make_model):
    model = make_model()

    assert model.chat([{"role": "user", "content": "say pong"}]) == "PONG"
    assert model.last_usage == TokenUsage(9, 4, 13)


def test_raw_response_content_can_carry_nonstandard_fields(make_model):
    """``invoke`` 返回的消息对象上应能读到厂商特有字段。"""
    message = make_model().invoke([HumanMessage(content="hi")])

    assert (message.additional_kwargs or {}).get("reasoning_content") == "I thought about it."


# ── 非标准字段回填 ───────────────────────────────────────────────────────────


def test_reasoning_content_is_written_back_into_the_request(make_model, fake_provider):
    """推理内容必须原样回到下一次请求体里，否则多轮工具调用会静默退化。"""
    _, received = fake_provider
    model = make_model()

    model.invoke([
        AIMessage(content="prev", additional_kwargs={"reasoning_content": "secret-thought"}),
        HumanMessage(content="next"),
    ])

    assistant_messages = [
        message for message in received[-1]["messages"] if message.get("role") == "assistant"
    ]
    assert assistant_messages[-1].get("reasoning_content") == "secret-thought"


# ── 流式 ─────────────────────────────────────────────────────────────────────


def test_chat_stream_yields_only_text(make_model):
    model = make_model()

    chunks = list(model.chat_stream([{"role": "user", "content": "say hello"}]))

    assert chunks == ["Hel", "lo", "!"]


def test_chat_stream_reads_usage_from_the_usage_chunk(make_model):
    """用量块不是最后一个块，不能被空收尾块挤掉。"""
    model = make_model()

    list(model.chat_stream([{"role": "user", "content": "say hello"}]))

    assert model.session_usage == TokenUsage(7, 3, 10)


# ── 工具调用 ─────────────────────────────────────────────────────────────────


@tool
def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"sunny in {city}"


def test_bind_tools_parses_tool_calls(make_model, fake_provider):
    _, received = fake_provider

    result = make_model().bind_tools([get_weather]).invoke(
        [HumanMessage(content="weather in Paris?")]
    )

    assert result.tool_calls[0]["name"] == "get_weather"
    assert result.tool_calls[0]["args"] == {"city": "Paris"}
    assert received[-1].get("tools")


# ── 上下文窗口探测 ───────────────────────────────────────────────────────────


def test_detect_context_window_hits_the_models_endpoint(make_model):
    model = make_model()

    assert model.detect_context_window() == 128000
    assert model.context_window_source == "api"


def test_probe_carries_the_plaintext_api_key(make_model):
    """探测请求必须带**明文**密钥，而不是 ``SecretStr`` 的掩码。

    回归保护：厂商集成把密钥字段声明成 pydantic ``SecretStr``，而
    ``str(SecretStr("k")) == '**********'``。重写时直接 ``str()`` 了它，于是探测
    请求带的是掩码 —— 真实端点回 **401**（实测：明文 200 / 掩码 401），
    而探测函数对所有非 200 一律返回 ``None``，表现为「上下文窗口永远探测不到」
    且完全无声；非交互模式下还会因此直接 ``RuntimeError``。

    这里同时断言私有取值与端到端结果：前者让失败信息直指病灶，后者保证
    假厂商真的在校验（见 ``test_probe_fails_with_a_rejected_key``）。
    """
    model = make_model()

    assert model._resolved_api_key() == _API_KEY
    assert model.detect_context_window() == 128000


def test_probe_fails_with_a_rejected_key(fake_provider):
    """证明假厂商**真的**在校验凭据 —— 否则上面那条探测断言就是空的。"""
    base_url, _ = fake_provider
    # 占位值刻意保持短：发布检查会扫描 `api_key="..."` 这类字面量，
    # 达到 8 个字符即判为疑似密钥（说明门禁在正常工作）。
    model = get_model_provider_registry().create_model(
        "generic", model_name="fake-model", base_url=base_url, api_key="nope"
    )

    assert model.detect_context_window() is None
    assert model.context_window_source == ""


def test_probe_prefers_the_per_model_route(make_model):
    """per-model 路由可用时不得走列表端点 —— 顺序是契约的一部分。"""
    model = make_model()
    _GET_PATHS.clear()

    assert model.detect_context_window() == 128000

    assert "/v1/models/fake-model" in _GET_PATHS
    assert "/v1/models" not in _GET_PATHS


def test_probe_falls_back_to_the_models_list(fake_provider):
    """per-model 路由 404 时，退化到列表端点并按 id 找到该模型。

    回归保护：实测某些网关（Command Code）**没有** per-model 路由，只在该列表里
    给出 ``context_length``。只试 per-model 的话这类端点永远探测不到窗口，
    而「探测失败 = 未知」是设计行为，用户只会看到「请手动输入」而不知原因。
    """
    base_url, _ = fake_provider
    list_only_base = base_url.replace("/v1", f"{LIST_ONLY_PREFIX}/v1", 1)
    model = get_model_provider_registry().create_model(
        "generic", model_name="fake-model", base_url=list_only_base, api_key=_API_KEY
    )
    _GET_PATHS.clear()

    assert model.detect_context_window() == 128000
    assert model.context_window_source == "api"

    assert f"{LIST_ONLY_PREFIX}/v1/models/fake-model" in _GET_PATHS
    assert f"{LIST_ONLY_PREFIX}/v1/models" in _GET_PATHS


def test_probe_falls_back_when_per_model_has_no_window_field(fake_provider):
    """per-model 返回 200、但条目里没有窗口字段时，也要继续试列表端点。"""
    base_url, _ = fake_provider
    nofield_base = base_url.replace("/v1", f"{NO_WINDOW_FIELD_PREFIX}/v1", 1)
    model = get_model_provider_registry().create_model(
        "generic", model_name="fake-model", base_url=nofield_base, api_key=_API_KEY
    )
    _GET_PATHS.clear()

    assert model.detect_context_window() == 128000
    assert model.context_window_source == "api"

    assert f"{NO_WINDOW_FIELD_PREFIX}/v1/models/fake-model" in _GET_PATHS
    assert f"{NO_WINDOW_FIELD_PREFIX}/v1/models" in _GET_PATHS


# ── 声明式扩展：extra_passthrough_fields ─────────────────────────────────────


def test_extra_passthrough_field_round_trips_over_the_wire(make_model, fake_provider):
    """目录里加一个字段名即可透传新厂商字段 —— 无需改代码，这是声明式的验收点。"""
    from lib.models.provider_catalog import CompatSwitches

    _, received = fake_provider
    model = make_model()
    model.compat = CompatSwitches(
        passthrough_nonstandard=True,
        extra_passthrough_fields=("x_vendor_note",),
    )

    message = model.invoke([HumanMessage(content="hi")])
    assert (message.additional_kwargs or {}).get("x_vendor_note") == "vendor-specific"

    model.invoke([
        AIMessage(content="prev", additional_kwargs={"x_vendor_note": "echoed"}),
        HumanMessage(content="next"),
    ])

    assistant_messages = [
        m for m in received[-1]["messages"] if m.get("role") == "assistant"
    ]
    assert assistant_messages[-1].get("x_vendor_note") == "echoed"


def test_undeclared_vendor_field_is_not_carried(make_model):
    """未声明的字段不得出现在 ``additional_kwargs`` 里（默认集合不含它）。"""
    model = make_model()

    message = model.invoke([HumanMessage(content="hi")])

    assert "x_vendor_note" not in (message.additional_kwargs or {})
    assert (message.additional_kwargs or {}).get("reasoning_content") == "I thought about it."


# ── 回归：harness 配置键不得进入请求体 ───────────────────────────────────────


def test_create_from_config_does_not_leak_harness_keys_into_request(fake_provider):
    """保存的 profile 里常见的 ``azure_*: None`` / ``metadata`` 不得进入请求体。

    真实链路实测：它们会被上游收进 ``model_kwargs`` 并原样发给厂商，导致
    ``TypeError: Completions.create() got an unexpected keyword argument``。
    """
    base_url, received = fake_provider

    model = get_model_provider_registry().create_from_config({
        "api_type": "openai",
        "base_url": base_url,
        "api_key": _API_KEY,
        "model_name": "fake-model",
        "timeout": 30,
        "max_retries": 1,
        "temperature": 0.2,
        "max_tokens": None,
        "context_window": 1048576,
        "metadata": {},
        "azure_api_version": None,
        "azure_deployment": None,
    })

    assert getattr(model, "model_kwargs", None) == {}
    assert model.context_window == 1048576
    assert model.chat([{"role": "user", "content": "ping"}]) == "PONG"
    assert not any(key.startswith("azure") for key in received[-1])
    assert "metadata" not in received[-1]


def test_context_window_is_consumed_even_on_direct_construction(fake_provider):
    """直接构造协议类时 ``context_window=`` 既不能丢，也不能漏进请求体。

    回归保护：``context_window`` 是 ``ModelExtras`` 的属性、不是 LangChain 字段，
    早期实现把它原样传进构造函数，结果 LangChain 的 ``_build_model_kwargs``
    判定它「不是默认参数」收进 ``model_kwargs``，**原样发进请求体** ——
    实测请求体里真的多出 ``{"context_window": 12345}``，厂商会以未知参数拒绝。
    同一路径下属性值还被静默丢弃（读回来是 0）。
    """
    base_url, received = fake_provider
    model = get_model_provider_registry().create_model(
        "generic", model_name="fake-model", base_url=base_url,
        api_key=_API_KEY, context_window=12345,
    )

    assert model.context_window == 12345
    assert model.context_window_source == "manual"
    assert getattr(model, "model_kwargs", None) == {}

    assert model.chat([{"role": "user", "content": "ping"}]) == "PONG"
    assert "context_window" not in received[-1]
