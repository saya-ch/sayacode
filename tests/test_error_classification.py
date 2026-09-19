"""恢复路径的错误分类回归测试。

背景：`classify_error` 的分类直接决定恢复动作。曾出现两类误判：
- 上下文超限被归为 max_output_tokens，于是给已超限的 prompt 再加一条消息；
- 最主流的超限文案被判 fatal，一次都不重试、不压缩。
"""

import pytest

from lib.agent.recovery import (
    classify_error,
    MAX_OUTPUT_TOKENS_PATTERNS,
    PROMPT_TOO_LONG_PATTERNS,
    RECOVERABLE_ERROR_PATTERNS,
    retry_delay,
)


@pytest.mark.parametrize(
    "message",
    [
        # OpenAI 上下文超限（最主流文案）
        "This model's maximum context length is 128000 tokens. However, your messages "
        "resulted in 150000 tokens. Please reduce the length of the messages.",
        # Anthropic 超限
        "prompt is too long: 213916 tokens > 200000 maximum",
        # OpenAI 错误码形式（下划线）
        "Error code: 400 - {'error': {'code': 'context_length_exceeded'}}",
        # 其他变体
        "Input length 500000 exceeds maximum allowed input length",
        "context window exceeded",
        "too many tokens in request",
    ],
)
def test_context_overflow_is_prompt_too_long(message):
    """上下文超限必须归为 prompt_too_long，才能触发压缩而非追加消息。"""
    assert classify_error(message) == "prompt_too_long"


@pytest.mark.parametrize(
    "message",
    [
        "max_output_tokens exceeded",
        "You have hit the max tokens limit for this model",
        "output token limit reached",
    ],
)
def test_output_limit_is_max_output_tokens(message):
    """输出 token 上限应缩短回复，与上下文超限区分开。"""
    assert classify_error(message) == "max_output_tokens"


@pytest.mark.parametrize(
    "message",
    [
        "Rate limit reached for requests",
        "The server is overloaded, please retry",
        "Request timed out",
        "Connection reset by peer",
        "503 Service Unavailable",
    ],
)
def test_transient_errors_are_recoverable(message):
    assert classify_error(message) == "recoverable"


@pytest.mark.parametrize(
    "message",
    [
        # 参数校验类错误：重试不会成功，且会重放已执行的有副作用工具调用
        "Invalid parameter: connection_timeout must be > 0",
        "invalid_request_error: model does not exist",
        "Validation error for parameter temperature",
    ],
)
def test_parameter_errors_are_fatal_not_retried(message):
    """参数错误不得被判为可重试，否则会重复执行有副作用的调用。"""
    assert classify_error(message) == "fatal"


def test_prompt_too_long_patterns_are_checked_before_output_patterns():
    """超限判定必须先于输出上限，否则混有 max tokens 字样的超限文案会被误判。"""
    combined = "This model's maximum context length is 128000 tokens; max tokens exceeded"
    assert classify_error(combined) == "prompt_too_long"


def test_pattern_tables_do_not_overlap_on_context_overflow():
    """max_output 表不应包含上下文超限的关键字。"""
    for pattern in MAX_OUTPUT_TOKENS_PATTERNS:
        assert "context" not in pattern
    assert any("context" in pattern for pattern in PROMPT_TOO_LONG_PATTERNS)


def test_recoverable_patterns_include_spaced_timed_out():
    """'timeout' 匹配不到 'timed out'，两种写法都要覆盖。"""
    assert "timed out" in RECOVERABLE_ERROR_PATTERNS


def testretry_delay_is_exponential():
    delays = [retry_delay(attempt) for attempt in range(1, 4)]
    assert delays[1] / delays[0] == pytest.approx(delays[2] / delays[1])
    assert delays[0] < delays[1] < delays[2]
