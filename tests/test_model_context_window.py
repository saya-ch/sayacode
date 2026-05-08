from typing import Dict, Iterator, List

from lib.api_config.api_config import APIConfig, APIType
from lib.core.session import SessionManager
from lib.models.base import BaseModel, ModelInfo, parse_context_window


class DummyModel(BaseModel):
    def _initialize_model(self):
        return None

    def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        return ""

    def chat_stream(self, messages: List[Dict[str, str]], **kwargs) -> Iterator[str]:
        return iter(())

    def get_model_info(self) -> ModelInfo:
        return ModelInfo(
            name=self.model_name,
            model_type="dummy",
            provider="test",
            supported_params=[],
        )


def test_parse_context_window_accepts_plain_numbers():
    assert parse_context_window(128000) == 128000
    assert parse_context_window("128000") == 128000
    assert parse_context_window("128,000") == 128000
    assert parse_context_window("128_000 tokens") == 128000


def test_parse_context_window_accepts_context_suffixes():
    assert parse_context_window("256k") == 262144
    assert parse_context_window("1M") == 1048576
    assert parse_context_window("1.5m") == 1572864


def test_parse_context_window_rejects_ambiguous_or_invalid_values():
    assert parse_context_window("") is None
    assert parse_context_window("abc") is None
    assert parse_context_window("0") is None
    assert parse_context_window("12.5") is None
    assert parse_context_window("101M") is None


def test_detect_context_window_does_not_fall_back_to_default():
    model = DummyModel("unknown-model")

    assert model.context_window == 0
    assert model.detect_context_window() is None


def test_detect_context_window_uses_manual_value():
    model = DummyModel("unknown-model", context_window="256k")

    assert model.detect_context_window() == 262144
    assert model.context_window == 262144
    assert model.context_window_source == "manual"


def test_session_unknown_context_does_not_compact_by_default():
    session = SessionManager()
    session.add_user_message("hello")
    compact_info = session.get_compact_info()

    assert session.model_context_limit == 0
    assert session.usage_ratio == 0.0
    assert session.needs_compact is False
    assert compact_info["context_limit_known"] is False
    assert "unknown" in repr(session)


def test_session_usage_ratio_does_not_count_output_reserve_as_used_context():
    session = SessionManager()

    session.set_context_limit(1048576)

    assert session.output_reserve == 157286
    assert session.usage_ratio < 0.01
    assert session.get_compact_info()["running_tokens"] < session.output_reserve


def test_session_compaction_budget_still_reserves_output_space():
    session = SessionManager()
    session.set_context_limit(1000)
    session._running_tokens = session.context_budget - session.output_reserve + 1

    assert session.usage_ratio < session.context_budget / session.model_context_limit
    assert session.needs_compact is True


def test_api_config_round_trips_context_window():
    config = APIConfig(
        api_type=APIType.OPENAI,
        base_url="https://api.openai.com/v1",
        model_name="gpt-test",
        context_window=262144,
    )

    loaded = APIConfig.from_dict(config.to_dict())

    assert loaded.context_window == 262144


def test_api_config_accepts_suffix_context_window_without_mutating_input():
    raw = {
        "api_type": "openai",
        "base_url": "https://api.openai.com/v1",
        "model_name": "gpt-test",
        "context_window": "1M",
    }

    loaded = APIConfig.from_dict(raw)

    assert loaded.context_window == 1048576
    assert raw["api_type"] == "openai"
