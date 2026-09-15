"""流式输出行为回归。

本文件的用例来自**真实 HTTP 端到端测试**（本地假厂商 + 真实请求），两个缺陷都是
mock 测试与契约冻结测试都没有覆盖到的：

1. ``chat_stream`` 把**没有 content 的 chunk 对象 repr 当成文本产出**。
   结束块与 usage 块的 content 是空字符串；判据只能是「有没有 content 属性」，
   不能是「content 是否非空」。
2. 流式用量取自**最后一个块**是错的：用量块之后通常还有一个只带
   ``chunk_position='last'`` 的空收尾块。实测真实用量 7/3/10 被静默估算成 3/2/5。
   正确做法是记住最后一个**带用量**的块。

这两条都是继承自旧实现的缺陷，重写时被真实链路暴露出来。
"""

from typing import Any, Iterator

from langchain_core.messages import AIMessageChunk

from lib.models import TokenUsage
from lib.models.extras import ModelExtras


class _StreamingModel(ModelExtras):
    """最小流式替身：按预设序列产出块，不碰网络。"""

    def __init__(self, chunks: list[Any]) -> None:
        self._preset_chunks = chunks

    def stream(self, messages: Any, **kwargs: Any) -> Iterator[Any]:
        yield from self._preset_chunks


def _content_chunk(text: str) -> AIMessageChunk:
    return AIMessageChunk(content=text)


def _usage_chunk(prompt: int, completion: int, total: int) -> AIMessageChunk:
    return AIMessageChunk(
        content="",
        usage_metadata={
            "input_tokens": prompt,
            "output_tokens": completion,
            "total_tokens": total,
        },
    )


def _final_chunk() -> AIMessageChunk:
    """真实 SSE 流的收尾块：content 为空、且不带用量。"""
    return AIMessageChunk(content="")


def test_stream_yields_only_text_content():
    model = _StreamingModel([
        _content_chunk("Hel"),
        _content_chunk("lo"),
        _content_chunk("!"),
        _usage_chunk(7, 3, 10),
        _final_chunk(),
    ])

    chunks = list(model.chat_stream([{"role": "user", "content": "say hello"}]))

    assert chunks == ["Hel", "lo", "!"]
    assert all("content=" not in chunk for chunk in chunks)


def test_stream_usage_comes_from_usage_chunk_not_last_chunk():
    """用量块之后还有空收尾块时，必须仍取到真实用量，而不是退化成字符估算。"""
    model = _StreamingModel([
        _content_chunk("Hello!"),
        _usage_chunk(7, 3, 10),
        _final_chunk(),
    ])

    list(model.chat_stream([{"role": "user", "content": "say hello"}]))

    assert model.session_usage == TokenUsage(7, 3, 10)


def test_stream_falls_back_to_estimate_when_provider_reports_no_usage():
    """上游确实不报用量时才允许估算 —— 估算值必须为正，不能静默记成 0。"""
    model = _StreamingModel([_content_chunk("Hello!"), _final_chunk()])

    list(model.chat_stream([{"role": "user", "content": "say hello"}]))

    usage = model.session_usage
    assert usage.total_tokens > 0
    assert usage.total_tokens == usage.prompt_tokens + usage.completion_tokens
