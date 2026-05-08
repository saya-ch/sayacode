"""Model profile services for SAYACODE runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
import os
from urllib.parse import urlparse

from ..api_config import APIConfig, APIConfigManager, APIType
from ..core.audit import append_audit_event
from ..models import parse_context_window
from ..models.provider_catalog import provider_defaults as catalog_provider_defaults
from ..models.registry import get_model_provider_registry
from .session_store import sync_session_model_runtime


CONTEXT_WINDOW_CONFIG_KEYS = (
    "context_window",
    "model_context_limit",
    "max_context_length",
    "max_context_len",
)

EnsureContextWindow = Callable[[str, str, Dict[str, Any]], None]


@dataclass
class ProfileSwitchResult:
    """Result for switching the active model profile."""

    ok: bool
    profile_name: Optional[str] = None
    changed: bool = False
    error: Optional[str] = None


def create_runtime_model(model_type: str, **kwargs: Any) -> Any:
    """Create a model through the single provider registry."""
    return get_model_provider_registry().create_model(model_type, **kwargs)


def normalize_api_type(api_type: Any) -> str:
    """Convert APIType or string to the runtime protocol value."""
    if isinstance(api_type, APIType):
        return api_type.value
    if hasattr(api_type, "value"):
        return str(api_type.value).lower().strip()
    return str(api_type or "").lower().strip()


def provider_defaults(model_type: Optional[str]) -> Dict[str, Any]:
    """Return provider defaults for a runtime model type."""
    return catalog_provider_defaults(model_type or "ollama")


def profile_requires_completion(config: APIConfig, env_getter: Callable[[str], Optional[str]] = os.environ.get) -> bool:
    """Return True when a saved profile still needs credentials before use."""
    model_type = normalize_api_type(config.api_type)
    defaults = provider_defaults(model_type)
    if not defaults.get("requires_api_key"):
        return False

    if str(config.api_key or "").replace("\ufeff", "").strip():
        return False

    env_name = defaults.get("api_key_env")
    return not (env_name and env_getter(str(env_name)))


def sanitize_base_url(base_url: Optional[str]) -> Optional[str]:
    """Return a valid HTTP(S) base URL or None."""
    if base_url is None:
        return None

    value = str(base_url).replace("\ufeff", "").strip()
    if not value or any(char in value for char in ("\r", "\n", "\t")):
        return None

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    return value


def extract_context_window_from_config(model_config: Dict[str, Any]) -> Optional[int]:
    """Read an explicitly configured context window."""
    for key in CONTEXT_WINDOW_CONFIG_KEYS:
        parsed = parse_context_window(model_config.get(key))
        if parsed:
            return parsed

    metadata = model_config.get("metadata")
    if isinstance(metadata, dict):
        for key in CONTEXT_WINDOW_CONFIG_KEYS:
            parsed = parse_context_window(metadata.get(key))
            if parsed:
                return parsed

    return None


def store_context_window_in_config(model_config: Dict[str, Any], context_window: int) -> int:
    """Normalize and store a context window in a runtime config."""
    parsed = parse_context_window(context_window)
    if not parsed:
        raise ValueError("Invalid model context window")
    model_config["context_window"] = parsed
    return parsed


def runtime_model_config_from_profile(config: APIConfig) -> Dict[str, Any]:
    """Convert a saved profile to runtime model kwargs."""
    model_config: Dict[str, Any] = {}

    if config.base_url:
        model_config["base_url"] = config.base_url
    if config.api_key:
        model_config["api_key"] = config.api_key
    if config.temperature is not None:
        model_config["temperature"] = config.temperature
    if config.max_tokens is not None:
        model_config["max_tokens"] = config.max_tokens
    if parse_context_window(getattr(config, "context_window", None)):
        model_config["context_window"] = config.context_window
    if config.timeout:
        model_config["timeout"] = config.timeout
    if config.max_retries:
        model_config["max_retries"] = config.max_retries
    if config.azure_api_version:
        model_config["azure_api_version"] = config.azure_api_version
    if config.azure_deployment:
        model_config["azure_deployment"] = config.azure_deployment
    if config.metadata:
        model_config["metadata"] = config.metadata

    return model_config


def profile_to_runtime_tuple(profile_name: str, config: APIConfig) -> tuple[str, str, Dict[str, Any], str]:
    """Restore runtime model information from a saved profile."""
    model_type = normalize_api_type(config.api_type)
    model_name = config.model_name or provider_defaults(model_type)["default_model_name"]
    model_config = runtime_model_config_from_profile(config)
    return model_type, model_name, model_config, profile_name


def runtime_to_api_config(model_type: str, model_name: str, model_config: Dict[str, Any]) -> APIConfig:
    """Convert runtime model kwargs to a saved API profile."""
    api_type = APIType.from_value(model_type) or APIType.GENERIC
    api_key = str(model_config.get("api_key") or "").strip()
    defaults = provider_defaults(model_type)
    env_name = defaults.get("api_key_env")
    if api_key and env_name and api_key == os.environ.get(env_name):
        api_key = ""

    return APIConfig(
        api_type=api_type,
        base_url=sanitize_base_url(model_config.get("base_url")) or defaults["default_base_url"],
        api_key=api_key,
        model_name=model_name,
        timeout=int(model_config.get("timeout", 60) or 60),
        max_retries=int(model_config.get("max_retries", 3) or 3),
        temperature=float(model_config.get("temperature", 0.2) or 0.2),
        max_tokens=model_config.get("max_tokens"),
        context_window=extract_context_window_from_config(model_config),
        metadata=model_config.get("metadata") or {},
        azure_api_version=model_config.get("azure_api_version"),
        azure_deployment=model_config.get("azure_deployment"),
    )


def build_profile_name(api_manager: APIConfigManager, model_type: str, model_name: str) -> str:
    """Build a stable, readable profile name."""
    import re

    base = f"{model_type}-{model_name or 'profile'}".lower()
    base = re.sub(r"[^a-z0-9._-]+", "-", base).strip("-") or model_type
    candidate = base
    counter = 2
    existing = set(api_manager.list_configs())

    while candidate in existing:
        candidate = f"{base}-{counter}"
        counter += 1

    return candidate


def save_model_profile(
    api_manager: APIConfigManager,
    model_type: str,
    model_name: str,
    model_config: Dict[str, Any],
    profile_name: Optional[str] = None,
) -> Optional[str]:
    """Save a runtime model config to the profile store."""
    profile_name = profile_name or build_profile_name(api_manager, model_type, model_name)
    config = runtime_to_api_config(model_type, model_name, model_config)

    if not api_manager.add_config(profile_name, config):
        return None

    api_manager.set_current(profile_name)
    append_audit_event(
        "model_profile",
        "save",
        allowed=True,
        details={"profile": profile_name, "model_type": model_type, "model_name": model_name},
    )
    return profile_name


def get_current_saved_profile(api_manager: APIConfigManager) -> tuple[Optional[str], Optional[APIConfig]]:
    """Read the active profile, falling back to the first saved profile."""
    current = api_manager.get_current_config()
    if current and api_manager.current_config_name:
        return api_manager.current_config_name, current

    names = api_manager.list_configs()
    if not names:
        return None, None

    fallback = names[0]
    api_manager.set_current(fallback)
    return fallback, api_manager.get_current_config()


def switch_active_profile(
    agent: Any,
    state: Any,
    *,
    api_manager: Optional[APIConfigManager] = None,
    ensure_context_window: Optional[EnsureContextWindow] = None,
) -> ProfileSwitchResult:
    """Apply the active saved profile to AppState, RuntimeContext, session, and Agent."""
    api_manager = api_manager or APIConfigManager()
    profile_name, config = get_current_saved_profile(api_manager)

    if not profile_name or not config:
        state.active_profile = None
        return ProfileSwitchResult(ok=False, error="no_saved_profile")

    model_type, model_name, model_config, _ = profile_to_runtime_tuple(profile_name, config)
    previous_context_window = extract_context_window_from_config(model_config)

    try:
        if ensure_context_window is not None:
            ensure_context_window(model_type, model_name, model_config)
        if previous_context_window is None and extract_context_window_from_config(model_config):
            save_model_profile(
                api_manager,
                model_type=model_type,
                model_name=model_name,
                model_config=model_config,
                profile_name=profile_name,
            )
    except Exception as exc:
        return ProfileSwitchResult(ok=False, profile_name=profile_name, error=str(exc))

    next_state_config = {"model_name": model_name, **model_config}

    if (
        state.active_profile == profile_name
        and state.model_type == model_type
        and state.model_config == next_state_config
    ):
        return ProfileSwitchResult(ok=True, profile_name=profile_name, changed=False)

    try:
        model = create_runtime_model(model_type, model_name=model_name, **model_config)
    except Exception as exc:
        return ProfileSwitchResult(ok=False, profile_name=profile_name, error=str(exc))

    state.model_type = model_type
    state.model_config = next_state_config
    state.active_profile = profile_name

    runtime_context = getattr(state, "runtime_context", None)
    if runtime_context is not None:
        runtime_context.model_type = model_type
        runtime_context.model_name = model_name
        runtime_context.model_config = dict(next_state_config)
        runtime_context.model = model
        runtime_context.session = state.session
        runtime_context.memory = state.memory
        runtime_context.attach_agent(agent)
        runtime_context.attach_tools(
            getattr(agent, "tools", []),
            registry=getattr(runtime_context, "tool_registry", None),
        )

    agent.model = model
    sync_session_model_runtime(state.session, model)
    if hasattr(agent, "session"):
        agent.session = state.session
        sync_session_model_runtime(agent.session, model)
    agent._create_agent()
    append_audit_event(
        "model_profile",
        "switch",
        workspace=getattr(state, "workspace", ""),
        allowed=True,
        details={"profile": profile_name, "model_type": model_type, "model_name": model_name},
    )

    return ProfileSwitchResult(ok=True, profile_name=profile_name, changed=True)


__all__ = [
    "CONTEXT_WINDOW_CONFIG_KEYS",
    "ProfileSwitchResult",
    "build_profile_name",
    "create_runtime_model",
    "extract_context_window_from_config",
    "get_current_saved_profile",
    "normalize_api_type",
    "profile_to_runtime_tuple",
    "profile_requires_completion",
    "provider_defaults",
    "runtime_model_config_from_profile",
    "runtime_to_api_config",
    "sanitize_base_url",
    "save_model_profile",
    "store_context_window_in_config",
    "switch_active_profile",
]
