"""终端实际斜杠命令的面向用户说明。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HelpTopic:
    """单条命令的帮助主题，聚合多语言说明与用法。
    参数是命令名别名分组与说明，返回不可变主题对象。
    调用方按语言取摘要用法示例，展示层不再拼字符串。"""

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
        """按语言取命令一句话摘要。
        参数是语言标识，返回对应摘要。
        非中文统一给英文，调用方直接展示。"""
        return self.zh if language == "zh" else self.en

    def detail(self, language: str) -> str:
        """按语言取补充说明，空串表示无补充。
        参数是语言标识，返回对应详情。
        展示层有内容才另起段落，避免多余空行。"""
        return self.detail_zh if language == "zh" else self.detail_en

    def shown_usage(self, language: str) -> str:
        """按语言取用法行，英文缺失回落中文。
        参数是语言标识，返回用法字符串。
        回落保证总有可展示的用法，不返回空串。"""
        return self.usage if language == "zh" else self.usage_en or self.usage

    def shown_example(self, language: str) -> str:
        """按语言取示例行，英文缺失回落中文。
        参数是语言标识，返回示例字符串。
        用法是给格式，示例是给可复制的输入。"""
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
        quick_actions=("/session list", "/session new", "/session use "),
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
        quick_actions=("/model add", "/model use ", "/model test", "/model key "),
    ),
    HelpTopic(
        "trust",
        (),
        "model",
        "设置当前会话信任档位或新会话默认值",
        "Set session trust or the default for new sessions",
        "/trust [read_only|ask|jev|full|default <档位>|clear]",
        "/trust jev",
        "只读档只提供读取类工具且不提供 Shell；询问档对每次有副作用的调用审批，可记住完全相同的调用；Jev 档自动批准低风险调用、把不确定调用交给用户并拒绝明确越权调用；完全信任不弹审批。所有档位均无操作系统沙箱，绝对路径可访问工作区外。",
        "Read-only exposes read tools without Shell; ask pauses every side-effecting call and can remember the exact call; Jev auto-approves low-risk calls, routes uncertainty to the user, and rejects clear policy violations; full trust skips approvals. None provides an OS sandbox, and absolute paths may reach outside the workspace.",
        usage_en="/trust [read_only|ask|jev|full|default <level>|clear]",
        quick_actions=("/trust read_only", "/trust ask", "/trust jev", "/trust full"),
    ),
    HelpTopic(
        "reviewer",
        (),
        "model",
        "配置和验证 Jev 自动审理",
        "Configure and test Jev automatic review",
        "/reviewer [status|setup|test|remove]",
        "/reviewer setup",
        "配置独立的 TypeSafe API 地址、隐藏密钥和版本化 Jev 模型。配置后使用 /trust jev 启用当前会话。",
        "Configure an independent TypeSafe endpoint, hidden key, and versioned Jev model. Enable it for the current session with /trust jev.",
        quick_actions=("/reviewer status", "/reviewer setup", "/reviewer test"),
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
    HelpTopic("prefs", (), "model", "查看回答语言", "Show response language", "/prefs", "/prefs"),
    HelpTopic(
        "settings",
        (),
        "model",
        "查看或设置终端运行参数",
        "Show or set terminal settings",
        "/settings [show|set output_limit_bytes <字节>|set task_notice_limit_bytes <字节>|set max_consecutive_wakes <次数>|set shutdown_grace_seconds <秒>]",
        "/settings set output_limit_bytes 65536",
        usage_en="/settings [show|set output_limit_bytes <bytes>|set task_notice_limit_bytes <bytes>|set max_consecutive_wakes <count>|set shutdown_grace_seconds <seconds>]",
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
        "/team [list|status <ID>|spawn <角色> [--worktree|--shared] <任务>|pending <ID>|approve <ID>|reject <ID>|wait <ID>|stop <ID>|resume <ID>|followup <ID> <消息>|diff <ID>|apply <ID>|cleanup <ID>]",
        "/team spawn builder --shared 直接修复解析器",
        "新子 Agent 用 /team spawn 创建；当前轮结束后进入 idle，可用 /team followup 在原线程继续。builder 默认使用 Git worktree；需要直接共享父工作区时显式加 --shared。待批准子 Agent 先用 /team pending <ID> 查看，再用 /team approve <ID> 或 /team reject <ID> 逐项审批。/team apply 只适用于 worktree 交付。",
        "Create a continuable child with /team spawn. A builder uses a Git worktree by default; add --shared to write in the parent workspace. It becomes idle after a turn and continues in the same thread with /team followup. Inspect paused approvals with /team pending <ID>, then use /team approve <ID> or /team reject <ID>. /team apply is for worktree deliveries.",
        usage_en="/team [list|status <ID>|spawn <role> [--worktree|--shared] <task>|pending <ID>|approve <ID>|reject <ID>|wait <ID>|stop <ID>|resume <ID>|followup <ID> <message>|diff <ID>|apply <ID>|cleanup <ID>]",
        example_en="/team spawn builder --shared Fix the parser directly",
        quick_actions=(
            "/team list",
            "/team spawn builder ",
            "/team spawn planner ",
            "/team spawn reviewer ",
            "/team status ",
        ),
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
        "skills",
        (),
        "extension",
        "列出当前工作区可用的 Skill",
        "List Skills available in this workspace",
        "/skills",
        "/skills",
        "项目 Skill 位于 .agents/skills/<名称>/SKILL.md，用户 Skill 位于 SAYACODE_HOME/skills/<名称>/SKILL.md；同名时项目优先。",
        "Project Skills live in .agents/skills/<name>/SKILL.md and user Skills in SAYACODE_HOME/skills/<name>/SKILL.md; the project copy takes priority.",
    ),
    HelpTopic(
        "skill",
        (),
        "extension",
        "查看或启用 Skill",
        "Inspect or activate a Skill",
        "/skill [show|use] <名称>",
        "/skill use review",
        "Skill 按需载入当前会话的 LangGraph 状态。引用文件通过原生 load_skill 工具读取；脚本执行仍由 Shell 权限控制。",
        "A Skill loads into the current LangGraph state on demand. Referenced files use the native load_skill tool; scripts still follow Shell approval.",
        usage_en="/skill [show|use] <name>",
        quick_actions=("/skill show ", "/skill use "),
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
        quick_actions=("/mcp status", "/mcp reload", "/mcp trust"),
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
    """按命令名找回帮助主题，找不到返回空。
    参数是用户输入的命令或别名，返回主题或空。
    会去斜杠与多余参数，前后空格不影响查找。"""
    return (
        _BY_NAME.get(name.strip().lstrip("/").split(maxsplit=1)[0].lower())
        if name.strip()
        else None
    )


def format_help(query: str = "", *, language: str = "en") -> str:
    """拼出总览或单命令的纯文本帮助，供两端共用。
    参数是查询词与语言，返回多行帮助文本。
    空查询按分组列命令，非空查询给别名用法示例与详情。
    流程分两段，先处理总览再处理单命令，未知命令给引导语。
    坑点是纯文本不带样式，交互面板另做排版。"""
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
                for item in ("/" + name for name in topic.names)
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
            f"未知命令：/{asked}。用 /help 查看可用命令。"
            if zh
            else f"Unknown command: /{asked}. Use /help for available commands."
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
