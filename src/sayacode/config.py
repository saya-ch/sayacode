"""Configuration for the LangChain/LangGraph runtime.

The repository stores product preferences only. Conversation state and task
progress belong to LangGraph checkpoints and its store.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Profile:
    """One model configuration and its optional built-in middleware settings."""

    name: str
    model: str
    provider: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    config_fields: dict[str, Any] = field(default_factory=dict)
    summary_model: str | None = None
    summary_trigger_tokens: int | None = 64_000
    summary_keep_messages: int = 12
    context_edit_trigger: int | None = None
    model_retries: int = 2
    tool_retries: int = 2
    max_model_calls: int | None = 20
    max_tool_calls: int | None = 50
    file_search: bool = True
    tool_selector_max_tools: int | None = 12
    provider_tool_search_tools: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        self.model = self.model.strip()
        if not self.name or not self.model:
            raise ValueError("profile name and model are required")
        if self.summary_trigger_tokens is not None and self.summary_trigger_tokens <= 0:
            raise ValueError("summary_trigger_tokens must be positive")
        if self.summary_keep_messages < 0:
            raise ValueError("summary_keep_messages cannot be negative")
        if self.context_edit_trigger is not None and self.context_edit_trigger <= 0:
            raise ValueError("context_edit_trigger must be positive")
        if self.model_retries < 0 or self.tool_retries < 0:
            raise ValueError("retry counts cannot be negative")
        if self.max_model_calls is not None and self.max_model_calls <= 0:
            raise ValueError("max_model_calls must be positive")
        if self.max_tool_calls is not None and self.max_tool_calls <= 0:
            raise ValueError("max_tool_calls must be positive")
        if self.tool_selector_max_tools is not None and self.tool_selector_max_tools <= 0:
            raise ValueError("tool_selector_max_tools must be positive")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Profile:
        fields = cls.__dataclass_fields__
        return cls(**{key: value for key, value in data.items() if key in fields})


@dataclass(slots=True)
class Config:
    """Saved settings for one SAYACODE installation."""

    default_profile: str | None = None
    profiles: dict[str, Profile] = field(default_factory=dict)
    preferences: dict[str, str] = field(default_factory=dict)
    user_policy: dict[str, str] = field(default_factory=dict)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    trusted_mcp_projects: list[str] = field(default_factory=list)

    def profile(self, name: str | None = None) -> Profile:
        chosen = name or self.default_profile
        if not chosen or chosen not in self.profiles:
            raise KeyError(f"unknown model profile: {chosen!r}")
        return self.profiles[chosen]

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_profile": self.default_profile,
            "profiles": {name: asdict(profile) for name, profile in self.profiles.items()},
            "preferences": dict(self.preferences),
            "user_policy": dict(self.user_policy),
            "mcp_servers": dict(self.mcp_servers),
            "trusted_mcp_projects": list(self.trusted_mcp_projects),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        raw_profiles = data.get("profiles", {})
        if not isinstance(raw_profiles, dict):
            raise ValueError("profiles must be an object")
        profiles: dict[str, Profile] = {}
        for name, raw in raw_profiles.items():
            if not isinstance(raw, dict):
                raise ValueError(f"profile {name!r} must be an object")
            profile = Profile.from_dict({**raw, "name": str(name)})
            profiles[profile.name] = profile
        default = data.get("default_profile")
        if default is not None and default not in profiles:
            raise ValueError(f"default profile {default!r} does not exist")
        preferences = data.get("preferences", {})
        user_policy = data.get("user_policy", {})
        mcp_servers = data.get("mcp_servers", {})
        trusted = data.get("trusted_mcp_projects", [])
        if not all(isinstance(value, dict) for value in (preferences, user_policy, mcp_servers)):
            raise ValueError("preferences, user_policy, and mcp_servers must be objects")
        if not isinstance(trusted, list) or not all(isinstance(value, str) for value in trusted):
            raise ValueError("trusted_mcp_projects must be a list of paths")
        return cls(
            default_profile=default,
            profiles=profiles,
            preferences={str(key): str(value) for key, value in preferences.items()},
            user_policy={str(key): str(value) for key, value in user_policy.items()},
            mcp_servers={str(key): dict(value) for key, value in mcp_servers.items()},
            trusted_mcp_projects=list(trusted),
        )


class ConfigRepository:
    """Asynchronous, atomic JSON persistence for installation settings."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / "config.json"

    async def load(self) -> Config:
        def read() -> Config:
            if not self.path.exists():
                return Config()
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("config.json must contain an object")
            return Config.from_dict(data)

        return await asyncio.to_thread(read)

    async def save(self, config: Config) -> None:
        data = json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n"

        def write() -> None:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.root,
                    prefix=".config-",
                    suffix=".tmp",
                    delete=False,
                ) as handle:
                    temporary = handle.name
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary is not None and os.path.exists(temporary):
                    os.unlink(temporary)

        await asyncio.to_thread(write)


__all__ = ["Config", "ConfigRepository", "Profile"]
