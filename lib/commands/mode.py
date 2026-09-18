"""Mode slash 命令。

显示或切换当前 agent 运行模式，核心类为 ModeCommandHandler，
经 router 由交互循环分发并调用 lib.core.modes 服务。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.modes import apply_agent_mode_permissions, normalize_agent_mode, render_agent_mode_summary
from ..i18n import tr
from ..theme import console, print_error, print_info, print_success, print_summary_card
from .base import CommandContext, CommandHandler
from ..runtime import RuntimeContext


@dataclass
class ModeCommandHandler(CommandHandler):
    """显示或切换当前 agent 运行模式。"""

    name: str = "mode"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        state = runtime.app_state
        agent = runtime.agent
        if state is None:
            print_error(tr("runtime.state_unavailable"))
            return True

        if not command.args:
            console.print()
            console.print(render_agent_mode_summary(runtime.agent_mode or state.agent_mode))
            console.print()
            print_summary_card(
                tr("mode.title"),
                {
                    "/mode build": tr("mode.desc_build"),
                    "/mode plan": tr("mode.desc_plan"),
                    "/mode review": tr("mode.desc_review"),
                },
                footer=tr("mode.usage"),
            )
            return True

        try:
            requested = normalize_agent_mode(command.args, fallback=None)
        except ValueError:
            requested = None
        if not requested:
            print_error(tr("mode.unknown", name=command.args))
            print_info(tr("mode.usage"))
            return True

        # 直接作用于会话 runtime（而非依赖「全局运行时」的隐式配对），避免顺序耦合。
        # mode 规则是整体替换，且与 session 授权分开存储，不会清空用户已授予的权限。
        definition = apply_agent_mode_permissions(requested, runtime=runtime.permissions)
        state.agent_mode = definition.name
        if agent is not None and hasattr(agent, "set_agent_mode"):
            agent.set_agent_mode(definition.name)

        runtime.sync_from_app_state(state)
        if agent is not None:
            runtime.attach_agent(agent)
            runtime.attach_tools(
                getattr(agent, "tools", []),
                registry=getattr(runtime, "tool_registry", None),
            )

        user_config = runtime.config_stores.get("user")
        if user_config is not None:
            user_config.agent_mode = definition.name
            user_config.save()

        print_success(tr("mode.updated", name=definition.name, label=definition.label))
        print_info(definition.description)
        return True


__all__ = ["ModeCommandHandler"]
