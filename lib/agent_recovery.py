"""兼容 shim：恢复策略已搬至 lib.agent.recovery，原路径仅做转发。"""

from lib.agent.recovery import (
    MAX_OUTPUT_CONTINUATION_TEXT,
    MAX_OUTPUT_TOKENS_PATTERNS,
    MAX_RETRIES,
    NON_RETRYABLE_ERROR_PATTERNS,
    PROMPT_TOO_LONG_PATTERNS,
    RECOVERABLE_ERROR_PATTERNS,
    RETRY_BACKOFF_BASE,
    RETRYABLE_STATUS_CODES,
    classify_error,
    classify_error_text,
    classify_exception,
    continue_after_stream_interrupt,
    detect_interrupt,
    drain_invoke_interrupts,
    force_compact_session,
    format_execution_error,
    recover_after_max_output_tokens,
    recover_after_prompt_too_long,
    recover_after_recoverable,
    resolve_interrupt,
    resume_after_interrupt,
    retry_delay,
)

__all__ = [
    "MAX_OUTPUT_CONTINUATION_TEXT",
    "MAX_OUTPUT_TOKENS_PATTERNS",
    "MAX_RETRIES",
    "NON_RETRYABLE_ERROR_PATTERNS",
    "PROMPT_TOO_LONG_PATTERNS",
    "RECOVERABLE_ERROR_PATTERNS",
    "RETRY_BACKOFF_BASE",
    "RETRYABLE_STATUS_CODES",
    "classify_error",
    "classify_error_text",
    "classify_exception",
    "continue_after_stream_interrupt",
    "detect_interrupt",
    "drain_invoke_interrupts",
    "force_compact_session",
    "format_execution_error",
    "recover_after_max_output_tokens",
    "recover_after_prompt_too_long",
    "recover_after_recoverable",
    "resolve_interrupt",
    "resume_after_interrupt",
    "retry_delay",
]


def __getattr__(name: str):
    """未显式列出的属性一律转发到新模块，保证旧引用继续生效。"""
    import importlib

    return getattr(importlib.import_module("lib.agent.recovery"), name)
