"""终端实际斜杠命令的面向用户说明。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HelpTopic:
    name: str
    aliases: tuple[str, ...]
    group: str
    zh: str
    en: str
    usage: str
    example: str
    detail_zh: str = ""
    detail_en: str = ""
    usage_en: str | None = None
    example_en: str | None = None
    quick_actions: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def summary(self, language: str) -> str:
        return self.zh if language == "zh" else self.en

    def detail(self, language: str) -> str:
        return self.detail_zh if language == "zh" else self.detail_en

    def shown_usage(self, language: str) -> str:
        return self.usage if language == "zh" else self.usage_en or self.usage

    def shown_example(self, language: str) -> str:
        return self.example if language == "zh" else self.example_en or self.example


GROUPS = (
    ("start", "开始", "Getting started"),
    ("session", "会话与上下文", "Sessions and context"),
    ("model", "模型与偏好", "Models and preferences"),
    ("work", "代码与任务", "Code and tasks"),
    ("extension", "权限与扩展", "Permissions and extensions"),
    ("diagnostic", "状态与诊断", "Status and diagnostics"),
)


TOPICS = (
    HelpTopic(
        "help",
        ("guide", "start"),
        "start",
        "查看命令总览或单条命令用法",
        "Show commands or detailed help",
        "/help [命令]",
        "/help session",
        usage_en="/help [command]",
    ),
    HelpTopic(
        "clear",
        (),
        "start",
        "清理终端显示",
        "Clear the terminal display",
        "/clear",
        "/clear",
        "只清屏，不清除会话或文件。",
        "Clears the screen, not the session or files.",
    ),
    HelpTopic(
        "quit",
        ("exit",),
        "start",
        "退出当前 CLI",
        "Exit the CLI",
        "/quit",
        "/quit",
        "后台任务按配置的宽限期停止并保存检查点。",
        "Background tasks drain to a checkpoint during the configured grace period.",
    ),
    HelpTopic(
        "commands",
        (),
        "start",
        "列出 Markdown 自定义命令",
        "List Markdown custom commands",
        "/commands",
        "/commands",
    ),
    HelpTopic(
        "new",
        ("reset",),
        "session",
        "创建并切换到新会话",
        "Create and switch to a new session",
        "/new",
        "/new",
        "使用 /sessions 可找回旧会话；文件和 Git 改动不会被撤销。",
        "Use /sessions to find older sessions; file and Git changes are not undone.",
    ),
    HelpTopic(
        "session",
        ("sessions",),
        "session",
        "查看、创建、切换和命名会话",
        "List, create, switch, and name sessions",
        "/session [current|list|new|use <ID>|rename <标题>]",
        "/new",
        "使用 /new 直接开新会话；/session list 查看列表，/session use <ID> 切换，/session rename <标题> 重命名。新会话不复制旧对话。",
        "Use /new for a new session, /session list to browse, /session use <ID> to switch, and /session rename <title> to rename. A new session does not copy prior messages.",
        usage_en="/session [current|list|new|use <ID>|rename <title>]",
    ),
    HelpTopic(
        "history",
        (),
        "session",
        "查看当前会话消息",
        "Show current session messages",
        "/history",
        "/history",
    ),
    HelpTopic(
        "compact",
        (),
        "session",
        "摘要旧消息，可指定关注点",
        "Summarize older messages with an optional focus",
        "/compact [关注点]",
        "/compact 保留未完成任务",
        usage_en="/compact [focus]",
        example_en="/compact outstanding work",
    ),
    HelpTopic(
        "rewind",
        (),
        "session",
        "列出检查点或从指定检查点继续",
        "List checkpoints or continue from one",
        "/rewind [序号|检查点 ID]",
        "/rewind 2",
        "只改变对话图状态，不撤销文件或 Git 操作。",
        "Changes conversation state only; it does not undo file or Git operations.",
    ),
    HelpTopic(
        "models",
        (),
        "model",
        "列出模型、接口协议和上下文配置",
        "List models, API protocols, and context limits",
        "/models",
        "/models",
        "用 /model add 打开向导，再用 /model use <名称> 切换。",
        "Use /model add to open setup, then /model use <name> to switch.",
    ),
    HelpTopic(
        "model",
        ("config",),
        "model",
        "添加、切换和验证模型",
        "Add, switch, and verify models",
        "/model [list|show|add|key <名称>|use <名称>|remove <名称>|test [名称]]",
        "/model add",
        "在交互终端输入 /model add，依次填写接口协议、完整接口地址、API Key、模型 ID、上下文长度和最大输出。API Key 默认必填；仅无认证接口输入 none。留空会重新提示，也不会读取环境变量。已保存的配置用 /model key <名称> 在隐藏输入框中更新密钥；输入 none 可清除。密钥不进入命令历史；配置名自动生成。/model test [名称] 检查文本、工具调用和流式输出能力。",
        "Enter /model add in the interactive terminal to choose an API protocol and enter the full endpoint URL, API key, model ID, context length, and maximum output. The API key is required by default; enter none only for an unauthenticated endpoint. Blank prompts again and never reads an environment variable. Use /model key <name> to update a saved profile through a hidden prompt, or enter none to clear it. Keys are omitted from command history; a profile name is generated automatically. /model test [name] checks text, tool-call, and streaming capabilities.",
        usage_en="/model [list|show|add|key <name>|use <name>|remove <name>|test [name]]",
        quick_actions=("/model add",),
    ),
    HelpTopic(
        "trust",
        (),
        "model",
        "设置当前会话信任档位或新会话默认值",
        "Set session trust or the default for new sessions",
        "/trust [read_only|ask|full|default <档位>|clear]",
        "/trust ask",
        "只读档自动允许读取，Shell 仍逐次审批；询问档对每次有副作用的调用审批，可记住完全相同的调用；完全信任不弹审批。所有档位均无操作系统沙箱，绝对路径可访问工作区外。",
        "Read-only trust allows reads and asks for every Shell command; ask trust pauses every side-effecting call and can remember the exact call; full trust skips approvals. None provides an OS sandbox, and absolute paths may reach outside the workspace.",
        usage_en="/trust [read_only|ask|full|default <level>|clear]",
    ),
    HelpTopic(
        "lang",
        (),
        "model",
        "设置终端与回答语言",
        "Set terminal and response language",
        "/lang [auto|zh|en]",
        "/lang zh",
    ),
    HelpTopic(
        "style",
        (),
        "model",
        "设置回答风格",
        "Set response style",
        "/style [名称]",
        "/style standard",
        "不带参数可查看可用风格。",
        "Omit the argument to list available styles.",
        usage_en="/style [name]",
    ),
    HelpTopic(
        "prefs", (), "model", "查看语言与风格", "Show language and style", "/prefs", "/prefs"
    ),
    HelpTopic(
        "settings",
        (),
        "model",
        "查看或设置终端运行参数",
        "Show or set terminal settings",
        "/settings [show|set output_limit_bytes <字节>|set shutdown_grace_seconds <秒>]",
        "/settings set output_limit_bytes 65536",
        usage_en="/settings [show|set output_limit_bytes <bytes>|set shutdown_grace_seconds <seconds>]",
    ),
    HelpTopic(
        "todos",
        (),
        "work",
        "显示当前线程待办",
        "Show the current thread's todos",
        "/todos",
        "/todos",
    ),
    HelpTopic(
        "team",
        (),
        "work",
        "管理后台 builder/planner/reviewer 任务",
        "Manage builder, planner, and reviewer tasks",
        "/team [list|status <ID>|spawn <角色> <任务>|pending <ID>|approve <ID>|reject <ID>|wait <ID>|stop <ID>|resume <ID>|followup <ID> <消息>|diff <ID>|apply <ID>|cleanup <ID>]",
        "/team spawn reviewer 检查权限实现",
        "新任务可用 /team spawn 创建。待批准任务先用 /team pending <ID> 查看，再在交互终端用 /team approve <ID> 或 /team reject <ID> 逐项审批；headless 不会自动批准。/team apply 会修改主工作区。",
        "Create a task with /team spawn. Inspect a paused task with /team pending <ID>, then handle each approval with /team approve <ID> or /team reject <ID> in the interactive terminal. Headless mode never approves automatically. /team apply changes the main workspace.",
        usage_en="/team [list|status <ID>|spawn <role> <task>|pending <ID>|approve <ID>|reject <ID>|wait <ID>|stop <ID>|resume <ID>|followup <ID> <message>|diff <ID>|apply <ID>|cleanup <ID>]",
        example_en="/team spawn reviewer Review permission checks",
    ),
    HelpTopic(
        "approve",
        ("reject",),
        "work",
        "处理主 Agent 的待批准操作",
        "Review pending main-agent actions",
        "/approve [会话 ID] 或 /reject [会话 ID]",
        "/approve",
        "子任务完成后主 Agent 会自动继续；若继续过程中请求工具审批，用 /approve 查看并逐项决定。/reject 拒绝全部操作。",
        "A child result can start a parent turn. If that turn pauses for tool approval, use /approve to review each action or /reject to decline all.",
        usage_en="/approve [session ID] or /reject [session ID]",
    ),
    HelpTopic(
        "tools",
        (),
        "work",
        "列出工具或查看单个工具参数",
        "List tools or inspect one tool's schema",
        "/tools [工具名]",
        "/tools read_file",
        usage_en="/tools [tool-name]",
    ),
    HelpTopic(
        "git",
        (),
        "work",
        "查看 Git 状态、差异与历史",
        "Read Git status, diff, and history",
        "/git [status|diff [ref]|log [数量]|branch|remote|show [ref]]",
        "/git diff",
        "Git 工具只执行查询；提交、拉取和推送等变更通过 Shell 执行，并按当前信任档位处理。",
        "The Git tool only queries; commits, pulls, pushes, and other changes use Shell under the current trust level.",
    ),
    HelpTopic(
        "symbols",
        (),
        "work",
        "列出或定位代码符号",
        "List or locate code symbols",
        "/symbols [名称]",
        "/symbols median",
        usage_en="/symbols [name]",
    ),
    HelpTopic(
        "analyze",
        (),
        "work",
        "分析项目结构与依赖",
        "Analyze project structure and dependencies",
        "/analyze",
        "/analyze",
    ),
    HelpTopic(
        "memory",
        (),
        "work",
        "查看、初始化或追加项目记忆",
        "Show, initialize, or append project memory",
        "/memory [status|init <user|project>|append <user|project> <文本>]",
        "/memory append project 遵循项目代码风格",
        usage_en="/memory [status|init <user|project>|append <user|project> <text>]",
        example_en="/memory append project Follow project conventions",
    ),
    HelpTopic(
        "mcp",
        (),
        "extension",
        "管理 MCP 服务器与项目信任",
        "Manage MCP servers and project trust",
        "/mcp [status|trust|untrust|reload|add <名称> <命令> [参数...]|remove <名称>]",
        "/mcp status",
        "信任项目 MCP 配置会启用外部服务器；工具调用仍经过权限入口。",
        "Trusting a project MCP configuration activates external servers; their tools still pass through permissions.",
        usage_en="/mcp [status|trust|untrust|reload|add <name> <command> [args...]|remove <name>]",
    ),
    HelpTopic(
        "hooks",
        (),
        "extension",
        "查看、信任或重载命令 Hook",
        "Inspect, trust, or reload command hooks",
        "/hooks [status|trust|untrust|reload|audit]",
        "/hooks status",
    ),
    HelpTopic(
        "status",
        ("stats", "context"),
        "diagnostic",
        "显示当前会话、模型与用量",
        "Show session, model, and usage",
        "/status",
        "/status",
    ),
    HelpTopic(
        "workspace",
        (),
        "diagnostic",
        "显示当前工作区",
        "Show the current workspace",
        "/workspace",
        "/workspace",
        "切换工作区请重新启动并传入 --workspace。",
        "Restart with --workspace to choose another workspace.",
    ),
    HelpTopic(
        "paths",
        (),
        "diagnostic",
        "显示本地存储路径",
        "Show local storage paths",
        "/paths",
        "/paths",
    ),
    HelpTopic(
        "doctor",
        (),
        "diagnostic",
        "运行诊断并可写支持包",
        "Run diagnostics and optionally write a support bundle",
        "/doctor [支持包路径]",
        "/doctor support.json",
        usage_en="/doctor [bundle-path]",
    ),
    HelpTopic(
        "trace",
        (),
        "diagnostic",
        "查看本地执行追踪",
        "Show local execution traces",
        "/trace [run ID]",
        "/trace",
    ),
)


ALL_COMMAND_NAMES = tuple(name for topic in TOPICS for name in topic.names)
_BY_NAME = {name: topic for topic in TOPICS for name in topic.names}


def find_topic(name: str) -> HelpTopic | None:
    return (
        _BY_NAME.get(name.strip().lstrip("/").split(maxsplit=1)[0].lower())
        if name.strip()
        else None
    )


def format_help(query: str = "", *, language: str = "en") -> str:
    zh = language == "zh"
    if not query.strip():
        lines = ["SAYACODE 命令" if zh else "SAYACODE commands"]
        lines.append(
            "直接输入文字与 Agent 对话；用 /help <命令> 查看用法。"
            if zh
            else "Type a task to talk to the agent; use /help <command> for usage."
        )
        for group, group_zh, group_en in GROUPS:
            names = "  ".join(
                item
                for topic in TOPICS
                if topic.group == group
                for item in (*("/" + name for name in topic.names), *topic.quick_actions)
            )
            lines.append(f"{group_zh if zh else group_en}: {names}")
        lines.append(
            "用 /help <命令> 查看用法与示例。"
            if zh
            else "Use /help <command> for usage and examples."
        )
        return "\n".join(lines)

    asked = query.strip().lstrip("/").split(maxsplit=1)[0].lower()
    topic = find_topic(asked)
    if topic is None:
        return (
            f"未知命令：/{asked}。用 /help 查看内建命令，或用 /commands 查看自定义命令。"
            if zh
            else f"Unknown command: /{asked}. Use /help for built-in commands or /commands for custom commands."
        )
    aliases = ", ".join("/" + name for name in topic.names if name != asked)
    lines = [f"/{asked} — {topic.summary(language)}"]
    if aliases:
        lines.append(("别名：" if zh else "Aliases: ") + aliases)
    lines.append(("用法：" if zh else "Usage: ") + topic.shown_usage(language))
    lines.append(("示例：" if zh else "Example: ") + topic.shown_example(language))
    if detail := topic.detail(language):
        lines.append(detail)
    return "\n".join(lines)


__all__ = ["ALL_COMMAND_NAMES", "GROUPS", "TOPICS", "HelpTopic", "find_topic", "format_help"]
