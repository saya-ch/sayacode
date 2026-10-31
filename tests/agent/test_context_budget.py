"""自动摘要预算与上下文编辑默认值契约。"""

from __future__ import annotations

from types import SimpleNamespace

from sayacode.agent.graph import summary_trigger_for
from sayacode.config import Profile


def profile(context: int, output: int, **overrides: object) -> Profile:
    """创建只关注上下文预算的测试模型配置。"""
    return Profile(
        name="budget",
        protocol="openai_chat_completions",
        base_url="https://unused.test/v1",
        api_key="test-key",
        model_id="test",
        context_length=context,
        max_output_tokens=output,
        file_search=False,
        tool_selector_max_tools=None,
        **overrides,
    )


def test_default_summary_uses_eighty_percent_when_output_budget_is_small() -> None:
    configured = profile(128_000, 8_000)
    assert summary_trigger_for(configured, SimpleNamespace(profile={})) == 102_400
    assert configured.context_edit_trigger is None
    assert configured.context_edit_keep == 3


def test_context_edit_keep_overrides_default() -> None:
    """工具结果清理保留条数可调：keep=8 比默认 3 保留更多近期工具结果。"""

    configured = profile(128_000, 8_000, context_edit_trigger=40_000, context_edit_keep=8)
    assert configured.context_edit_trigger == 40_000
    assert configured.context_edit_keep == 8


def test_large_output_budget_moves_summary_earlier() -> None:
    configured = profile(1_000_000, 256_000)
    assert summary_trigger_for(configured, SimpleNamespace(profile={})) == 694_000


def test_provider_input_limit_and_absolute_override_can_only_move_trigger_earlier() -> None:
    configured = profile(128_000, 8_000, summary_trigger_tokens=60_000)
    model = SimpleNamespace(profile={"max_input_tokens": 100_000})
    assert summary_trigger_for(configured, model) == 60_000


def test_both_summary_triggers_none_disables_summarization() -> None:
    configured = profile(
        128_000,
        8_000,
        summary_trigger_ratio=None,
        summary_trigger_tokens=None,
    )
    assert summary_trigger_for(configured, SimpleNamespace(profile={})) is None
    assert configured.context_edit_trigger is None


def test_192k_context_with_32k_output_reserves_output_and_margin() -> None:
    """192k 上下文、32k 输出预算时，摘要阈值给输出预算和估算误差留余量。

    计算：输入上限 160000，安全余量 min(9600, 16000) = 9600，
    触发点 150400，早于 0.8 比例的 153600。"""

    configured = profile(192_000, 32_000)
    assert summary_trigger_for(configured, SimpleNamespace(profile={})) == 150_400

