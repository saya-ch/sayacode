"""
CLI 主入口

包含 main() 主函数、用户配置加载/保存、偏好设置保存等。
"""

import sys
from pathlib import Path
from typing import Optional, List

from lib.theme import (
    console,
    print_logo,
    print_status,
    print_success,
    print_warning,
    print_error,
    print_info,
    confirm_action,
)
from lib.commands.workspace import print_workspace_dashboard

from lib.core.doctor import (
    has_failed_checks,
    render_doctor_json,
    render_doctor_report,
    run_doctor_checks,
    write_support_bundle,
)
from lib.core.modes import (
    list_agent_modes,
    normalize_agent_mode,
)
from lib.runtime import (
    persist_local_state,
)
from lib.runtime.interactive import InteractiveLoop
from lib.runtime.startup import StartupOptions, StartupService
from lib.state import UserConfig
from lib.api_config import APIConfigManager
from lib.prompts import normalize_prompt_style
from lib.i18n import (
    normalize_language,
    set_language,
    tr,
)
from lib.cli.parser import build_cli_parser, _prepare_cli_language, BUILTIN_COMMANDS
from lib.cli.permissions import configure_permission_confirmation, _supports_interactive_input

from lib.cli.workspace import resolve_launch_workspace, suggest_git_commit
from lib.cli.configure import resolve_launch_model_config, test_model_connection, _ensure_context_window_configured


def load_user_config() -> UserConfig:
    """加载本地用户配置；不存在时返回默认配置。"""
    return UserConfig.load() or UserConfig()


def save_user_config(user_config: UserConfig) -> Path:
    """保存本地用户配置。"""
    return user_config.save()


def _save_language_preference(user_config: Optional[UserConfig], language: str) -> None:
    """保存并立即应用语言偏好。"""
    normalized = normalize_language(language)
    set_language(normalized)
    if user_config is not None:
        user_config.language = normalized
        save_user_config(user_config)


def _save_prompt_style_preference(user_config: Optional[UserConfig], prompt_style: str) -> None:
    """保存 prompt style 偏好。"""
    normalized = normalize_prompt_style(prompt_style)
    if user_config is not None:
        user_config.prompt_style = normalized
        save_user_config(user_config)


def _save_agent_mode_preference(user_config: Optional[UserConfig], agent_mode: str) -> None:
    """保存 Agent 工作模式偏好。"""
    normalized = normalize_agent_mode(agent_mode)
    if user_config is not None:
        user_config.agent_mode = normalized or "build"
        save_user_config(user_config)


def _configure_stdio_encoding() -> None:
    """Keep localized CLI output printable on narrow Windows code pages."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (TypeError, ValueError, OSError):
            pass


def main(argv: Optional[List[str]] = None):
    """主函数"""
    _configure_stdio_encoding()
    user_config = load_user_config()
    _prepare_cli_language(argv, user_config)
    args = build_cli_parser().parse_args(argv)
    set_language(args.lang or user_config.language)

    if getattr(args, "doctor", False):
        workspace = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd().resolve()
        checks = run_doctor_checks(workspace)
        if getattr(args, "bundle", None):
            bundle_path = write_support_bundle(args.bundle, workspace=workspace, checks=checks)
            print(str(bundle_path))
            sys.exit(1 if has_failed_checks(checks) else 0)
        if getattr(args, "json", False):
            print(render_doctor_json(checks))
        else:
            console.print(render_doctor_report(checks))
        sys.exit(1 if has_failed_checks(checks) else 0)

    if getattr(args, "lang", None):
        _save_language_preference(user_config, args.lang)
    prompt_style = normalize_prompt_style(user_config.prompt_style)
    if getattr(args, "style", None):
        requested_style = normalize_prompt_style(args.style, fallback=None)
        if not requested_style:
            print_error(tr("style.invalid", value=args.style))
            print_info(tr("style.usage"))
            sys.exit(2)
        prompt_style = requested_style
        _save_prompt_style_preference(user_config, prompt_style)

    agent_mode = normalize_agent_mode(user_config.agent_mode)
    if getattr(args, "mode", None):
        requested_mode = normalize_agent_mode(args.mode, fallback=None)
        if not requested_mode:
            print_error(tr("mode.unknown", name=args.mode))
            print_info(tr("mode.supported_values", modes=" | ".join(list_agent_modes())))
            sys.exit(2)
        agent_mode = requested_mode
        _save_agent_mode_preference(user_config, agent_mode)

    api_manager = APIConfigManager()

    # 清屏并显示 Logo
    if not args.no_clear:
        console.clear()
    print_logo()

    # =========================================================================
    # 第一步：选择工作区
    # =========================================================================
    workspace = resolve_launch_workspace(args, user_config)
    print_success(tr("startup.workspace", workspace=workspace))
    if workspace.resolve() == Path.home().resolve():
        print_warning(tr("startup.home_warning"))

    # =========================================================================
    # 第二步：配置模型
    # =========================================================================
    model_type, model_name, model_config, active_profile = resolve_launch_model_config(
        args=args,
        user_config=user_config,
        api_manager=api_manager,
    )

    # 测试连接
    if not args.skip_connection_test:
        if not test_model_connection(model_type, model_name, model_config):
            if _supports_interactive_input():
                if not confirm_action(tr("startup.connection_continue")):
                    sys.exit(0)
            else:
                print_warning(tr("startup.connection_non_interactive"))

    console.print()

    # =========================================================================
    # 第三步：扩展入口（Claude-compatible file-based config）
    # =========================================================================
    # 第四步：创建运行时
    print_status(tr("startup.initializing"))
    configure_permission_confirmation(user_config.confirm_dangerous)

    try:
        startup = StartupService(
            api_manager=api_manager,
            user_config=user_config,
        )
        startup_result = startup.bootstrap(StartupOptions(
            workspace=workspace,
            model_type=model_type,
            model_name=model_name,
            model_config=model_config,
            active_profile=active_profile,
            prompt_style=prompt_style,
            agent_mode=agent_mode,
            stream_output=False if args.no_stream else user_config.stream_output,
            confirm_dangerous=user_config.confirm_dangerous,
            requested_session_id=args.session,
            create_new_session=args.new_session,
        ))
        state = startup_result.state
        agent = startup_result.agent
        mcp_manager = startup_result.mcp

        print_success(tr("startup.ready"))

    except Exception as e:
        print_error(tr("startup.failed", error=str(e)))
        sys.exit(1)

    console.print()

    # =========================================================================
    # 第五步：运行对话
    # =========================================================================
    if _supports_interactive_input():
        print_workspace_dashboard(state, mcp_manager)
        if user_config.show_startup_guide and not user_config.onboarding_completed:
            print_info(tr("startup.guide_hint"))
            user_config.onboarding_completed = True
            persist_local_state(state, user_config)

        InteractiveLoop(
            agent=agent,
            state=state,
            user_config=user_config,
            mcp_service=mcp_manager,
            builtin_commands=BUILTIN_COMMANDS,
            ensure_context_window=lambda model_type, model_name, model_config: _ensure_context_window_configured(
                model_type,
                model_name,
                model_config,
                interactive_input=_supports_interactive_input(),
            ),
        ).run()
    else:
        print_info(tr("startup.non_interactive"))
        persist_local_state(state, user_config)

    # =========================================================================
    # 第六步：退出前检查
    # =========================================================================
    persist_local_state(state, user_config)

    console.print()
    suggest_git_commit(workspace)
    if hasattr(agent, "close"):
        agent.close()

    # 打印告别
    from lib.theme import print_farewell
    print_farewell()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print()
        print_info(tr("errors.interrupted"))
    except Exception as e:
        print_error(tr("errors.generic", error=str(e)))
        sys.exit(1)
