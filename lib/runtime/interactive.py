"""Interactive runtime loop for SAYACODE."""

from __future__ import annotations

import atexit
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from ..commands import build_default_command_router
from ..core.hooks import hook_runtime_session, trigger_hook_event
from ..core.permissions import permission_runtime_session
from ..custom_commands import list_custom_commands, render_custom_command
from ..i18n import tr
from ..theme import (
    SayacodeColors,
    console,
    print_agent_message,
    print_error,
    print_info,
    print_user_message,
    render_streaming_agent_message,
    short_prompt,
)
from .context import RuntimeContext
from .session_store import persist_local_state


EnsureContextWindow = Callable[[str, str, dict], bool]

_HISTORY_FILE: Optional[Path] = None
_history_loaded = False


def _setup_readline_history(history_path: Path) -> None:
    global _history_loaded
    if _history_loaded:
        return
    try:
        import readline
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_str = str(history_path)
        if history_path.exists():
            readline.read_history_file(history_str)
        atexit.register(readline.write_history_file, history_str)
        _history_loaded = True
    except (ImportError, OSError):
        pass


def _append_history(line: str, history_path: Optional[Path] = None) -> None:
    path = history_path or _HISTORY_FILE
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")
    except OSError:
        pass


def _resolve_history_path() -> Path:
    from ..core.paths import SayacodePaths
    return SayacodePaths.resolve().home / "history"


@dataclass
class InteractiveLoop:
    """Own command routing and prompt execution for one runtime."""

    agent: Any
    state: Any
    user_config: Optional[Any] = None
    mcp_service: Optional[Any] = None
    builtin_commands: Iterable[str] = field(default_factory=tuple)
    ensure_context_window: Optional[EnsureContextWindow] = None
    router: Any = None

    def __post_init__(self) -> None:
        global _HISTORY_FILE
        if _HISTORY_FILE is None:
            _HISTORY_FILE = _resolve_history_path()
        _setup_readline_history(_HISTORY_FILE)
        if self.router is None:
            self.router = build_default_command_router()

    def run(self) -> None:
        runtime = self._runtime()
        if self.mcp_service is not None and hasattr(self.mcp_service, "attach_runtime"):
            self.mcp_service.attach_runtime(runtime)

        with self._runtime_scope(runtime):
            if self.state.restored_session and (self.state.session.get_message_count() or len(self.state.memory)):
                print_info(
                    tr(
                        "interactive.restored",
                        messages=self.state.session.get_message_count(),
                        interactions=len(self.state.memory),
                    )
                )
                console.print()

            start_block = trigger_hook_event(
                "SessionStart",
                {"session_id": self.state.session.session_id, "workspace": str(self.state.workspace)},
            )
            if start_block:
                print_error(start_block)

            try:
                while True:
                    try:
                        user_input = self._read_user_input()
                        if not user_input.strip():
                            continue

                        _append_history(user_input, _HISTORY_FILE)

                        if self._prompt_blocked(user_input):
                            continue

                        result = self.dispatch_command(user_input)
                        if result is None:
                            self._run_agent_turn(user_input)
                        elif result is False:
                            break
                        else:
                            persist_local_state(self.state, self.user_config)

                    except KeyboardInterrupt:
                        console.print()
                        break
                    except EOFError:
                        print_info(tr("interactive.input_closed"))
                        break
                    except Exception as exc:
                        print_error(tr("errors.generic", error=str(exc)))
            finally:
                trigger_hook_event(
                    "SessionEnd",
                    {"session_id": self.state.session.session_id, "workspace": str(self.state.workspace)},
                )
                if hasattr(self.agent, "close"):
                    self.agent.close()

    def dispatch_command(self, command: str) -> Optional[bool]:
        """Dispatch one slash command through the runtime command router."""
        runtime = self._runtime()
        runtime.sync_from_app_state(self.state)
        runtime.attach_agent(self.agent)
        runtime.mcp = self.mcp_service
        runtime.config_stores["user"] = self.user_config
        if self.ensure_context_window is not None:
            runtime.config_stores["ensure_context_window"] = self.ensure_context_window
        runtime.config_stores["command_router"] = self.router
        with self._runtime_scope(runtime):
            return self.router.dispatch(command, runtime)

    def _runtime(self) -> RuntimeContext:
        runtime = getattr(self.state, "runtime_context", None)
        if runtime is None:
            runtime = RuntimeContext.from_app_state(
                self.state,
                model_name=self.state.model_config.get("model_name"),
                mcp=self.mcp_service,
                config_stores={"user": self.user_config},
            )
            self.state.runtime_context = runtime
        return runtime

    @contextmanager
    def _runtime_scope(self, runtime: RuntimeContext):
        with ExitStack() as stack:
            if runtime.permissions is not None:
                stack.enter_context(permission_runtime_session(runtime.permissions))
            if runtime.hooks is not None:
                stack.enter_context(hook_runtime_session(runtime.hooks))
            yield

    def _read_user_input(self) -> str:
        ctx = getattr(self.agent.session, "usage_ratio", None) if hasattr(self.agent, "session") else None
        return console.input(short_prompt(self.state.workspace.name, context_usage=ctx))

    def _prompt_blocked(self, user_input: str) -> bool:
        prompt_block = trigger_hook_event(
            "UserPromptSubmit",
            {
                "session_id": self.state.session.session_id,
                "input": user_input,
                "is_slash_command": user_input.strip().startswith("/"),
            },
        )
        if prompt_block:
            print_error(prompt_block)
            return True
        return False

    def _run_agent_turn(self, user_input: str) -> None:
        resolved_command, expanded_prompt = render_custom_command(user_input, self.state.workspace)
        if user_input.strip().startswith("/"):
            if not resolved_command or not expanded_prompt:
                print_error(tr("interactive.unknown_slash", command=user_input.strip()))
                suggestions = self._suggest_command_invocations(user_input)
                if suggestions:
                    print_info(
                        tr(
                            "interactive.unknown_slash_suggestion",
                            commands=", ".join(suggestions),
                        )
                    )
                print_info(tr("interactive.unknown_slash_hint"))
                return
            print_info(
                tr(
                    "interactive.custom_command",
                    command=resolved_command.primary_invocation,
                    source=resolved_command.source_label,
                )
            )

        print_user_message(user_input)
        agent_input = expanded_prompt if expanded_prompt else user_input

        if self.state.stream_output:
            response = render_streaming_agent_message(
                self.agent.stream_run(agent_input),
                thinking_message=tr("thinking"),
            )
        else:
            with console.status(
                f"[{SayacodeColors.INFO}]{tr('thinking')}[/]",
                spinner="dots",
            ):
                response = self.agent.run(agent_input)
            print_agent_message(response)

        persist_local_state(self.state, self.user_config)

    def _suggest_command_invocations(self, raw_command: str) -> list[str]:
        normalized = raw_command.strip().split()[0] if raw_command.strip() else ""
        known = self._known_command_invocations()
        return get_close_matches(normalized, known, n=5, cutoff=0.55)

    def _known_command_invocations(self) -> list[str]:
        commands = set(str(command) for command in self.builtin_commands)
        try:
            for command in list_custom_commands(Path(self.state.workspace)):
                commands.update(command.invocations)
                if command.qualified_invocation:
                    commands.add(command.qualified_invocation)
        except Exception:
            pass
        return sorted(commands)


__all__ = ["InteractiveLoop"]
