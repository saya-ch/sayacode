"""Preference and settings slash commands."""

from __future__ import annotations

from dataclasses import dataclass

from ..i18n import (
    get_effective_language,
    get_language_preference,
    language_label,
    normalize_language,
    on_off,
    set_language,
    tr,
)
from ..core.paths import SayacodePaths
from ..prompts import list_prompt_styles, normalize_prompt_style, prompt_style_label
from ..runtime import RuntimeContext
from ..state import UserConfig
from ..theme import SayacodeColors, console, print_error, print_info, print_success, print_summary_card
from ..core.modes import agent_mode_label
from .base import CommandContext, CommandHandler


@dataclass
class PrefsCommandHandler(CommandHandler):
    name: str = "prefs"
    aliases: tuple[str, ...] = ("preferences",)

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        state = runtime.app_state
        prefs = runtime.config_stores.get("user") or UserConfig()
        prefs_display = prefs.to_display_dict()
        print_summary_card(
            tr("prefs.title"),
            {
                tr("prefs.config_path"): str(UserConfig.default_path()),
                tr("prefs.workspace"): prefs_display.get("workspace") or tr("common.not_set"),
                tr("prefs.active_profile"): state.active_profile or prefs_display.get("active_profile") or tr("common.not_set"),
                tr("prefs.profiles_store"): str(SayacodePaths.resolve(create=False).api_configs),
                tr("status.streaming"): on_off(prefs_display.get("stream_output", True)),
                tr("model.danger_confirm"): on_off(prefs_display.get("confirm_dangerous", True)),
                tr("prefs.startup_guide"): on_off(prefs_display.get("show_startup_guide", True)),
                tr("prefs.language"): language_label(prefs_display.get("language")),
                tr("prefs.style"): prompt_style_label(prefs_display.get("prompt_style")),
                tr("workspace.mode_label"): agent_mode_label(prefs_display.get("agent_mode")),
            },
            subtitle=tr("prefs.subtitle"),
            footer=tr("prefs.footer"),
        )
        return True


@dataclass
class LanguageCommandHandler(CommandHandler):
    name: str = "lang"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        user_config = runtime.config_stores.get("user")
        if not command.args:
            print_language_dashboard(user_config)
            return True

        raw_value = command.args.strip()
        requested = normalize_language(raw_value)
        lower_value = raw_value.lower()
        if requested == "auto" and lower_value not in {"auto", "system", "default"}:
            print_error(tr("lang.invalid", value=raw_value))
            print_info(tr("lang.usage"))
            return True
        if requested == "zh-CN" and lower_value not in {"zh", "zh-cn", "cn", "chinese"}:
            print_error(tr("lang.invalid", value=raw_value))
            print_info(tr("lang.usage"))
            return True
        if requested == "en" and lower_value not in {"en", "en-us", "english"}:
            print_error(tr("lang.invalid", value=raw_value))
            print_info(tr("lang.usage"))
            return True

        set_language(requested)
        if user_config is not None:
            user_config.language = requested
            user_config.save()
        print_success(tr("lang.updated", language=language_label(requested)))
        print_language_dashboard(user_config)
        return True


@dataclass
class StyleCommandHandler(CommandHandler):
    name: str = "style"
    aliases: tuple[str, ...] = ("persona",)

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        state = runtime.app_state
        agent = runtime.agent
        user_config = runtime.config_stores.get("user")

        if not command.args:
            print_prompt_style_dashboard(state, user_config)
            return True

        requested_raw = command.args.strip()
        requested = normalize_prompt_style(requested_raw, fallback=None)
        if not requested:
            print_error(tr("style.invalid", value=requested_raw))
            print_info(tr("style.usage"))
            return True

        state.prompt_style = requested
        if hasattr(agent, "set_prompt_style"):
            agent.set_prompt_style(requested)
        runtime.sync_from_app_state(state)
        runtime.attach_agent(agent)
        if user_config is not None:
            user_config.prompt_style = requested
            user_config.save()

        print_success(tr("style.updated", style=prompt_style_label(requested)))
        print_prompt_style_dashboard(state, user_config)
        return True


@dataclass
class SettingsCommandHandler(CommandHandler):
    name: str = "settings"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        state = runtime.app_state
        console.print()
        console.print(f"[{SayacodeColors.PRIMARY}]{tr('settings.title')}[/]")

        def toggle_confirm_dangerous() -> None:
            from ..cli.permissions import configure_permission_confirmation

            state.confirm_dangerous = not state.confirm_dangerous
            configure_permission_confirmation(state.confirm_dangerous)

        config_options = [
            ("1", tr("settings.streaming", value=on_off(state.stream_output)),
             lambda: setattr(state, "stream_output", not state.stream_output)),
            ("2", tr("settings.confirm_dangerous", value=on_off(state.confirm_dangerous)),
             toggle_confirm_dangerous),
            ("0", tr("settings.back"), None),
        ]

        for opt, desc, _ in config_options:
            console.print(f"  [{opt}] {desc}")

        choice = console.input("\n  > ").strip()

        for opt, _, func in config_options:
            if choice == opt and func:
                func()
                runtime.sync_from_app_state(state)
                print_success(tr("settings.updated"))
                break

        return True


def print_language_dashboard(user_config: UserConfig | None) -> None:
    preference = user_config.language if user_config else get_language_preference()
    print_summary_card(
        tr("lang.title"),
        {
            tr("lang.saved"): language_label(preference),
            tr("lang.effective"): language_label(get_effective_language()),
            tr("lang.supported"): "auto | zh-CN | en",
        },
        subtitle=tr("lang.subtitle"),
        footer=tr("lang.footer"),
    )
    console.print()


def print_prompt_style_dashboard(state: object, user_config: UserConfig | None) -> None:
    saved = normalize_prompt_style(user_config.prompt_style if user_config else state.prompt_style)
    active = normalize_prompt_style(state.prompt_style)
    print_summary_card(
        tr("style.title"),
        {
            tr("style.saved"): prompt_style_label(saved),
            tr("style.active"): prompt_style_label(active),
            tr("style.supported"): " | ".join(list_prompt_styles()),
        },
        subtitle=tr("style.subtitle"),
        footer=tr("style.footer"),
    )
    console.print()


__all__ = [
    "LanguageCommandHandler",
    "PrefsCommandHandler",
    "SettingsCommandHandler",
    "StyleCommandHandler",
    "print_language_dashboard",
    "print_prompt_style_dashboard",
]
