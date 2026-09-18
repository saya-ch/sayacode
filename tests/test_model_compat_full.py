# compat 全覆盖：提取、回填、mixin 钩子（真实 OpenAI 类离线测）。


import lib.models.compat as cp
from lib.models.compat import (
    apply_compat_to_payload,
    extract_reasoning_text,
    passthrough_fields,
)
from lib.models.provider_catalog import CompatSwitches


def _openai():
    # 真实 OpenAI 协议类（离线构造，不发请求）。
    import lib.models.providers as _pv

    cls = _pv.resolve_protocol_class("openai")
    assert cls is not None
    return cls(model="m", api_key="k", base_url="http://x")


class TestExtract:
    def test_string_forms(self):
        assert extract_reasoning_text({"reasoning_content": "r"}) == "r"
        assert extract_reasoning_text({"reasoning": "r"}) == "r"
        assert extract_reasoning_text({}) == ""
        assert extract_reasoning_text("oops") == ""
        assert extract_reasoning_text({"reasoning": ""}) == ""

    def test_list_form(self):
        assert extract_reasoning_text({"reasoning": ["a", {"text": "b"}, {"content": "c"}, {}, 1]}) == "abc"
        assert extract_reasoning_text({"reasoning": []}) == ""
        assert extract_reasoning_text({"reasoning_details": [{"type": "reasoning.text", "text": "z"}]}) == "z"

    def test_join_non_list(self):
        assert cp._join_reasoning_blocks("oops") == ""
        assert cp._join_reasoning_blocks(["a", {"text": ""}, {"other": 1}, None]) == "a"

    def test_first_delta(self):
        assert cp._first_delta("oops") == {}
        assert cp._first_delta({}) == {}
        assert cp._first_delta({"choices": []}) == {}
        assert cp._first_delta({"choices": ["oops"]}) == {}
        assert cp._first_delta({"choices": [{"delta": "oops"}]}) == {}
        assert cp._first_delta({"choices": [{"delta": {"content": "x"}}]}) == {"content": "x"}
        assert cp._first_delta({"chunk": {"choices": [{"delta": {"content": "y"}}]}}) == {"content": "y"}

    def test_accumulate(self):
        assert cp._accumulate(None, "n") == "n"
        assert cp._accumulate("a", "b") == "ab"
        assert cp._accumulate([1], [2]) == [1, 2]
        assert cp._accumulate("a", [1]) == [1]

    def test_passthrough_fields(self):
        assert passthrough_fields(None) == frozenset()
        assert passthrough_fields(CompatSwitches()) == frozenset()
        assert "reasoning_content" in passthrough_fields(CompatSwitches(passthrough_nonstandard=True))
        assert "custom" in passthrough_fields(CompatSwitches(passthrough_nonstandard=True, extra_passthrough_fields=("custom",)))


class TestExtractFields:
    def test_model_extra_crash(self):
        class Bad:
            reasoning_content = "r"

            @property
            def model_extra(self):
                raise RuntimeError("boom")

        out = cp._extract_nonstandard_fields(Bad(), frozenset({"reasoning_content"}))
        assert out == {"reasoning_content": "r"}

    def test_dict_extra(self):
        class Msg:
            model_extra = {"reasoning_content": "r", "content": "c", "empty": ""}

        out = cp._extract_nonstandard_fields(Msg(), frozenset({"reasoning_content", "content", "empty"}))
        assert out == {"reasoning_content": "r", "content": "c"}

    def test_strip(self):
        out = cp._strip_standard_fields({"reasoning_content": "r", "content": "c", "x": None},
                                        frozenset({"reasoning_content", "content", "x"}))
        assert out == {"reasoning_content": "r", "content": "c"}


class TestInject:
    def test_non_list(self):
        cp._inject_nonstandard_fields("oops", {}, frozenset())

    def test_skips(self):
        from langchain_core.messages import AIMessage

        payload = {"messages": ["oops", {"role": "user"}, {"role": "assistant"}, {"role": "assistant"}]}
        msgs = [AIMessage(content="a", additional_kwargs={"reasoning_content": "r"})]
        cp._inject_nonstandard_fields(msgs, payload, frozenset({"reasoning_content"}))
        assert payload["messages"][2]["reasoning_content"] == "r"
        assert "reasoning_content" not in payload["messages"][3]

    def test_no_overwrite(self):
        from langchain_core.messages import AIMessage

        payload = {"messages": [{"role": "assistant", "reasoning_content": "old"}]}
        msgs = [AIMessage(content="a", additional_kwargs={"reasoning_content": "new"})]
        cp._inject_nonstandard_fields(msgs, payload, frozenset({"reasoning_content"}))
        assert payload["messages"][0]["reasoning_content"] == "old"


class TestApply:
    def test_no_max_tokens(self):
        payload = {"max_tokens": 5, "max_completion_tokens": 6, "model": "m"}
        apply_compat_to_payload(payload, CompatSwitches(supports_max_output_tokens=False))
        assert "max_tokens" not in payload and payload["model"] == "m"

    def test_rename_field(self):
        payload = {"max_tokens": 5}
        apply_compat_to_payload(payload, CompatSwitches(max_tokens_field="max_completion_tokens"))
        assert payload == {"max_completion_tokens": 5}

    def test_system_role(self):
        payload = {"messages": [{"role": "system", "content": "s"}, "oops"]}
        apply_compat_to_payload(payload, CompatSwitches(system_role="developer"))
        assert payload["messages"][0]["role"] == "developer"

    def test_default_noop(self):
        payload = {"max_tokens": 5, "messages": [{"role": "system"}]}
        apply_compat_to_payload(payload, CompatSwitches())
        assert payload["max_tokens"] == 5


class TestMixin:
    def test_chunk_hook_extracts(self):
        from langchain_core.messages import AIMessageChunk

        m = _openai()
        chunk = {"id": "1", "choices": [{"delta": {"content": "x", "reasoning_content": "r"}, "index": 0}],
                 "created": 1, "model": "m", "object": "chat.completion.chunk"}
        gen = m._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, {})
        assert gen.message.additional_kwargs.get("reasoning_content") == "r"

    def test_chunk_hook_no_delta(self):
        from langchain_core.messages import AIMessageChunk

        m = _openai()
        chunk = {"id": "1", "choices": [], "created": 1, "model": "m", "object": "chat.completion.chunk"}
        gen = m._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, {})
        assert gen is not None

    def test_chunk_hook_crash(self, monkeypatch):
        from langchain_core.messages import AIMessageChunk

        m = _openai()
        monkeypatch.setattr(cp, "_accumulate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        chunk = {"id": "1", "choices": [{"delta": {"content": "x", "reasoning_content": "r"}, "index": 0}],
                 "created": 1, "model": "m", "object": "chat.completion.chunk"}
        gen = m._convert_chunk_to_generation_chunk(chunk, AIMessageChunk, {})
        assert gen is not None

    def test_chat_result_extracts(self):
        from openai.types.chat import ChatCompletion, ChatCompletionMessage
        from openai.types.chat.chat_completion import Choice

        m = _openai()
        message = ChatCompletionMessage(role="assistant", content="hi", reasoning_content="thought")
        resp = ChatCompletion(id="1", choices=[Choice(finish_reason="stop", index=0, message=message)],
                              created=1, model="m", object="chat.completion")
        result = m._create_chat_result(resp)
        assert result.generations[0].message.additional_kwargs.get("reasoning_content") == "thought"

    def test_payload_backfill(self):
        from langchain_core.messages import AIMessage, HumanMessage

        m = _openai()
        msgs = [HumanMessage(content="q"), AIMessage(content="a", additional_kwargs={"reasoning_content": "r"})]
        payload = m._get_request_payload(msgs)
        assistants = [x for x in payload["messages"] if x.get("role") == "assistant"]
        assert assistants and assistants[0].get("reasoning_content") == "r"

    def test_result_skips_foreign_generations(self, monkeypatch):
        from openai.types.chat import ChatCompletion, ChatCompletionMessage
        from openai.types.chat.chat_completion import Choice

        m = _openai()
        monkeypatch.setattr(cp, "ChatGeneration", type("NotAGeneration", (), {}))
        message = ChatCompletionMessage(role="assistant", content="hi", reasoning_content="thought")
        resp = ChatCompletion(id="1", choices=[Choice(finish_reason="stop", index=0, message=message)],
                              created=1, model="m", object="chat.completion")
        result = m._create_chat_result(resp)
        assert result is not None

    def test_result_extract_crash(self, monkeypatch):
        from openai.types.chat import ChatCompletion, ChatCompletionMessage
        from openai.types.chat.chat_completion import Choice

        m = _openai()
        monkeypatch.setattr(cp, "_extract_nonstandard_fields",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        message = ChatCompletionMessage(role="assistant", content="hi", reasoning_content="thought")
        resp = ChatCompletion(id="1", choices=[Choice(finish_reason="stop", index=0, message=message)],
                              created=1, model="m", object="chat.completion")
        assert m._create_chat_result(resp) is not None

    def test_payload_inject_crash(self, monkeypatch):
        from langchain_core.messages import AIMessage, HumanMessage

        m = _openai()
        monkeypatch.setattr(cp, "_inject_nonstandard_fields",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        msgs = [HumanMessage(content="q"), AIMessage(content="a", additional_kwargs={"reasoning_content": "r"})]
        assert m._get_request_payload(msgs)["model"] == "m"

    def test_payload_apply_crash(self, monkeypatch):
        from langchain_core.messages import HumanMessage

        m = _openai()
        monkeypatch.setattr(cp, "apply_compat_to_payload",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert m._get_request_payload([HumanMessage(content="q")])["model"] == "m"

    def test_payload_system_role(self):
        from langchain_core.messages import HumanMessage, SystemMessage
        from lib.models.provider_catalog import CompatSwitches as _CS

        m = _openai()
        m.compat = _CS(passthrough_nonstandard=True, system_role="developer")
        payload = m._get_request_payload([SystemMessage(content="s"), HumanMessage(content="q")])
        assert payload["messages"][0]["role"] == "developer"

    def test_serializable(self):
        assert _openai().is_lc_serializable() is False
