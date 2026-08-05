"""Non-interactive one-shot execution for scripts and CI."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
from typing import Any

from lib.api_config import APIConfigManager
from lib.cli.configure import resolve_launch_model_config
from lib.cli.permissions import configure_permission_confirmation
from lib.runtime import persist_local_state
from lib.runtime.startup import StartupOptions, StartupService


def resolve_headless_prompt(raw_prompt: str, *, stdin: Any = None) -> str:
    """Resolve a literal prompt or read it from stdin when ``-`` is used."""
    if raw_prompt != "-":
        prompt = str(raw_prompt or "").strip()
    else:
        stream = stdin if stdin is not None else sys.stdin
        prompt = stream.read().strip()
    if not prompt:
        raise ValueError("Prompt must not be empty")
    return prompt


def _emit_payload(payload: dict[str, Any], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, default=str))
        return
    if payload.get("ok"):
        print(str(payload.get("response") or ""))
    else:
        print(f"Error: {payload.get('error') or 'unknown error'}", file=sys.stderr)


def run_headless(
    args: Any,
    user_config: Any,
    *,
    prompt_style: str,
    agent_mode: str,
) -> int:
    """Bootstrap one isolated CLI turn without interactive prompts or UI noise."""
    startup_result = None
    captured_stdout = io.StringIO()
    captured_stderr = io.StringIO()

    try:
        prompt = resolve_headless_prompt(args.prompt)
        workspace = (
            Path(args.workspace).expanduser().resolve()
            if getattr(args, "workspace", None)
            else Path.cwd().resolve()
        )
        if not workspace.is_dir():
            raise NotADirectoryError(f"Workspace does not exist or is not a directory: {workspace}")

        # Headless output is a protocol surface. Suppress startup cards, model
        # adapter diagnostics, and tool progress so stdout remains parseable.
        with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
            api_manager = APIConfigManager()
            model_type, model_name, model_config, active_profile = resolve_launch_model_config(
                args=args,
                user_config=user_config,
                api_manager=api_manager,
                interactive_input=False,
            )

            # An unattended invocation must never open a permission prompt.
            # Existing allow/deny policy still applies; "ask" decisions fail closed.
            configure_permission_confirmation(False)
            startup_result = StartupService(
                api_manager=api_manager,
                user_config=user_config,
            ).bootstrap(StartupOptions(
                workspace=workspace,
                model_type=model_type,
                model_name=model_name,
                model_config=model_config,
                active_profile=active_profile,
                prompt_style=prompt_style,
                agent_mode=agent_mode,
                stream_output=False,
                confirm_dangerous=False,
                requested_session_id=getattr(args, "session", None),
                create_new_session=bool(getattr(args, "new_session", False)),
            ))
            response = startup_result.agent.run(prompt)
            persist_local_state(startup_result.state, user_config)

        session = getattr(startup_result.state, "session", None)
        payload = {
            "ok": True,
            "response": str(response),
            "model_type": model_type,
            "model_name": model_name,
            "session_id": getattr(session, "session_id", None),
        }
        _emit_payload(payload, getattr(args, "output_format", "text"))
        return 0
    except Exception as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "error_type": exc.__class__.__name__,
        }
        _emit_payload(payload, getattr(args, "output_format", "text"))
        return 1
    finally:
        agent = getattr(startup_result, "agent", None)
        if agent is not None and hasattr(agent, "close"):
            try:
                with redirect_stdout(captured_stdout), redirect_stderr(captured_stderr):
                    agent.close()
            except Exception:
                # Cleanup is best-effort and must not corrupt the one-shot
                # stdout protocol after a result has already been emitted.
                pass


__all__ = ["resolve_headless_prompt", "run_headless"]
