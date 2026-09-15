"""模型 provider 行为测试。

**本文件在重写后被大幅精简，说明如下：**

删除了整个 ``TestGeminiModel``（原 18 项）与 ``test_deepseek_prefers_deepseek_key_over_openai_env``
——它们断言的是**已删除实现的内部细节**：

* ``_build_url`` / ``_build_headers`` / ``_build_payload`` / ``_extract_text`` /
  ``_convert_tools_to_gemini`` / ``_extract_function_calls`` 是手写 Gemini REST 客户端的
  私有方法；现在 Gemini 走官方 ``langchain-google-genai``，这些方法不存在了，
  协议正确性由上游集成与其自身测试负责。
* DeepSeek 的 ``base_url`` 嗅探已按「声明式」原则删除：DeepSeek 现在是目录里的
  一等 provider（``protocol=deepseek``），不再靠 URL 猜测。

``test_import_failure_prints_hint`` 也被删除：可选依赖的缺失现在在**导入期**保护，
相关断言已移到 ``tests/test_provider_optional_deps.py``（含 AST 门禁）。

对外的行为契约由 ``tests/test_model_contract.py`` 冻结覆盖。
"""

from __future__ import annotations

import pytest


# ── OllamaModel ──────────────────────────────────────────────────────────


class TestOllamaModel:
    def test_init_defaults(self, monkeypatch):
        """默认端点声明在**目录**里，经工厂构造时落到 base_url 上。

        直接类构造（`OllamaModel(model_name=...)`）不再自己填默认端点 —— 上游
        `ChatOllama` 的 `base_url` 字段默认为 `None`，由它内部解析。默认值的
        唯一来源是目录，这也正是 `test_architecture_boundaries` 要求的方向。
        """
        monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)

        from lib.models import OllamaModel
        from lib.models.provider_catalog import PROVIDER_CATALOG
        from lib.models.registry import get_model_provider_registry

        assert OllamaModel(model_name="llama3.2").model_name == "llama3.2"

        model = get_model_provider_registry().create_model("ollama", model_name="llama3.2")

        assert model.base_url.rstrip("/") == PROVIDER_CATALOG["ollama"].default_base_url.rstrip("/")

    def test_check_connection_returns_false_on_failure(self, monkeypatch):
        """注意用 monkeypatch 打**类**而不是实例：协议类是 pydantic 模型，
        直接 `m.chat = ...` 会被拒绝（object has no field "chat"）。"""
        from lib.models import OllamaModel

        def boom(self, *args, **kwargs):
            raise RuntimeError("offline")

        monkeypatch.setattr(OllamaModel, "chat", boom)

        assert OllamaModel(model_name="no-model").check_connection() is False

    def test_check_connection_detects_context_window(self, monkeypatch):
        from lib.models import OllamaModel

        monkeypatch.setattr(OllamaModel, "chat", lambda self, *a, **k: "ok")
        monkeypatch.setattr(OllamaModel, "detect_context_window", lambda self: 128000)

        assert OllamaModel(model_name="llama3.2").check_connection() is True

    def test_repr(self):
        from lib.models import OllamaModel

        assert "llama3.2" in repr(OllamaModel(model_name="llama3.2"))


# ── OpenAIModel ──────────────────────────────────────────────────────────


class TestOpenAIModel:
    # 官方集成要求在**构造期**提供凭据（旧手写实现是延迟到调用期校验）。
    _KEY = {"api_key": "dummy"}

    def test_init_defaults(self):
        from lib.models import OpenAIModel

        assert OpenAIModel(model_name="gpt-4o", **self._KEY).model_name == "gpt-4o"

    def test_get_model_info(self):
        from lib.models import OpenAIModel

        assert OpenAIModel(model_name="gpt-4o", **self._KEY).get_model_info().model_type == "openai"

    def test_repr(self):
        from lib.models import OpenAIModel

        assert "gpt-4o" in repr(OpenAIModel(model_name="gpt-4o", **self._KEY))

    def test_inject_nonstandard_fields_uses_matching_ai_message(self):
        """非标准字段必须回填到**对应顺序**的 assistant 消息上。"""
        from langchain_core.messages import AIMessage, HumanMessage

        from lib.models.compat import _KNOWN_NONSTANDARD_ATTRS, _inject_nonstandard_fields

        payload = {
            "messages": [
                {"role": "assistant", "content": "one"},
                {"role": "user", "content": "next"},
                {"role": "assistant", "content": "two"},
            ],
        }
        _inject_nonstandard_fields(
            [
                AIMessage(content="one", additional_kwargs={"reasoning_content": "r1"}),
                HumanMessage(content="next"),
                AIMessage(content="two", additional_kwargs={"reasoning_content": "r2"}),
            ],
            payload,
            _KNOWN_NONSTANDARD_ATTRS,
        )

        assert payload["messages"][0]["reasoning_content"] == "r1"
        assert payload["messages"][2]["reasoning_content"] == "r2"

    def test_inject_only_carries_declared_fields(self):
        """**声明之外的字段一律不搬** —— 字段集合由 compat 声明决定，不是「除标准外都要」。"""
        from langchain_core.messages import AIMessage

        from lib.models.compat import _KNOWN_NONSTANDARD_ATTRS, _inject_nonstandard_fields

        payload = {"messages": [{"role": "assistant", "content": "x"}]}
        _inject_nonstandard_fields(
            [AIMessage(content="x", additional_kwargs={
                "reasoning_content": "kept",
                "some_undeclared_vendor_field": "dropped",
            })],
            payload,
            _KNOWN_NONSTANDARD_ATTRS,
        )

        assert payload["messages"][0]["reasoning_content"] == "kept"
        assert "some_undeclared_vendor_field" not in payload["messages"][0]


# ── AnthropicModel ───────────────────────────────────────────────────────


class TestAnthropicModel:
    def test_init_defaults(self):
        from lib.models import AnthropicModel

        m = AnthropicModel(model_name="claude-sonnet-4-6", api_key="dummy")

        assert m.model_name == "claude-sonnet-4-6"

    def test_get_model_info(self):
        from lib.models import AnthropicModel

        m = AnthropicModel(model_name="claude-sonnet-4-6", api_key="dummy")

        assert m.get_model_info().model_type == "anthropic"


# ── DeepSeek（重写后升为一等 provider）────────────────────────────────────


class TestDeepSeekModel:
    @pytest.mark.skipif(
        __import__("lib.models", fromlist=["DeepSeekModel"]).DeepSeekModel is None,
        reason="langchain-deepseek 未安装",
    )
    def test_deepseek_is_a_first_class_provider(self):
        """DeepSeek 不再靠 base_url 嗅探，而是目录里的一等条目。"""
        from lib.models import DeepSeekModel
        from lib.models.provider_catalog import provider_catalog_entry

        entry = provider_catalog_entry("deepseek")

        assert entry.protocol == "deepseek"
        # 断言的是「目录里的 DeepSeek 选择了透传协议」这一配置意图；
        # 开关**是否真的生效**由 tests/test_model_compat.py 用行为断言，
        # 不靠「字面值等于字面值」的自证。
        assert entry.compat.passthrough_nonstandard is True

        m = DeepSeekModel(model_name="deepseek-chat", api_key="dummy")

        assert m.get_model_info().model_type == "deepseek"


# ── BaseModel utilities ──────────────────────────────────────────────────


class TestBaseModel:
    def test_token_usage_add(self):
        from lib.models.base import TokenUsage

        assert TokenUsage(10, 20, 30) + TokenUsage(5, 10, 15) == TokenUsage(15, 30, 45)

    def test_parse_context_window_edge_cases(self):
        from lib.models.base import parse_context_window

        assert parse_context_window(None) is None
        assert parse_context_window(False) is None
        assert parse_context_window(True) is None

    def test_parse_context_window_int(self):
        from lib.models.base import parse_context_window

        assert parse_context_window(128_000) == 128000

    @staticmethod
    def _dummy():
        from lib.models.base import BaseModel, ModelInfo

        class M(BaseModel):
            def _initialize_model(self):
                return None

            def chat(self, messages, **kw):
                return ""

            def chat_stream(self, messages, **kw):
                return iter(())

            def get_model_info(self):
                return ModelInfo(name="x", model_type="t", provider="p", supported_params=[])

        return M

    def test_detect_context_window_uses_manual_override(self):
        model = self._dummy()("test", context_window=64000)

        assert model.context_window == 64000
        assert model.detect_context_window() == 64000

    def test_convert_messages(self):
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        converted = self._dummy()("test").convert_messages([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])

        assert isinstance(converted[0], SystemMessage)
        assert isinstance(converted[1], HumanMessage)
        assert isinstance(converted[2], AIMessage)

    def test_validate_temperature(self):
        """自有传输基类保留旧方法名与旧语义。"""
        model = self._dummy()("test")

        assert model.validate_temperature(0.5) == 0.5
        assert model.validate_temperature(2.0) == 1.0
        assert model.validate_temperature(-1.0) == 0.0

    def test_reset_session_usage(self):
        from lib.models.base import TokenUsage

        model = self._dummy()("test")
        model._record_usage(TokenUsage(10, 20, 30))

        assert model.session_usage.total_tokens == 30

        model.reset_session_usage()

        assert model.session_usage.total_tokens == 0
