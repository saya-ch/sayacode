from types import SimpleNamespace

from lib.agent import SAIAgent, _safe_token_count


class UsageRecorder:
    def __init__(self):
        self.records = []

    def _record_usage(self, usage):
        self.records.append(usage)


def _usage_agent():
    agent = object.__new__(SAIAgent)
    agent.model = UsageRecorder()
    agent._estimate_agent_usage = lambda result: None
    return agent


def test_safe_token_count_handles_provider_nulls_and_invalid_values():
    assert _safe_token_count(None) == 0
    assert _safe_token_count("12") == 12
    assert _safe_token_count("unknown") == 0
    assert _safe_token_count(-3) == 0


def test_agent_usage_accepts_nullable_and_string_counters():
    agent = _usage_agent()
    message = SimpleNamespace(
        usage_metadata={
            "input_tokens": None,
            "output_tokens": "4",
            "total_tokens": "4",
        },
    )

    agent._record_agent_usage({"messages": [message]})

    usage = agent.model.records[0]
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 4
    assert usage.total_tokens == 4


def test_stream_usage_ignores_malformed_counter_without_losing_valid_totals():
    agent = _usage_agent()
    message = SimpleNamespace(
        usage_metadata={
            "input_tokens": "not-a-number",
            "output_tokens": 7,
            "total_tokens": 7,
        },
    )

    agent._record_stream_usage({"agent": {"messages": [message]}})

    usage = agent.model.records[0]
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 7
    assert usage.total_tokens == 7
