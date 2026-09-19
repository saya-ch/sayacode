"""Doctor slash 命令。

对当前 workspace 运行本地诊断检查，核心类为 DoctorCommandHandler，
经 router 由交互循环分发并渲染诊断报告。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.doctor import has_failed_checks, render_doctor_report, run_doctor_checks
from ..i18n import tr
from ..runtime import RuntimeContext
from ..cli.theme import console, print_error, print_success
from .base import CommandContext, CommandHandler


@dataclass
class DoctorCommandHandler(CommandHandler):
    """对当前 workspace 运行本地诊断检查。"""

    name: str = "doctor"
    aliases: tuple[str, ...] = ()

    def handle(self, command: CommandContext, runtime: RuntimeContext) -> bool:
        checks = run_doctor_checks(runtime.workspace)
        console.print()
        console.print(render_doctor_report(checks))
        if has_failed_checks(checks):
            print_error(tr("doctor.blocking"))
        else:
            print_success(tr("doctor.ok"))
        return True


__all__ = ["DoctorCommandHandler"]
