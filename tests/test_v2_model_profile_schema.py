"""Model profiles select a wire protocol explicitly and reject legacy input."""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from sayacode.config import (
    SUPPORTED_MODEL_PROTOCOLS,
    Config,
    ConfigRepository,
    Profile,
)


def profile_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "name": "custom",
        "protocol": "openai_chat_completions",
        "base_url": "https://gateway.example.test/v1",
        "api_key": "test-only-key",
        "model_id": "coder-large",
        "context_length": 32_000,
        "max_output_tokens": 4_000,
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize("protocol", SUPPORTED_MODEL_PROTOCOLS)
def test_each_explicit_protocol_is_accepted_without_vendor(protocol: str) -> None:
    profile = Profile.from_dict(profile_data(protocol=protocol))
    assert profile.protocol == protocol
    assert profile.model_id == "coder-large"
    assert profile.tool_selector_max_tools is None
    assert not hasattr(profile, "provider")
    assert not hasattr(profile, "config_fields")


def test_keyless_local_endpoint_and_alias_are_supported() -> None:
    profile = Profile.from_dict(
        profile_data(
            name="Local coder",
            protocol="ollama_native_chat",
            base_url="http://127.0.0.1:11434",
            api_key=None,
        )
    )
    assert profile.name == "Local coder"
    assert profile.api_key is None


def test_tool_selector_remains_an_explicit_advanced_option() -> None:
    profile = Profile.from_dict(profile_data(tool_selector_max_tools=4))
    assert profile.tool_selector_max_tools == 4


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"protocol": "openai"}, "unsupported model protocol"),
        ({"protocol": "anthropic"}, "unsupported model protocol"),
        ({"base_url": ""}, "base_url"),
        ({"base_url": "file:///tmp/model"}, "base_url"),
        ({"base_url": "https://user:pass@host.test/v1"}, "base_url"),
        ({"base_url": "https://host.test/v1?key=secret"}, "base_url"),
        ({"api_key": ""}, "api_key"),
        ({"context_length": 0}, "context_length"),
        ({"context_length": True}, "context_length"),
        ({"max_output_tokens": 0}, "max_output_tokens"),
        ({"max_output_tokens": 32_001}, "max_output_tokens"),
    ],
)
def test_invalid_endpoint_capability_or_credentials_fail(overrides, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        Profile.from_dict(profile_data(**overrides))


def test_legacy_or_typo_fields_are_rejected_instead_of_ignored() -> None:
    with pytest.raises(ValueError, match="provider"):
        Profile.from_dict(profile_data(provider="openai"))
    with pytest.raises(ValueError, match="config_fields"):
        Profile.from_dict(profile_data(config_fields={"temperature": 0}))
    with pytest.raises(ValueError, match="model"):
        Profile.from_dict(profile_data(model="wrong-field"))
    with pytest.raises(ValueError, match="summary_model"):
        Profile.from_dict(profile_data(summary_model="other-provider-model"))
    with pytest.raises(ValueError, match="max_output_tokens"):
        incomplete = profile_data()
        del incomplete["max_output_tokens"]
        Profile.from_dict(incomplete)


async def test_config_repository_roundtrip_keeps_protocol_and_capabilities(tmp_path) -> None:
    profile = Profile.from_dict(profile_data())
    repository = ConfigRepository(tmp_path)
    await repository.save(Config(default_profile="custom", profiles={"custom": profile}))

    saved = json.loads(repository.path.read_text(encoding="utf-8"))
    assert saved["profiles"]["custom"]["protocol"] == "openai_chat_completions"
    assert saved["profiles"]["custom"]["context_length"] == 32_000
    assert saved["profiles"]["custom"]["max_output_tokens"] == 4_000
    assert "provider" not in saved["profiles"]["custom"]
    assert "model" not in saved["profiles"]["custom"]
    assert "config_fields" not in saved["profiles"]["custom"]
    assert asdict((await repository.load()).profile()) == asdict(profile)


def test_config_rejects_legacy_top_level_and_mismatched_alias() -> None:
    with pytest.raises(ValueError, match="unknown config fields"):
        Config.from_dict({"legacy_provider": "openai"})
    with pytest.raises(ValueError, match="does not match key"):
        Config.from_dict({"profiles": {"alias": profile_data(name="different")}})


async def test_invalid_saved_profile_reports_the_config_path(tmp_path) -> None:
    repository = ConfigRepository(tmp_path)
    repository.path.write_text(
        json.dumps({
            "default_profile": "old",
            "profiles": {"old": {"name": "old", "provider": "openai", "model": "x"}},
        }), encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Invalid model configuration") as failure:
        await repository.load()
    assert str(repository.path) in str(failure.value)
    assert "protocol profiles" in str(failure.value)
