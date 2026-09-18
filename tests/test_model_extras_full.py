# models/extras 全覆盖：窗口、用量、转换、调用、元信息。

from types import SimpleNamespace

import pytest

from lib.models.extras import ModelExtras
from lib.models.vocabulary import TokenUsage


class FakeModel(ModelExtras):
    # 不经过 pydantic 的最小模型替身。
    MODEL_TYPE_NAME = "fake"
    PROVIDER_NAME = "FakeProvider"

    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.model_name = kw.get("model_name", "m")
        self.temperature = kw.get("temperature", 0.2)

    def invoke(self, messages, **kw):
        return SimpleNamespace(content="reply", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})

    def stream(self, messages, **kw):
        yield SimpleNamespace(content="a", usage_metadata={})
        yield SimpleNamespace(content="b", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
        yield SimpleNamespace(content="")


class TestWindow:
    def test_default(self):
        assert FakeModel().context_window == 0
        assert FakeModel().context_window_source == ""

    def test_set_variants(self):
        m = FakeModel()
        m.context_window = "128k"
        assert m.context_window == 131072 and m.context_window_source == "manual"
        m.context_window = "oops"
        assert m.context_window == 131072
        m.context_window = 0
        assert m.context_window == 131072
        m.context_window = None
        assert m.context_window == 0 and m.context_window_source == ""

    def test_source_setter(self):
        m = FakeModel()
        m.context_window_source = "api"
        assert m.context_window_source == "api"
        m.context_window_source = None
        assert m.context_window_source == ""

    def test_detect_manual(self):
        m = FakeModel()
        m.context_window = 8000
        assert m.detect_context_window() == 8000

    def test_detect_api(self, monkeypatch):
        m = FakeModel()
        monkeypatch.setattr(m, "_probe_api_for_context_window", lambda: 16000)
        assert m.detect_context_window() == 16000
        assert m.context_window_source == "api"

    def test_detect_none(self):
        assert FakeModel().detect_context_window() is None
        assert FakeModel._probe_api_for_context_window(FakeModel()) is None


class TestUsage:
    def test_session_accumulates(self):
        m = FakeModel()
        assert m.session_usage.total_tokens == 0
        m._record_usage(TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15))
        assert m.last_usage.total_tokens == 15
        assert m.session_usage.total_tokens == 15
        m.reset_session_usage()
        assert m.session_usage.total_tokens == 0

    def test_last_wrong_type(self):
        m = FakeModel()
        m.__dict__["_last_usage"] = "oops"
        assert m.last_usage is None

    def test_extract_variants(self):
        ex = FakeModel._extract_usage_from_response
        assert ex(None).total_tokens == 0
        assert ex(SimpleNamespace(usage_metadata={"input_tokens": 3, "output_tokens": 4})).total_tokens == 7
        assert ex(SimpleNamespace(response_metadata={"token_usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}})).prompt_tokens == 1
        assert ex(SimpleNamespace(response_metadata={"usage": {"prompt_tokens": 1, "completion_tokens": 2}})).total_tokens == 3
        assert ex(SimpleNamespace(usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 9})).total_tokens == 9
        assert ex(SimpleNamespace(token_usage=SimpleNamespace(prompt_tokens="x", completion_tokens=1, total_tokens=0))).completion_tokens == 1
        assert ex(SimpleNamespace()).total_tokens == 0

    def test_estimate(self):
        out = FakeModel._estimate_usage_from_text([{"content": "abc"}], "defghi")
        assert (out.prompt_tokens, out.completion_tokens, out.total_tokens) == (1, 2, 3)

    def test_clamp(self):
        assert FakeModel().clamp_temperature(2.0) == 1.0
        assert FakeModel().clamp_temperature(-1.0) == 0.0
        assert FakeModel().clamp_temperature(0.5) == 0.5


class TestConvert:
    def test_roles(self):
        m = FakeModel()
        out = m.convert_messages([{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"},
                                  {"role": "system", "content": "s"}, {"role": "weird", "content": "w"},
                                  {}])
        assert [type(x).__name__ for x in out] == ["HumanMessage", "AIMessage", "SystemMessage", "HumanMessage", "HumanMessage"]

    def test_prepare(self):
        m = FakeModel()
        assert len(m.prepare_messages("hi")) == 1
        out = m.prepare_messages("hi", system_prompt="sys", history=[{"role": "user", "content": "q"}])
        assert len(out) == 3 and out[0].content == "sys"


class TestCalls:
    def test_chat(self):
        m = FakeModel()
        assert m.chat([{"role": "user", "content": "hi"}]) == "reply"
        assert m.last_usage.total_tokens == 15

    def test_chat_no_content(self):
        class Odd(FakeModel):
            def invoke(self, messages, **kw):
                return 42

        assert Odd().chat([{"role": "user", "content": "hi"}]) == "42"

    def test_stream(self):
        m = FakeModel()
        assert "".join(m.chat_stream([{"role": "user", "content": "hi"}])) == "ab"
        assert m.last_usage.total_tokens == 15

    def test_stream_plain_chunks(self):
        class Plain(FakeModel):
            def stream(self, messages, **kw):
                yield "x" * 10
                yield ""

        m = Plain()
        out = "".join(m.chat_stream([{"role": "user", "content": "hi there friend"}]))
        assert out == "x" * 10
        assert m.last_usage.total_tokens == 8

    def test_check_connection_ok(self):
        assert FakeModel().check_connection() is True

    def test_check_connection_fail(self):
        class Down(FakeModel):
            def invoke(self, messages, **kw):
                raise RuntimeError("down")

        assert Down().check_connection() is False


class TestMeta:
    def test_info(self):
        info = FakeModel().get_model_info()
        assert info.name == "m" and info.provider == "FakeProvider"

    def test_extra_params(self):
        m = FakeModel()
        assert m.extra_params == {}
        m.__dict__["_extra_params"] = {"k": "v"}
        assert m.extra_params == {"k": "v"}
        m.__dict__["_extra_params"] = "oops"
        assert m.extra_params == {}

    def test_config_repr(self):
        m = FakeModel()
        assert m.get_config()["model_name"] == "m"
        assert "FakeModel" in repr(m)

    def test_parse_edges(self):
        from lib.models.vocabulary import parse_context_window

        assert parse_context_window(1.5) is None
        assert parse_context_window(8.0) == 8
        assert parse_context_window("1.5") is None
        assert parse_context_window("0") is None
        assert parse_context_window("999999999999") is None
        assert parse_context_window("8k tokens") == 8192

    def test_check_detected_print(self):
        m = FakeModel()
        m.context_window = 8000
        assert m.check_connection() is True

    def test_base_alias_bind(self):
        from lib.models.base import BaseModel

        class Concrete(BaseModel):
            def _initialize_model(self):
                return None

            def chat(self, messages, **kw):
                return "hi"

            def chat_stream(self, messages, **kw):
                yield "hi"

            def get_model_info(self):
                return None

        c = Concrete.__new__(Concrete)
        assert c.validate_temperature(2.0) == 1.0
        c._model = None
        with pytest.raises(NotImplementedError):
            c.bind_tools([])
        c._model = SimpleNamespace(bind_tools=lambda tools: ("bound", tools))
        assert c.bind_tools(["t"]) == ("bound", ["t"])

    def test_lazy_attr(self):
        import lib.models as _models

        assert _models.OllamaModel is not None
        with pytest.raises(AttributeError):
            _models.GhostProtocol

    def test_read_int(self):
        from lib.models.extras import _read_int

        assert _read_int({"k": "5"}, "k") == 5
        assert _read_int({"k": "oops"}, "k") == 0
        assert _read_int({}, "k") == 0
        assert _read_int(SimpleNamespace(k=7), "k") == 7
        assert _read_int(SimpleNamespace(k="bad"), "k") == 0
        assert _read_int(None, "k") == 0
