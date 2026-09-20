"""Credentials for protocol profiles are explicit, correctable, and never exposed."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import sayacode.runtime as runtime_module
from sayacode.app import SayacodeApp
from sayacode.config import Config, ConfigRepository, Profile
from sayacode.paths import AppPaths
from sayacode.runtime import AgentRuntime


def profile(*, api_key: str | None = None, protocol: str = "openai_chat_completions") -> Profile:
    return Profile(
        name="custom",
        protocol=protocol,
        base_url="https://gateway.example.invalid/v1",
        api_key=api_key,
        model_id="custom-model",
        context_length=8192,
        max_output_tokens=1024,
    )


@pytest.mark.parametrize("reference", ["env:API_KEY", "ENV:API_KEY", "env:", "env:HAS-DASH"])
def test_profile_rejects_unsupported_environment_reference(reference: str) -> None:
    with pytest.raises(ValueError, match="api_key"):
        profile(api_key=reference)


@pytest.mark.parametrize(
    "protocol",
    [
        "openai_chat_completions",
        "openai_responses",
        "anthropic_messages",
        "gemini_generate_content",
        "ollama_native_chat",
    ],
)
@pytest.mark.parametrize("api_key", [None, "key-for-test-only"])
def test_model_uses_only_profile_key_and_never_ambient_provider_key(
    protocol: str, api_key: str | None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ambient = "different-ambient-provider-key"
    monkeypatch.setenv("OPENAI_API_KEY", ambient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ambient)
    monkeypatch.setenv("GOOGLE_API_KEY", ambient)
    monkeypatch.setenv("OLLAMA_API_KEY", ambient)
    captured: dict[str, Any] = {}
    marker = object()

    def fake_init_chat_model(model_id: str, **options: Any) -> object:
        captured.update(options)
        return marker

    monkeypatch.setattr(runtime_module, "init_chat_model", fake_init_chat_model)
    actual = AgentRuntime._model_for(profile(api_key=api_key, protocol=protocol))
    assert actual is marker
    expected = api_key or "sayacode-keyless-endpoint"
    if protocol == "ollama_native_chat":
        supplied = captured["client_kwargs"]["headers"]["Authorization"]
        assert supplied == f"Bearer {expected}"
    else:
        assert captured["api_key"] == expected
    assert ambient not in json.dumps(captured)


async def make_app(tmp_path: Path, *, api_key: str | None = None) -> SayacodeApp:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = AppPaths.resolve(tmp_path / "state")
    config = Config(default_profile="custom", profiles={"custom": profile(api_key=api_key)})
    repository = ConfigRepository(paths.home)
    await repository.save(config)
    runtime = await AgentRuntime.open(paths.home)
    app = SayacodeApp(
        paths=paths,
        repository=repository,
        config=config,
        runtime=runtime,
        workspace=workspace,
        session_id="key-test",
        trust_level="ask",
        profile_name="custom",
    )
    await app.initialize()
    return app


async def test_model_set_key_updates_saved_profile_without_echoing_secret(tmp_path: Path) -> None:
    app = await make_app(tmp_path)
    try:
        key = "secret-used-only-for-this-test"
        result = await app.command(
            "model", {"action": "set_key", "name": "custom", "api_key": key}
        )
        assert key not in json.dumps(result)
        assert (await app.repository.load()).profile("custom").api_key == key
        replacement = "another-secret-used-only-for-this-test"
        result = await app.command(
            "model", {"action": "set_key", "name": "custom", "api_key": replacement}
        )
        assert replacement not in json.dumps(result)
        assert (await app.repository.load()).profile("custom").api_key == replacement
        cleared = await app.command(
            "model", {"action": "set_key", "name": "custom", "api_key": None}
        )
        assert cleared == {"updated": "custom"}
        assert (await app.repository.load()).profile("custom").api_key is None
        assert app._handles == {}
    finally:
        await app.aclose()


@pytest.mark.parametrize("api_key", [None, "old-secret-used-only-for-this-test"])
async def test_http_401_reports_key_repair_without_leaking_provider_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api_key: str | None,
) -> None:
    app = await make_app(tmp_path, api_key=api_key)
    exposed = "secret-fragment-from-provider-response"

    class Unauthorized(Exception):
        status_code = 401

        def __str__(self) -> str:
            return f"Authentication failed, provided key {exposed}"

    async def fail_before_graph(**_kwargs: Any) -> Any:
        raise Unauthorized()

    monkeypatch.setattr(app, "_get_handle", fail_before_graph)
    try:
        result = await app.run("hello")
        events = [event async for event in app.stream("hello")]
        assert result["status"] == "failed"
        assert events[-1]["type"] == "run.failed"
        for error in (result["error"], events[-1]["error"]):
            assert "/model key" in error
            assert exposed not in error
            assert "old-secret-used-only-for-this-test" not in error
        audit = await app.audit.list(thread_id="key-test")
        assert exposed not in json.dumps(audit)
    finally:
        await app.aclose()
