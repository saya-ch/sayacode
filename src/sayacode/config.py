"""运行时的配置。

仓库只存产品偏好。会话状态和任务进度由图检查点和存储保管。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

SUPPORTED_MODEL_PROTOCOLS = (
    "openai_chat_completions",
    "openai_responses",
    "anthropic_messages",
    "gemini_generate_content",
    "ollama_native_chat",
)

TrustLevel = Literal["read_only", "ask", "jev", "full"]
TRUST_LEVELS = ("read_only", "ask", "jev", "full")


def normalize_trust(value: str | None) -> TrustLevel:
    """规范化用户配置中的四档信任名称。传入原文或空，返回四档之一。空按询问处理，大小写横杠下划线和中文别名都认，未知会抛错。"""
    chosen = str(value or "ask").strip().lower().replace("-", "_")
    chosen = {
        "只读": "read_only",
        "询问": "ask",
        "jev自动审理": "jev",
        "jev_自动审理": "jev",
        "完全信任": "full",
    }.get(chosen, chosen)
    if chosen not in TRUST_LEVELS:
        raise ValueError(f"Unknown trust level: {value}")
    return cast(TrustLevel, chosen)


@dataclass(slots=True)
class Profile:
    """一个模型接入点，按传输协议选择而不按厂商。存地址密钥和额度上限，构造时全量校验，不合法直接抛错。"""

    name: str
    protocol: str
    base_url: str
    api_key: str | None
    model_id: str
    context_length: int
    max_output_tokens: int
    summary_trigger_ratio: float | None = 0.8
    summary_trigger_tokens: int | None = None
    summary_keep_messages: int = 12
    context_edit_trigger: int | None = None
    model_retries: int = 2
    tool_retries: int = 2
    file_search: bool = True
    tool_selector_max_tools: int | None = None
    native_tool_search_tools: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str)
            for value in (self.name, self.protocol, self.base_url, self.model_id)
        ):
            raise ValueError("profile name, protocol, base_url, and model_id must be strings")
        self.name = self.name.strip()
        self.model_id = self.model_id.strip()
        self.base_url = self.base_url.strip()
        if not self.name or not self.model_id:
            raise ValueError("profile name and model_id are required")
        if self.protocol not in SUPPORTED_MODEL_PROTOCOLS:
            raise ValueError(
                f"unsupported model protocol {self.protocol!r}; "
                f"choose one of {', '.join(SUPPORTED_MODEL_PROTOCOLS)}"
            )
        try:
            parsed_url = urlsplit(self.base_url)
            valid_url = (
                parsed_url.scheme in {"http", "https"}
                and bool(parsed_url.hostname)
                and parsed_url.username is None
                and parsed_url.password is None
                and not parsed_url.query
                and not parsed_url.fragment
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError(
                "base_url must be an http(s) URL without credentials, query, or fragment"
            )
        if self.api_key is not None and (
            not isinstance(self.api_key, str) or not self.api_key.strip()
        ):
            raise ValueError("api_key must be a nonempty string or null")
        if self.api_key is not None and self.api_key.casefold().startswith("env:"):
            raise ValueError(
                "api_key must be entered directly; environment references are unsupported"
            )
        if (
            isinstance(self.context_length, bool)
            or not isinstance(self.context_length, int)
            or self.context_length <= 0
        ):
            raise ValueError("context_length must be a positive integer")
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or self.max_output_tokens <= 0
            or self.max_output_tokens > self.context_length
        ):
            raise ValueError("max_output_tokens must be positive and at most context_length")
        if self.summary_trigger_tokens is not None and self.summary_trigger_tokens <= 0:
            raise ValueError("summary_trigger_tokens must be positive")
        if self.summary_trigger_ratio is not None and not 0 < self.summary_trigger_ratio < 1:
            raise ValueError("summary_trigger_ratio must be between 0 and 1")
        if self.summary_keep_messages < 0:
            raise ValueError("summary_keep_messages cannot be negative")
        if self.context_edit_trigger is not None and self.context_edit_trigger <= 0:
            raise ValueError("context_edit_trigger must be positive")
        if self.model_retries < 0 or self.tool_retries < 0:
            raise ValueError("retry counts cannot be negative")
        if self.tool_selector_max_tools is not None and self.tool_selector_max_tools <= 0:
            raise ValueError("tool_selector_max_tools must be positive")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Profile:
        """从字典建模型接入点。传入原始字典，返回校验过的对象。多字段少字段都抛错，名字以传入为准。"""
        fields = cls.__dataclass_fields__
        unknown = set(data) - set(fields)
        if unknown:
            raise ValueError(f"unknown model profile fields: {', '.join(sorted(unknown))}")
        required = {
            "name",
            "protocol",
            "base_url",
            "api_key",
            "model_id",
            "context_length",
            "max_output_tokens",
        }
        missing = required - set(data)
        if missing:
            raise ValueError(f"missing model profile fields: {', '.join(sorted(missing))}")
        return cls(**data)


@dataclass(slots=True)
class JevConfig:
    """Jev 工具调用审理端点，与聊天模型配置相互独立。"""

    base_url: str = "https://api.typesafe.ai"
    api_key: str = ""
    model_id: str = "jev-1.13.0"
    timeout_seconds: float = 3.0
    max_retries: int = 1

    def __post_init__(self) -> None:
        self.base_url = str(self.base_url).strip().rstrip("/")
        self.api_key = str(self.api_key).strip()
        self.model_id = str(self.model_id).strip()
        try:
            parsed_url = urlsplit(self.base_url)
            valid_url = (
                parsed_url.scheme in {"http", "https"}
                and bool(parsed_url.hostname)
                and parsed_url.username is None
                and parsed_url.password is None
                and not parsed_url.query
                and not parsed_url.fragment
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError(
                "Jev base_url must be an http(s) URL without credentials, query, or fragment"
            )
        if not self.api_key:
            raise ValueError("Jev api_key is required")
        if self.api_key.casefold().startswith("env:"):
            raise ValueError(
                "Jev api_key must be entered directly; environment references are unsupported"
            )
        if not self.model_id:
            raise ValueError("Jev model_id is required")
        if isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise ValueError("Jev timeout_seconds must be positive")
        if isinstance(self.max_retries, bool) or self.max_retries < 0:
            raise ValueError("Jev max_retries cannot be negative")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JevConfig:
        """从严格字段字典恢复 Jev 配置。"""
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown Jev config fields: {', '.join(sorted(unknown))}")
        return cls(**data)


@dataclass(slots=True)
class Config:
    """单台机器安装的已保存设置。只存产品偏好和模型接入点，会话和任务不在这里。"""

    default_profile: str | None = None
    default_trust: str = "ask"
    profiles: dict[str, Profile] = field(default_factory=dict)
    jev: JevConfig | None = None
    preferences: dict[str, str] = field(default_factory=dict)
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    trusted_mcp_projects: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 构造收尾把默认信任档规范化，后续各处可直接用四档之一。
        self.default_trust = normalize_trust(self.default_trust)

    def profile(self, name: str | None = None) -> Profile:
        """取出指定或默认的模型接入点。传入名字或空，返回接入点。名字未知会抛错，无默认时也要先配好。"""
        chosen = name or self.default_profile
        if not chosen or chosen not in self.profiles:
            raise KeyError(f"unknown model profile: {chosen!r}")
        return self.profiles[chosen]

    def to_dict(self) -> dict[str, Any]:
        """转成可存盘的字典。传入无，返回深拷贝风格的字典。改返回物不影响原对象。"""
        return {
            "default_profile": self.default_profile,
            "default_trust": self.default_trust,
            "profiles": {name: asdict(profile) for name, profile in self.profiles.items()},
            "jev": asdict(self.jev) if self.jev is not None else None,
            "preferences": dict(self.preferences),
            "mcp_servers": dict(self.mcp_servers),
            "trusted_mcp_projects": list(self.trusted_mcp_projects),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        """从存盘字典恢复配置。传入原始字典，返回校验过的配置。未知字段多余字段和旧偏好都会抛错，默认接入点必须已存在。"""
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown config fields: {', '.join(sorted(unknown))}")
        raw_profiles = data.get("profiles", {})
        if not isinstance(raw_profiles, dict):
            raise ValueError("profiles must be an object")
        profiles: dict[str, Profile] = {}
        for name, raw in raw_profiles.items():
            if not isinstance(raw, dict):
                raise ValueError(f"profile {name!r} must be an object")
            if "name" in raw and raw["name"] != name:
                raise ValueError(f"profile name {raw['name']!r} does not match key {name!r}")
            profile = Profile.from_dict({**raw, "name": str(name)})
            if profile.name in profiles:
                raise ValueError(f"duplicate profile name {profile.name!r}")
            profiles[profile.name] = profile
        raw_jev = data.get("jev")
        if raw_jev is not None and not isinstance(raw_jev, dict):
            raise ValueError("jev must be an object or null")
        jev = JevConfig.from_dict(raw_jev) if isinstance(raw_jev, dict) else None
        default = data.get("default_profile")
        if default is not None and default not in profiles:
            raise ValueError(f"default profile {default!r} does not exist")
        preferences = data.get("preferences", {})
        mcp_servers = data.get("mcp_servers", {})
        trusted = data.get("trusted_mcp_projects", [])
        if not all(isinstance(value, dict) for value in (preferences, mcp_servers)):
            raise ValueError("preferences and mcp_servers must be objects")
        if "mode" in preferences:
            raise ValueError("old mode preference is unsupported; use default_trust")
        if not isinstance(trusted, list) or not all(isinstance(value, str) for value in trusted):
            raise ValueError("trusted_mcp_projects must be a list of paths")
        return cls(
            default_profile=default,
            default_trust=normalize_trust(data.get("default_trust")),
            profiles=profiles,
            jev=jev,
            preferences={str(key): str(value) for key, value in preferences.items()},
            mcp_servers={str(key): dict(value) for key, value in mcp_servers.items()},
            trusted_mcp_projects=list(trusted),
        )


class ConfigRepository:
    """安装设置的异步原子 JSON 存取。读不到文件给默认配置，写用临时文件加换名保证不断电坏一半。"""

    def __init__(self, root: str | Path) -> None:
        """记住配置根目录。传入目录，返回无。只拼路径，不读写文件。"""
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / "config.json"

    async def load(self) -> Config:
        """读出配置。传入无，返回配置对象。文件不存在给默认，内容坏了会抛错并提示换成协议接入点写法。"""

        def read() -> Config:
            """读取配置文件内容。"""
            if not self.path.exists():
                return Config()
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("config.json must contain an object")
            try:
                return Config.from_dict(data)
            except ValueError as exc:
                raise ValueError(
                    f"Invalid model configuration at {self.path}: {exc}. "
                    "Replace old settings with protocol profiles and a default_trust value."
                ) from exc

        return await asyncio.to_thread(read)

    async def save(self, config: Config) -> None:
        """原子写回配置。传入配置对象，返回无。先写临时文件再换名，残留临时文件会顺手清掉。"""
        data = json.dumps(config.to_dict(), ensure_ascii=False, indent=2) + "\n"

        def write() -> None:
            """写回配置文件内容。"""
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


__all__ = [
    "Config",
    "ConfigRepository",
    "JevConfig",
    "Profile",
    "SUPPORTED_MODEL_PROTOCOLS",
    "TRUST_LEVELS",
    "TrustLevel",
    "normalize_trust",
]
