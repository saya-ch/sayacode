"""Runtime launch model configuration services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from ..api_config import APIConfigManager
from ..models import parse_context_window
from .model_profiles import (
    extract_context_window_from_config,
    get_current_saved_profile,
    normalize_api_type,
    profile_requires_completion,
    profile_to_runtime_tuple,
    save_model_profile,
    store_context_window_in_config,
)


ConfigureModelCallback = Callable[..., tuple[str, str, Dict[str, Any]]]
EnsureContextWindowCallback = Callable[[str, str, Dict[str, Any]], int]


@dataclass(frozen=True)
class LaunchModelOverrides:
    """Model-related CLI overrides for one process launch."""

    model_type: Optional[str] = None
    model_name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    context_window: Optional[Any] = None

    @property
    def has_model_overrides(self) -> bool:
        return any(
            value is not None and value != ""
            for value in (self.model_type, self.model_name, self.base_url, self.api_key)
        )

    def parsed_context_window(self) -> Optional[int]:
        if self.context_window in (None, ""):
            return None
        parsed = parse_context_window(self.context_window)
        if not parsed:
            raise ValueError("Invalid model context window")
        return parsed


@dataclass(frozen=True)
class LaunchModelResult:
    """Resolved model configuration for startup."""

    model_type: str
    model_name: str
    model_config: Dict[str, Any]
    active_profile: Optional[str]

    def as_tuple(self) -> tuple[str, str, Dict[str, Any], Optional[str]]:
        return self.model_type, self.model_name, self.model_config, self.active_profile


@dataclass
class ModelLaunchResolver:
    """Resolve startup model/profile state without owning terminal I/O."""

    api_manager: APIConfigManager
    configure_model: ConfigureModelCallback
    ensure_context_window: EnsureContextWindowCallback
    interactive_input: bool
    on_profile_missing_credentials: Optional[Callable[[str], None]] = None
    on_saved_profile_summary: Optional[Callable[[str, str, str, Dict[str, Any]], None]] = None
    on_profile_saved: Optional[Callable[[str], None]] = None
    on_profile_not_saved: Optional[Callable[[], None]] = None

    def resolve(self, overrides: Optional[LaunchModelOverrides] = None) -> LaunchModelResult:
        overrides = overrides or LaunchModelOverrides()
        current_profile_name, current_profile = get_current_saved_profile(self.api_manager)
        cli_context_window = overrides.parsed_context_window()

        if overrides.has_model_overrides:
            current_profile_type = (
                normalize_api_type(current_profile.api_type)
                if current_profile
                else None
            )
            same_protocol_profile = bool(
                current_profile
                and (overrides.model_type is None or current_profile_type == overrides.model_type)
            )
            same_model_profile = bool(
                same_protocol_profile
                and current_profile
                and (overrides.model_name is None or overrides.model_name == current_profile.model_name)
            )
            model_type, model_name, model_config = self.configure_model(
                default_model_type=overrides.model_type or current_profile_type,
                default_model_name=overrides.model_name or (
                    current_profile.model_name if same_protocol_profile else None
                ),
                default_base_url=overrides.base_url or (
                    current_profile.base_url if same_protocol_profile else None
                ),
                default_api_key=overrides.api_key or (
                    current_profile.api_key if same_protocol_profile else None
                ),
                default_context_window=cli_context_window or (
                    current_profile.context_window if same_model_profile else None
                ),
                lock_model_type=overrides.model_type is not None,
            )
            return LaunchModelResult(model_type, model_name, model_config, None)

        if current_profile_name and current_profile:
            if profile_requires_completion(current_profile) and self.interactive_input:
                if self.on_profile_missing_credentials:
                    self.on_profile_missing_credentials(current_profile_name)
                model_type, model_name, model_config = self.configure_model(
                    default_model_type=normalize_api_type(current_profile.api_type),
                    default_model_name=current_profile.model_name,
                    default_base_url=current_profile.base_url,
                    default_api_key=current_profile.api_key,
                    default_context_window=cli_context_window or current_profile.context_window,
                    lock_model_type=True,
                )
                saved_name = save_model_profile(
                    self.api_manager,
                    model_type=model_type,
                    model_name=model_name,
                    model_config=model_config,
                    profile_name=current_profile_name,
                )
                return LaunchModelResult(
                    model_type,
                    model_name,
                    model_config,
                    saved_name or current_profile_name,
                )

            model_type, model_name, model_config, profile_name = profile_to_runtime_tuple(
                current_profile_name,
                current_profile,
            )
            if cli_context_window:
                store_context_window_in_config(model_config, cli_context_window)

            previous_context_window = extract_context_window_from_config(model_config)
            self.ensure_context_window(model_type, model_name, model_config)
            if not cli_context_window and previous_context_window is None:
                save_model_profile(
                    self.api_manager,
                    model_type=model_type,
                    model_name=model_name,
                    model_config=model_config,
                    profile_name=profile_name,
                )
            if self.on_saved_profile_summary:
                self.on_saved_profile_summary(profile_name, model_type, model_name, model_config)
            return LaunchModelResult(model_type, model_name, model_config, profile_name)

        model_type, model_name, model_config = self.configure_model(
            default_model_type=overrides.model_type,
            default_model_name=overrides.model_name,
            default_base_url=overrides.base_url,
            default_api_key=overrides.api_key,
            default_context_window=cli_context_window,
            lock_model_type=overrides.model_type is not None,
        )
        saved_name = save_model_profile(
            self.api_manager,
            model_type=model_type,
            model_name=model_name,
            model_config=model_config,
        )
        if saved_name:
            if self.on_profile_saved:
                self.on_profile_saved(saved_name)
        elif self.on_profile_not_saved:
            self.on_profile_not_saved()
        return LaunchModelResult(model_type, model_name, model_config, saved_name)


__all__ = [
    "LaunchModelOverrides",
    "LaunchModelResult",
    "ModelLaunchResolver",
]
