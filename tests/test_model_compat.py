"""``CompatSwitches`` 的行为测试 —— 每个开关都必须**真的被读取**。

存在的理由：重写后曾出现过「声明式开关」全部形同虚设的状态 ——

* ``passthrough_nonstandard`` 与 ``thinking_format`` 只被测试断言其**字面值**，
  没有任何生产代码读它们（mixin 无条件提取/回填，开关读了等于没读）；
* ``system_role`` / ``max_tokens_field`` / ``supports_max_output_tokens`` 虽然被
  ``apply_compat_to_payload`` 读取，但目录里 7 个条目全部取默认值，
  于是整个函数对当前所有 provider 都是空操作。

也就是说：打开的两个开关没人读，有人读的三个开关没人打开。本文件把「开关 → 行为」
这条因果链钉死：每个开关都要有一条「打开它 → 行为改变」的用例，
因此任何人把它改回装饰性字段都会立刻红灯。

这正是 ``CODE_REVIEW_FINDINGS.md`` 里 A14（声明了但无人消费）模式的回归保护。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from lib.models.compat import (
    _KNOWN_NONSTANDARD_ATTRS,
    _extract_nonstandard_fields,
    apply_compat_to_payload,
    extract_reasoning_text,
    passthrough_fields,
)
from lib.models.provider_catalog import PROVIDER_CATALOG, CompatSwitches

DUMMY = {"model": "m", "api_key": "dummy", "base_url": "https://example.invalid/v1"}


def _openai_model(**overrides):
    from lib.models import OpenAIModel

    return OpenAIModel(**DUMMY, **overrides)


# ── 总闸：passthrough_nonstandard ────────────────────────────────────────────


def test_no_compat_object_means_no_passthrough():
    """没有 ``compat`` 字段的对象（例如 Anthropic 协议类）不应触发透传。"""
    assert passthrough_fields(None) == frozenset()
    assert passthrough_fields("不是 CompatSwitches") == frozenset()


def test_disabled_gate_yields_no_fields():
    assert passthrough_fields(CompatSwitches()) == frozenset()


def test_enabled_gate_yields_the_builtin_field_set():
    fields = passthrough_fields(CompatSwitches(passthrough_nonstandard=True))

    assert fields == _KNOWN_NONSTANDARD_ATTRS
    assert "reasoning_content" in fields


def test_extra_fields_extend_the_builtin_set():
    fields = passthrough_fields(CompatSwitches(
        passthrough_nonstandard=True,
        extra_passthrough_fields=("my_vendor_note",),
    ))

    assert fields == _KNOWN_NONSTANDARD_ATTRS | {"my_vendor_note"}


# ── 总闸真的作用于线上请求 ───────────────────────────────────────────────────


def test_gate_off_keeps_reasoning_content_out_of_the_request():
    model = _openai_model()
    model.compat = CompatSwitches(passthrough_nonstandard=False)

    payload = model._get_request_payload(
        [AIMessage(content="x", additional_kwargs={"reasoning_content": "secret"})]
    )

    assert "reasoning_content" not in payload["messages"][0]


def test_gate_on_puts_reasoning_content_back_into_the_request():
    model = _openai_model()
    model.compat = CompatSwitches(passthrough_nonstandard=True)

    payload = model._get_request_payload(
        [AIMessage(content="x", additional_kwargs={"reasoning_content": "secret"})]
    )

    assert payload["messages"][0]["reasoning_content"] == "secret"


def test_extra_passthrough_fields_are_carried_into_the_request():
    model = _openai_model()
    model.compat = CompatSwitches(
        passthrough_nonstandard=True,
        extra_passthrough_fields=("my_vendor_note",),
    )

    payload = model._get_request_payload(
        [AIMessage(content="x", additional_kwargs={"my_vendor_note": "hello"})]
    )

    assert payload["messages"][0]["my_vendor_note"] == "hello"


def test_undeclared_field_is_not_carried():
    """声明之外的字段一律不搬 —— 字段集合由声明决定，不是「除标准外都要」。"""
    model = _openai_model()
    model.compat = CompatSwitches(passthrough_nonstandard=True)

    payload = model._get_request_payload(
        [AIMessage(content="x", additional_kwargs={"my_vendor_note": "hello"})]
    )

    assert "my_vendor_note" not in payload["messages"][0]


# ── 提取方向 ─────────────────────────────────────────────────────────────────


def test_extraction_is_limited_to_declared_fields():
    raw = SimpleNamespace(reasoning_content="r", my_vendor_note="n")

    assert _extract_nonstandard_fields(raw, frozenset({"reasoning_content"})) == {
        "reasoning_content": "r"
    }
    assert _extract_nonstandard_fields(
        raw, frozenset({"reasoning_content", "my_vendor_note"})
    ) == {"reasoning_content": "r", "my_vendor_note": "n"}


def test_extraction_reads_declared_fields_from_model_extra():
    """字典形式（``model_extra``）里的字段同样只取声明过的。"""
    raw = SimpleNamespace(model_extra={"my_vendor_note": "n", "other": "x"})

    assert _extract_nonstandard_fields(raw, frozenset({"my_vendor_note"})) == {
        "my_vendor_note": "n"
    }


# ── apply_compat_to_payload：三个请求侧开关 ──────────────────────────────────


def test_system_role_switch_is_applied():
    payload = {
        "messages": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
        ]
    }

    apply_compat_to_payload(payload, CompatSwitches(system_role="developer"))

    assert payload["messages"][0]["role"] == "developer"
    assert payload["messages"][1]["role"] == "user"


def test_max_tokens_field_is_renamed_when_switched():
    payload = {"messages": [], "max_tokens": 10}

    apply_compat_to_payload(payload, CompatSwitches(max_tokens_field="max_completion_tokens"))

    assert payload["max_completion_tokens"] == 10
    assert "max_tokens" not in payload


def test_max_tokens_is_dropped_when_endpoint_rejects_it():
    payload = {"messages": [], "max_tokens": 10, "max_completion_tokens": 20}

    apply_compat_to_payload(payload, CompatSwitches(supports_max_output_tokens=False))

    assert "max_tokens" not in payload
    assert "max_completion_tokens" not in payload


@pytest.mark.parametrize("key", sorted(PROVIDER_CATALOG))
def test_default_switches_change_nothing(key):
    """默认（全关）组合必须是彻底的空操作 —— 标准 OpenAI 行为。"""
    payload = {"messages": [{"role": "system", "content": "s"}], "max_tokens": 10}
    before = {
        "messages": [dict(m) for m in payload["messages"]],
        "max_tokens": 10,
    }

    apply_compat_to_payload(payload, CompatSwitches())

    assert payload == before
    assert passthrough_fields(CompatSwitches()) == frozenset()
    assert PROVIDER_CATALOG[key].compat is not None


class TestSwitchesAreNotDecorative:
    """元测试：每个 ``CompatSwitches`` 字段都必须在 ``lib/models/compat.py`` 里被读取。

    「声明了但没人读」正是 A14 的形态。本测试用 AST 直接检查读取点，
    因此新增一个装饰性开关会立刻失败，而不是等到下一次人工审查。
    """

    EXPECTED_READERS = {
        "passthrough_nonstandard": "passthrough_fields",
        "extra_passthrough_fields": "passthrough_fields",
        "system_role": "apply_compat_to_payload",
        "max_tokens_field": "apply_compat_to_payload",
        "supports_max_output_tokens": "apply_compat_to_payload",
    }

    def test_every_switch_has_a_reader(self):
        import ast
        import pathlib

        import lib.models.compat as compat_module

        source = pathlib.Path(compat_module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        # 收集「哪个函数里出现了 compat.<字段> 形式的属性读取」
        reads: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Attribute)
                    and isinstance(child.value, ast.Name)
                    and child.value.id == "compat"
                ):
                    reads.setdefault(child.attr, set()).add(node.name)

        for field, reader in self.EXPECTED_READERS.items():
            assert field in reads, f"CompatSwitches.{field} 没有任何生产代码读取它"
            assert reader in reads[field], (
                f"CompatSwitches.{field} 的读取点应包含 {reader}，实际为 {sorted(reads[field])}"
            )

    def test_no_switch_is_missing_from_the_reader_table(self):
        """新增开关必须同时登记读取点，否则本文件的元测试会漏掉它。"""
        import dataclasses

        declared = {f.name for f in dataclasses.fields(CompatSwitches)}

        assert declared == set(self.EXPECTED_READERS)


# ── 流式路径 ──────────────────────────────────────────────────────────────────
#
# 真机背景：``_create_chat_result`` 只在非流式时被调用；流式走的是
# ``_convert_chunk_to_generation_chunk``，而 langchain-openai 在那里只认标准字段。
# 实测该网关一次回答里 47 个 chunk 带推理、只有 7 个带正文 —— 不做这件事的话，
# 大部分流式内容在集成层就被丢掉了。


def _sse_chunk(delta: dict) -> dict:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}


def _convert(model, delta: dict):
    from langchain_core.messages import AIMessageChunk

    return model._convert_chunk_to_generation_chunk(
        _sse_chunk(delta), AIMessageChunk, None
    )


def test_first_delta_reads_both_chunk_shapes():
    from lib.models.compat import _first_delta

    assert _first_delta(_sse_chunk({"content": "a"})) == {"content": "a"}
    assert _first_delta({"chunk": {"choices": [{"delta": {"content": "b"}}]}}) == {
        "content": "b"
    }
    assert _first_delta({"choices": []}) == {}
    assert _first_delta("不是 dict") == {}
    assert _first_delta({"choices": [{"delta": None}]}) == {}


def test_accumulate_merges_stream_deltas():
    from lib.models.compat import _accumulate

    assert _accumulate(None, "a") == "a"
    assert _accumulate("a", "b") == "ab"
    assert _accumulate([1], [2]) == [1, 2]
    # 类型不一致时以新值为准，不抛异常
    assert _accumulate("a", [1]) == [1]


def test_streaming_chunk_carries_reasoning_into_additional_kwargs():
    model = _openai_model()
    model.compat = CompatSwitches(passthrough_nonstandard=True)

    generation = _convert(model, {"reasoning": "先看目录", "content": ""})

    assert generation is not None
    assert generation.message.additional_kwargs.get("reasoning") == "先看目录"


def test_streaming_reasoning_accumulates_across_chunks():
    """逐 token 的推理要拼起来，不能一块盖掉上一块。"""
    model = _openai_model()
    model.compat = CompatSwitches(passthrough_nonstandard=True)

    first = _convert(model, {"reasoning": "甲"}).message
    _convert(model, {"reasoning": "乙"})

    # 每个 chunk 是独立消息，增量由提取方累积；这里确认单块的增量是完整的
    assert first.additional_kwargs.get("reasoning") == "甲"


def test_streaming_gate_off_drops_reasoning():
    """总闸关闭时流式也必须什么都不搬。"""
    model = _openai_model()
    model.compat = CompatSwitches(passthrough_nonstandard=False)

    generation = _convert(model, {"reasoning": "不该出现"})

    assert generation is not None
    assert "reasoning" not in (generation.message.additional_kwargs or {})


def test_streaming_undeclared_field_is_not_carried():
    model = _openai_model()
    model.compat = CompatSwitches(passthrough_nonstandard=True)

    generation = _convert(model, {"some_vendor_field": "x"})

    assert generation is not None
    assert "some_vendor_field" not in (generation.message.additional_kwargs or {})


def test_streaming_extra_passthrough_fields_are_carried():
    model = _openai_model()
    model.compat = CompatSwitches(
        passthrough_nonstandard=True, extra_passthrough_fields=("my_note",)
    )

    generation = _convert(model, {"my_note": "v"})

    assert generation is not None
    assert generation.message.additional_kwargs.get("my_note") == "v"


# ── 展示用的推理文本抽取 ──────────────────────────────────────────────────────


def test_reasoning_text_prefers_string_forms_and_joins_blocks():
    assert extract_reasoning_text({"reasoning": "甲"}) == "甲"
    assert extract_reasoning_text({"reasoning_content": "乙"}) == "乙"
    assert extract_reasoning_text({"thinking": "丙"}) == "丙"
    assert extract_reasoning_text(
        {"reasoning_details": [{"type": "reasoning.text", "text": "丁"}, {"text": "戊"}]}
    ) == "丁戊"
    assert extract_reasoning_text({"reasoning_details": ["己"]}) == "己"
    assert extract_reasoning_text({"reasoning_details": "不是列表"}) == ""
    assert extract_reasoning_text({}) == ""
    assert extract_reasoning_text("不是 dict") == ""


def test_reasoning_text_priority_is_deterministic():
    """同时存在多个字段时按固定优先级取，避免输出抖动。"""
    assert extract_reasoning_text({"reasoning": "裸", "reasoning_content": "长"}) == "长"
