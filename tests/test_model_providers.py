"""Unit tests for model providers: Gemini, OpenAI, Anthropic, Ollama."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ── GeminiModel ──────────────────────────────────────────────────────────

class TestGeminiModel:
    @staticmethod
    def _make_model(**kw: Any):
        from lib.models.gemini_model import GeminiModel

        return GeminiModel(
            model_name=kw.pop("model_name", "gemini-2.5-flash"),
            api_key=kw.pop("api_key", "test-key"),
            **kw,
        )

    def test_init_requires_api_key(self):
        from lib.models.gemini_model import GeminiModel

        m = GeminiModel(model_name="gemini-2.5-flash")
        assert m.api_key is None
        assert m.model_name == "gemini-2.5-flash"

    def test_build_url_generate_content(self):
        m = self._make_model()
        assert "/models/gemini-2.5-flash:generateContent" in m._build_url(streaming=False)

    def test_build_url_stream(self):
        m = self._make_model()
        url = m._build_url(streaming=True)
        assert "streamGenerateContent" in url
        assert "alt=sse" in url

    def test_build_headers_raises_without_key(self):
        from lib.models.gemini_model import GeminiModel

        m = GeminiModel(model_name="gemini-2.5-flash", api_key=None)
        with pytest.raises(ValueError, match="API Key"):
            m._build_headers()

    def test_build_headers_includes_key(self):
        m = self._make_model(api_key="k123")
        headers = m._build_headers()
        assert headers["x-goog-api-key"] == "k123"

    def test_build_payload_system_instruction(self):
        m = self._make_model()
        payload = m._build_payload([
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ])
        assert "systemInstruction" in payload
        assert payload["systemInstruction"]["parts"][0]["text"] == "You are helpful."
        assert len(payload["contents"]) == 1

    def test_build_payload_role_mapping(self):
        m = self._make_model()
        payload = m._build_payload([
            {"role": "assistant", "content": "Hello"},
            {"role": "user", "content": "World"},
        ])
        contents = payload["contents"]
        assert contents[0]["role"] == "model"
        assert contents[1]["role"] == "user"

    def test_build_payload_empty_messages(self):
        m = self._make_model()
        payload = m._build_payload([])
        assert len(payload["contents"]) == 1
        assert payload["contents"][0]["parts"][0]["text"] == ""

    def test_extract_text_single_candidate(self):
        m = self._make_model()
        text = m._extract_text({
            "candidates": [
                {"content": {"parts": [{"text": "Hello world"}]}},
            ],
        })
        assert text == "Hello world"

    def test_extract_text_multiple_parts(self):
        m = self._make_model()
        text = m._extract_text({
            "candidates": [
                {"content": {"parts": [{"text": "Part1"}, {"text": "Part2"}]}},
            ],
        })
        assert text == "Part1Part2"

    def test_extract_text_empty(self):
        m = self._make_model()
        assert m._extract_text({}) == ""

    def test_convert_tools_to_gemini(self):
        m = self._make_model()
        tool_mock = MagicMock()
        tool_mock.name = "search"
        tool_mock.description = "Search the web"
        tool_mock.get_input_schema.return_value.model_json_schema.return_value = {
            "title": "SearchInput",
            "type": "object",
            "$defs": {"Unused": {"type": "string"}},
            "properties": {"query": {"title": "Query", "type": "string"}},
        }
        result = m._convert_tools_to_gemini([tool_mock])
        assert len(result) == 1
        assert "function_declarations" in result[0]
        decl = result[0]["function_declarations"][0]
        assert decl["name"] == "search"
        assert decl["description"] == "Search the web"
        assert "title" not in decl["parameters"]
        assert "$defs" not in decl["parameters"]

    def test_extract_function_calls(self):
        m = self._make_model()
        calls = m._extract_function_calls({
            "candidates": [
                {"content": {"parts": [
                    {"functionCall": {"name": "get_weather", "args": {"city": "NYC"}}},
                ]}},
            ],
        })
        assert len(calls) == 1
        assert calls[0]["name"] == "get_weather"
        assert calls[0]["arguments"] == {"city": "NYC"}

    def test_chat_with_tools_returns_text_when_no_function_call(self):
        m = self._make_model()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "candidates": [
                {"content": {"parts": [{"text": "The weather is sunny."}]}},
            ],
        }
        mock_response.raise_for_status.return_value = None
        with patch.object(m, "_build_payload", return_value={}) as _bp, \
             patch("requests.post", return_value=mock_response) as _post:
            result = m.chat_with_tools(
                [{"role": "user", "content": "Weather?"}],
                tools=[],
            )
            assert result == "The weather is sunny."

    def test_chat_with_tools_returns_json_when_function_call(self):
        m = self._make_model()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "candidates": [
                {"content": {"parts": [
                    {"functionCall": {"name": "search", "args": {"q": "test"}}},
                ]}},
            ],
        }
        mock_response.raise_for_status.return_value = None
        with patch.object(m, "_build_payload", return_value={}), \
             patch("requests.post", return_value=mock_response):
            result = m.chat_with_tools(
                [{"role": "user", "content": "Search"}],
                tools=[MagicMock()],
            )
            parsed = json.loads(result)
            assert "tool_calls" in parsed
            assert parsed["tool_calls"][0]["name"] == "search"

    def test_bind_tools_returns_langchain_tool_calls(self):
        from langchain_core.messages import HumanMessage

        m = self._make_model()
        tool_mock = MagicMock()
        tool_mock.name = "search"
        tool_mock.description = "Search"
        tool_mock.get_input_schema.return_value.model_json_schema.return_value = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        }
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "candidates": [
                {"content": {"parts": [
                    {"functionCall": {"name": "search", "args": {"query": "test"}}},
                ]}},
            ],
            "usageMetadata": {
                "promptTokenCount": 7,
                "candidatesTokenCount": 3,
                "totalTokenCount": 10,
            },
        }
        mock_response.raise_for_status.return_value = None

        chat = m.bind_tools([tool_mock])
        with patch("requests.post", return_value=mock_response) as post:
            result = chat.invoke([HumanMessage(content="Search")])

        assert result.tool_calls[0]["name"] == "search"
        assert result.tool_calls[0]["args"] == {"query": "test"}
        assert m.last_usage.total_tokens == 10
        payload = post.call_args.kwargs["json"]
        assert payload["tools"][0]["function_declarations"][0]["name"] == "search"

    def test_probe_api_for_context_window(self):
        m = self._make_model()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"inputTokenLimit": 1048576}
        with patch("requests.get", return_value=mock_resp):
            cw = m._probe_api_for_context_window()
            assert cw == 1048576

    def test_get_model_info(self):
        m = self._make_model()
        info = m.get_model_info()
        assert info.model_type == "gemini"
        assert info.supports_streaming is True


# ── OllamaModel ──────────────────────────────────────────────────────────

class TestOllamaModel:
    def test_init_defaults(self):
        from lib.models.ollama_model import OllamaModel

        m = OllamaModel(model_name="llama3.2")
        assert m.model_name == "llama3.2"
        assert m.base_url.rstrip("/") == "http://localhost:11434"

    def test_check_connection_returns_false_on_failure(self):
        from lib.models.ollama_model import OllamaModel

        m = OllamaModel(model_name="no-model")
        m._initialize_model = MagicMock()
        m.chat = MagicMock(side_effect=Exception("offline"))
        assert m.check_connection() is False

    def test_check_connection_detects_context_window(self):
        from lib.models.ollama_model import OllamaModel

        m = OllamaModel(model_name="llama3.2")
        m._initialize_model = MagicMock()
        m.chat = MagicMock(return_value="ok")
        m.detect_context_window = MagicMock(return_value=128000)
        assert m.check_connection() is True

    def test_repr(self):
        from lib.models.ollama_model import OllamaModel

        m = OllamaModel(model_name="llama3.2")
        assert "llama3.2" in repr(m)


# ── OpenAIModel ──────────────────────────────────────────────────────────

class TestOpenAIModel:
    def test_init_defaults(self):
        from lib.models.openai_model import OpenAIModel

        m = OpenAIModel(model_name="gpt-4o")
        assert m.model_name == "gpt-4o"

    def test_get_model_info(self):
        from lib.models.openai_model import OpenAIModel

        m = OpenAIModel(model_name="gpt-4o")
        info = m.get_model_info()
        assert info.model_type == "openai"

    def test_repr(self):
        from lib.models.openai_model import OpenAIModel

        m = OpenAIModel(model_name="gpt-4o")
        assert "gpt-4o" in repr(m)

    def test_deepseek_prefers_deepseek_key_over_openai_env(self, monkeypatch):
        from lib.models.openai_model import OpenAIModel
        import lib.models.universal_chat_openai as universal

        captured = {}

        class FakeDeepSeek:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")
        monkeypatch.setattr(universal, "UniversalChatDeepSeek", FakeDeepSeek)

        m = OpenAIModel(model_name="deepseek-chat", base_url="https://api.deepseek.com/v1")
        m._init_deepseek()

        assert captured["api_key"] == "deepseek-key"

    def test_inject_nonstandard_fields_uses_matching_ai_message(self):
        from langchain_core.messages import AIMessage, HumanMessage
        from lib.models.universal_chat_openai import _inject_nonstandard_fields

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
        )

        assert payload["messages"][0]["reasoning_content"] == "r1"
        assert payload["messages"][2]["reasoning_content"] == "r2"


# ── AnthropicModel ───────────────────────────────────────────────────────

class TestAnthropicModel:
    def test_init_defaults(self):
        from lib.models.anthropic_model import AnthropicModel

        m = AnthropicModel(model_name="claude-sonnet-4-6")
        assert m.model_name == "claude-sonnet-4-6"

    def test_get_model_info(self):
        from lib.models.anthropic_model import AnthropicModel

        m = AnthropicModel(model_name="claude-sonnet-4-6")
        info = m.get_model_info()
        assert info.model_type == "anthropic"

    def test_import_failure_prints_hint(self, capsys):
        with patch("lib.models.anthropic_model._check_anthropic_available", return_value=False):
            from lib.models.anthropic_model import AnthropicModel

            m = AnthropicModel(model_name="claude-sonnet-4-6")
            result = m.check_connection()
            captured = capsys.readouterr()
            assert result is False
            assert "langchain-anthropic" in captured.out


# ── BaseModel utilities ──────────────────────────────────────────────────

class TestBaseModel:
    def test_token_usage_add(self):
        from lib.models.base import TokenUsage

        a = TokenUsage(10, 20, 30)
        b = TokenUsage(5, 10, 15)
        c = a + b
        assert c.prompt_tokens == 15
        assert c.completion_tokens == 30
        assert c.total_tokens == 45

    def test_parse_context_window_edge_cases(self):
        from lib.models.base import parse_context_window

        assert parse_context_window(None) is None
        assert parse_context_window(False) is None
        assert parse_context_window(True) is None

    def test_parse_context_window_int(self):
        from lib.models.base import parse_context_window

        assert parse_context_window(128_000) == 128000

    def test_detect_context_window_uses_manual_override(self):
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

        model = M("test", context_window=64000)
        assert model.context_window == 64000
        assert model.detect_context_window() == 64000

    def test_convert_messages(self):
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

        model = M("test")
        converted = model.convert_messages([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])
        from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

        assert isinstance(converted[0], SystemMessage)
        assert isinstance(converted[1], HumanMessage)
        assert isinstance(converted[2], AIMessage)

    def test_validate_temperature(self):
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

        model = M("test")
        assert model.validate_temperature(0.5) == 0.5
        assert model.validate_temperature(2.0) == 1.0
        assert model.validate_temperature(-1.0) == 0.0

    def test_reset_session_usage(self):
        from lib.models.base import BaseModel, ModelInfo, TokenUsage

        class M(BaseModel):
            def _initialize_model(self):
                return None

            def chat(self, messages, **kw):
                return ""

            def chat_stream(self, messages, **kw):
                return iter(())

            def get_model_info(self):
                return ModelInfo(name="x", model_type="t", provider="p", supported_params=[])

        model = M("test")
        model._record_usage(TokenUsage(10, 20, 30))
        assert model.session_usage.total_tokens == 30
        model.reset_session_usage()
        assert model.session_usage.total_tokens == 0
