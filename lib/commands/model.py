"""Model and config slash commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from ..api_config import APIConfigManager, APIConfigWizardCLI
from ..core.modes import agent_mode_label
from ..i18n import on_off, tr
from ..prompts import prompt_style_label
from ..runtime import RuntimeContext
from ..runtime.model_profiles import extract_context_window_from_config, switch_active_profile
from ..theme import console, print_error, print_success, print_summary_card
from .base import CommandContext, CommandHandler


@dataclass
class ModelCommandHandler(CommandHandler):
    """Show, add, test, and switch model profiles."""

    name: str = "model"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        state = runtime.app_state
        agent = runtime.agent
        if state is None or agent is None:
            print_error(tr("runtime.state_unavailable"))
            return True

        parts = command.raw.strip().split()
        if len(parts) == 1:
            print_model_dashboard(state)
            print_summary_card(
                tr("model.commands_title"),
                {
                    "/model list": tr("model.commands.list"),
                    "/model use <name>": tr("model.commands.use"),
                    "/model add": tr("model.commands.add"),
                },
                footer=tr("model.commands_footer"),
            )
            console.print()
            return True

        action = parts[1].lower()

        if action == "list":
            return run_config_command(runtime, ["list"], switch_after_success=False)

        if action in {"add", "new"}:
            return run_config_command(runtime, ["add", *parts[2:]], switch_after_success=True)

        if action in {"use", "set", "switch"}:
            if len(parts) < 3:
                print_error(tr("model.usage_use"))
                return True

            api_manager = APIConfigManager()
            if not api_manager.set_current(parts[2]):
                print_error(tr("model.not_found", name=parts[2]))
                return True

            result = switch_runtime_profile(runtime, api_manager=api_manager)
            if result.ok:
                print_success(tr("model.profile_switched", name=result.profile_name))
            else:
                _print_profile_switch_error(result.error, result.profile_name)
            return True

        if action == "test":
            if len(parts) < 3:
                print_error(tr("model.usage_test"))
                return True
            return run_config_command(runtime, ["test", parts[2]], switch_after_success=False)

        if action in {"show", "current"}:
            print_model_dashboard(state)
            return True

        print_error(tr("model.unknown_command"))
        return True


@dataclass
class ConfigCommandHandler(CommandHandler):
    """Run the API configuration wizard command surface."""

    name: str = "config"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        args = command.raw.strip().split()[1:]
        return run_config_command(runtime, args, switch_after_success=True)


def run_config_command(runtime: RuntimeContext, args: list[str], *, switch_after_success: bool) -> bool:
    agent = runtime.agent
    if agent is None or runtime.app_state is None:
        print_error(tr("runtime.state_unavailable"))
        return True

    api_manager = APIConfigManager()
    wizard_cli = APIConfigWizardCLI(console, manager=api_manager)
    exit_code = wizard_cli.run(args)

    if exit_code == 0 and switch_after_success:
        result = switch_runtime_profile(runtime, api_manager=api_manager)
        if not result.ok:
            _print_profile_switch_error(result.error, result.profile_name)

    return True


def switch_runtime_profile(runtime: RuntimeContext, *, api_manager: APIConfigManager | None = None):
    ensure_context_window = runtime.config_stores.get("ensure_context_window")
    return switch_active_profile(
        runtime.agent,
        runtime.app_state,
        api_manager=api_manager,
        ensure_context_window=ensure_context_window,
    )


def print_model_dashboard(state: Any) -> None:
    """Print the current model profile summary."""
    model_rows: Dict[str, str] = {
        tr("model.profile"): state.active_profile or tr("model.session_override"),
        tr("model.protocol"): state.model_type,
        tr("workspace.model"): state.model_config.get("model_name", "unknown"),
        tr("model.base_url"): state.model_config.get("base_url", tr("common.not_set")),
        tr("model.context_window"): _format_context_window_value(
            extract_context_window_from_config(state.model_config)
        ),
        tr("style.active"): prompt_style_label(state.prompt_style),
        tr("workspace.mode_label"): f"{state.agent_mode} ({agent_mode_label(state.agent_mode)})",
        tr("model.streaming"): on_off(state.stream_output),
        tr("model.danger_confirm"): on_off(state.confirm_dangerous),
    }
    print_summary_card(
        tr("model.title"),
        model_rows,
        subtitle=tr("model.subtitle"),
        footer=tr("model.footer"),
    )
    console.print()


def _format_context_window_value(value: int | None) -> str:
    return f"{value:,} tokens" if value else tr("common.not_set")


def _print_profile_switch_error(error: str | None, profile_name: str | None) -> None:
    if error == "no_saved_profile":
        print_error(tr("model.no_saved_profile"))
    else:
        print_error(tr("model.profile_load_failed", name=profile_name or "", error=error or "unknown"))


__all__ = ["ConfigCommandHandler", "ModelCommandHandler", "print_model_dashboard"]
