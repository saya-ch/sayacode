"""策略判定模块：常量、类型、规则匹配与三层策略。

判定优先级见 permissions 主模块说明。
本模块只做纯判定与策略文件解析，不持有会话状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Optional
import json
import re

from .paths import SayacodePaths


PermissionAction = str
VALID_ACTIONS = {"allow", "ask", "deny"}

# 权限来源类型
PermissionSource = str
SOURCE_USER = "user"
SOURCE_PROJECT = "project"
SOURCE_SESSION = "session"
SOURCE_BUILTIN = "built-in"

# 特别危险工具集合，allow 会被强制降级
DANGEROUS_TOOLS = {
    "delete_file",
    "git_push",
}

READ_ONLY_TOOLS = {
    "read_file",
    "glob_search",
    "grep_search",
    "list_directory",
    "analyze_project",
    "get_project_summary",
    "list_project_files",
    "get_file_info",
    "list_symbols",
    "find_symbol",
    "git_status",
    "git_diff",
    "git_log",
    "git_branch",
    "git_remote",
    "check_command_safety_tool",
    "get_system_info",
    "list_environment_variables",
    "read_output_file",
    "web_search",
}

SAFE_WRITE_TOOLS = {
    "write_file",
    "search_replace",
    "create_directory",
    "batch_edit",
}

SAFE_GIT_TOOLS = {
    "git_add",
    "git_commit",
}

DIRECT_MUTATING_TOOLS = {
    "execute_command_tool",
    "git_checkout",
    "git_pull",
    "git_stash",
}

ASK_TOOLS = {
    "delete_file",
    "git_push",
}

RESTRICTED_TOOLS = ASK_TOOLS
MUTATING_TOOLS = SAFE_WRITE_TOOLS | SAFE_GIT_TOOLS | DIRECT_MUTATING_TOOLS | ASK_TOOLS

DEFAULT_COMMAND_RULES: Dict[str, PermissionAction] = {
    # 默认放宽常见命令，绕行命令仍需确认
    "git push*": "ask",
    "git reset*": "ask",
    "git clean*": "ask",
    "git checkout*": "ask",
    "git switch*": "ask",
    "rm *": "ask",
    "del *": "ask",
    "erase *": "ask",
    "rd *": "ask",
    "rmdir *": "ask",
    "remove-item *": "ask",
}

DEFAULT_TOOL_RULES: Dict[str, PermissionAction] = {
    **{name: "allow" for name in READ_ONLY_TOOLS},
    **{name: "allow" for name in SAFE_WRITE_TOOLS},
    **{name: "allow" for name in SAFE_GIT_TOOLS},
    **{name: "allow" for name in DIRECT_MUTATING_TOOLS},
    **{name: "ask" for name in ASK_TOOLS},
}

PATH_ARGUMENT_KEYS = {
    "path",
    "file_path",
    "directory",
    "dir_path",
    "target_path",
    "source_path",
    "src",
    "dst",
    "cwd",
}


@dataclass(frozen=True)
class PermissionRequest:
    """权限询问载荷。"""

    tool_name: str
    action: PermissionAction
    arguments_preview: str
    source: str


@dataclass(frozen=True)
class PermissionDecision:
    """权限检查结果。"""

    allowed: bool
    action: PermissionAction
    reason: str
    source: str


def _normalize_action(value: Any, fallback: PermissionAction) -> PermissionAction:
    action = str(value or "").strip().lower()
    return action if action in VALID_ACTIONS else fallback


def _match_rule_with_key(
    rules: Dict[str, PermissionAction],
    tool_name: str,
) -> Optional[tuple[str, PermissionAction]]:
    """查规则字典，返回命中的规则键与动作，精确优先其次前缀通配。"""
    if tool_name in rules:
        return tool_name, rules[tool_name]
    for pattern, action in rules.items():
        if pattern.endswith("*") and tool_name.startswith(pattern[:-1]):
            return pattern, action
    return None


def _match_rule(
    rules: Dict[str, PermissionAction],
    tool_name: str,
) -> Optional[PermissionAction]:
    """查规则字典，返回动作，精确优先其次前缀通配。"""
    matched = _match_rule_with_key(rules, tool_name)
    return matched[1] if matched else None


def _is_sensitive_key(key: str) -> bool:
    normalized = key.upper()
    return any(marker in normalized for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH"))


def summarize_arguments(arguments: Dict[str, Any]) -> str:
    """生成紧凑且已脱敏的参数预览。"""
    if not arguments:
        return "{}"

    redacted: Dict[str, Any] = {}
    for key, value in arguments.items():
        key_text = str(key)
        if _is_sensitive_key(key_text):
            redacted[key_text] = "***"
            continue
        value_text = str(value)
        redacted[key_text] = value_text[:160] + ("..." if len(value_text) > 160 else "")
    return json.dumps(redacted, ensure_ascii=False, sort_keys=True)


def _extract_argument_paths(arguments: Dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key, value in arguments.items():
        if str(key) not in PATH_ARGUMENT_KEYS:
            continue
        if isinstance(value, (list, tuple, set)):
            paths.extend(str(item) for item in value if item)
        elif value:
            paths.append(str(value))
    return paths


def _normalize_path_for_match(path: str) -> str:
    return str(path).replace("\\", "/").strip()


def _path_pattern_matches(pattern: str, path: str) -> bool:
    normalized_pattern = _normalize_path_for_match(pattern)
    candidates = {path, path.lower()}
    pattern_candidates = {normalized_pattern, normalized_pattern.lower()}
    return any(
        fnmatch(candidate, pattern_candidate)
        for candidate in candidates
        for pattern_candidate in pattern_candidates
    )


def _policy_paths(workspace: Optional[Path]) -> list[tuple[str, Path]]:
    sayacode_paths = SayacodePaths.resolve(create=False)
    paths = [("user", sayacode_paths.user_permissions)]
    if workspace:
        paths.append(("project", sayacode_paths.project_permissions(workspace)))
    return paths


def _read_policy_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


class PermissionPolicy:
    """合并后的用户与项目工具权限策略。"""

    def __init__(
        self,
        workspace: Optional[Path] = None,
        default_action: PermissionAction = "ask",
        tool_rules: Optional[Dict[str, PermissionAction]] = None,
        path_rules: Optional[Dict[str, PermissionAction]] = None,
        command_rules: Optional[Dict[str, PermissionAction]] = None,
        sources: Optional[Dict[str, str]] = None,
        path_sources: Optional[Dict[str, str]] = None,
        command_sources: Optional[Dict[str, str]] = None,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve() if workspace else None
        self.default_action = _normalize_action(default_action, fallback="ask")
        self.tool_rules = dict(DEFAULT_TOOL_RULES)
        # 策略文件手写的危险工具 allow 同样强制降级为拒绝
        # 否则能写策略文件就能静默提权
        self.stripped_dangerous: List[str] = []
        if tool_rules:
            for tool_name, action in tool_rules.items():
                normalized_action = _normalize_action(action, fallback="")
                if not normalized_action:
                    continue
                if normalized_action == "allow" and str(tool_name) in DANGEROUS_TOOLS:
                    self.tool_rules[str(tool_name)] = "deny"
                    self.stripped_dangerous.append(str(tool_name))
                    continue
                self.tool_rules[str(tool_name)] = normalized_action
        self.path_rules: Dict[str, PermissionAction] = {}
        if path_rules:
            for pattern, action in path_rules.items():
                normalized_action = _normalize_action(action, fallback="")
                if normalized_action:
                    self.path_rules[str(pattern)] = normalized_action
        self.command_rules: Dict[str, PermissionAction] = dict(DEFAULT_COMMAND_RULES)
        if command_rules:
            for pattern, action in command_rules.items():
                normalized_action = _normalize_action(action, fallback="")
                if normalized_action:
                    self.command_rules[str(pattern)] = normalized_action
        self.sources = sources or {}
        self.path_sources = path_sources or {}
        self.command_sources = {
            **{pattern: "built-in" for pattern in DEFAULT_COMMAND_RULES},
            **(command_sources or {}),
        }
        # 策略文件键优先于内置精确键，避免通配被抵消
        # 否则摘要显示拒绝实际却是放行
        self._policy_rule_keys = {
            str(name) for name, source in self.sources.items()
            if str(source) != "built-in"
        }

    def _match_tool_rule(
        self,
        tool_name: str,
    ) -> Optional[tuple[str, PermissionAction]]:
        """匹配工具规则，策略文件键优先，同层精确优先于通配。"""
        if self._policy_rule_keys:
            explicit = _match_rule_with_key(
                {
                    name: action
                    for name, action in self.tool_rules.items()
                    if name in self._policy_rule_keys
                },
                tool_name,
            )
            if explicit:
                return explicit
        return _match_rule_with_key(self.tool_rules, tool_name)

    @classmethod
    def load(cls, workspace: Optional[Path] = None) -> "PermissionPolicy":
        """加载内置、用户、项目三层策略。"""
        merged_rules: Dict[str, PermissionAction] = {}
        merged_path_rules: Dict[str, PermissionAction] = {}
        merged_command_rules: Dict[str, PermissionAction] = {}
        sources: Dict[str, str] = {name: "built-in" for name in DEFAULT_TOOL_RULES}
        path_sources: Dict[str, str] = {}
        command_sources: Dict[str, str] = {}
        default_action: PermissionAction = "ask"

        for label, path in _policy_paths(workspace):
            data = _read_policy_file(path)
            if not data:
                continue

            candidate_default = _normalize_action(data.get("default"), fallback="")
            if candidate_default:
                default_action = candidate_default

            for tool_name, action in (data.get("tools") or {}).items():
                normalized_action = _normalize_action(action, fallback="")
                if normalized_action:
                    merged_rules[str(tool_name)] = normalized_action
                    sources[str(tool_name)] = label

            for pattern, action in (data.get("paths") or {}).items():
                normalized_action = _normalize_action(action, fallback="")
                if normalized_action:
                    merged_path_rules[str(pattern)] = normalized_action
                    path_sources[str(pattern)] = label

            for pattern, action in (data.get("commands") or {}).items():
                normalized_action = _normalize_action(action, fallback="")
                if normalized_action:
                    merged_command_rules[str(pattern)] = normalized_action
                    command_sources[str(pattern)] = label

        return cls(
            workspace=workspace,
            default_action=default_action,
            tool_rules=merged_rules,
            path_rules=merged_path_rules,
            command_rules=merged_command_rules,
            sources=sources,
            path_sources=path_sources,
            command_sources=command_sources,
        )

    def decide(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> PermissionDecision:
        """返回该工具按当前配置应执行的动作，危险工具统一兜底降级。"""
        decision = self._decide_raw(tool_name, arguments or {})
        if decision.action == "allow" and tool_name in DANGEROUS_TOOLS:
            if tool_name not in self.stripped_dangerous:
                self.stripped_dangerous.append(tool_name)
            return PermissionDecision(
                allowed=False,
                action="deny",
                reason=f"{tool_name}: deny (危险工具不允许自动放行)",
                source=decision.source,
            )
        return decision

    def _decide_raw(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> PermissionDecision:
        """解析策略文件动作，显式拒绝优先，其次命令路径工具与默认。"""
        explicit_deny = self._decide_explicit_tool_deny(tool_name)
        if explicit_deny:
            return explicit_deny

        command_decision = self._decide_command_rule(tool_name, arguments)
        if command_decision:
            return command_decision

        path_decision = self._decide_path_rule(tool_name, arguments)
        if path_decision:
            return path_decision

        matched = self._match_tool_rule(tool_name)
        if matched:
            pattern, action = matched
            source = self.sources.get(pattern, "default")
        else:
            action = self.default_action
            source = "default"
        return PermissionDecision(
            allowed=action == "allow",
            action=action,
            reason=f"{tool_name}: {action}",
            source=source,
        )

    def _decide_explicit_tool_deny(
        self,
        tool_name: str,
    ) -> Optional[PermissionDecision]:
        """策略文件显式手写的工具拒绝，不含内置默认值。"""
        matched = self._match_tool_rule(tool_name)
        if not matched:
            return None
        pattern, action = matched
        if action != "deny" or self.sources.get(pattern) == "built-in":
            return None
        return PermissionDecision(
            allowed=False,
            action="deny",
            reason=f"{tool_name}: deny",
            source=self.sources.get(pattern, "policy"),
        )

    def to_dict(self) -> Dict[str, Any]:
        """序列化生效后的策略。"""
        return {
            "default": self.default_action,
            "tools": dict(sorted(self.tool_rules.items())),
            "paths": dict(sorted(self.path_rules.items())),
            "commands": dict(sorted(self.command_rules.items())),
        }

    def _decide_path_rule(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Optional[PermissionDecision]:
        if not self.path_rules:
            return None

        for value in _extract_argument_paths(arguments):
            normalized_path = _normalize_path_for_match(value)
            for pattern, action in self.path_rules.items():
                if _path_pattern_matches(pattern, normalized_path):
                    source = self.path_sources.get(pattern, "path")
                    return PermissionDecision(
                        allowed=action == "allow",
                        action=action,
                        reason=f"{tool_name}: {action} for path {pattern}",
                        source=source,
                    )
        return None

    def _decide_command_rule(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Optional[PermissionDecision]:
        if not self.command_rules:
            return None

        command = str(arguments.get("command") or "").strip()
        if not command:
            return None

        segments = [s.strip().lower() for s in re.split(r"(?:&&|\|\||;|\|)", command) if s.strip()]
        if not segments:
            return None
        for segment in segments:
            for pattern, action in self.command_rules.items():
                if fnmatch(segment, pattern.lower()):
                    source = self.command_sources.get(pattern, "command")
                    return PermissionDecision(
                        allowed=action == "allow",
                        action=action,
                        reason=f"{tool_name}: {action} for command {pattern}",
                        source=source,
                    )
        return None
