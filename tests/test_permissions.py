import pytest

from lib.core.permissions import (
    DANGEROUS_TOOLS,
    _active_runtime,
    configure_permission_workspace,
    enforce_tool_permission,
    get_permission_policy_summary,
    reset_session_permission_rules,
    set_permission_confirm_callback,
    set_session_permission_rules,
    set_tool_permission,
    summarize_arguments,
)
from lib.core.private_io import write_private_json
from lib.i18n import set_language


def test_read_tool_is_allowed_by_default(tmp_path):
    configure_permission_workspace(tmp_path)
    set_permission_confirm_callback(None)

    result = enforce_tool_permission("read_file", {"path": "a.txt"})

    assert result is None


def test_only_high_risk_tools_default_to_ask_without_callback(tmp_path):
    configure_permission_workspace(tmp_path)
    set_permission_confirm_callback(None)

    delete_result = enforce_tool_permission("delete_file", {"path": "a.txt"})
    push_result = enforce_tool_permission("git_push", {})

    assert delete_result is not None
    assert "Permission required" in delete_result
    assert push_result is not None
    assert "Permission required" in push_result


def test_common_mutating_tools_are_allowed_by_default(tmp_path):
    configure_permission_workspace(tmp_path)
    set_permission_confirm_callback(None)

    assert enforce_tool_permission("execute_command_tool", {"command": "python --version"}) is None
    assert enforce_tool_permission("git_pull", {}) is None
    assert enforce_tool_permission("git_checkout", {"branch": "feature/test"}) is None
    assert enforce_tool_permission("git_stash", {}) is None


def test_tool_set_to_ask_triggers_callback(tmp_path):
    configure_permission_workspace(tmp_path)
    callback_called = []
    set_permission_confirm_callback(lambda req: callback_called.append(req.tool_name) or True)

    # 将工具设为 ask 模式，验证回调被触发
    set_tool_permission("write_file", "ask", scope="project")
    result = enforce_tool_permission("write_file", {"path": "a.txt"})

    assert result is None
    assert "write_file" in callback_called
    set_permission_confirm_callback(None)


def test_project_permission_rule_can_allow_tool(tmp_path):
    configure_permission_workspace(tmp_path)
    set_permission_confirm_callback(None)
    set_tool_permission("write_file", "allow", scope="project")

    result = enforce_tool_permission("write_file", {"path": "a.txt"})

    assert result is None


def test_permission_callback_allows_one_request(tmp_path):
    configure_permission_workspace(tmp_path)
    set_permission_confirm_callback(lambda request: request.tool_name == "execute_command_tool")

    result = enforce_tool_permission("execute_command_tool", {"command": "python --version"})

    assert result is None
    set_permission_confirm_callback(None)


def test_path_rule_can_allow_specific_path(tmp_path):
    policy_dir = tmp_path / ".sayacode"
    policy_dir.mkdir()
    write_private_json(policy_dir / "permissions.json", {
        "default": "ask",
        "paths": {"docs/**": "allow"},
    })
    configure_permission_workspace(tmp_path)
    set_permission_confirm_callback(None)

    allowed = enforce_tool_permission("delete_file", {"path": "docs/readme.md"})
    blocked = enforce_tool_permission("delete_file", {"path": "src/app.py"})

    assert allowed is None
    assert blocked is not None
    assert "Permission required" in blocked


def test_command_rule_can_allow_specific_command(tmp_path):
    policy_dir = tmp_path / ".sayacode"
    policy_dir.mkdir()
    write_private_json(policy_dir / "permissions.json", {
        "default": "ask",
        "commands": {"python -m pytest*": "allow"},
    })
    configure_permission_workspace(tmp_path)
    set_permission_confirm_callback(None)

    allowed = enforce_tool_permission("execute_command_tool", {"command": "python -m pytest -q"})
    blocked = enforce_tool_permission("execute_command_tool", {"command": "git push"})

    assert allowed is None
    assert blocked is not None
    assert "Permission required" in blocked


def test_builtin_command_rules_keep_shell_destructive_commands_behind_confirm(tmp_path):
    configure_permission_workspace(tmp_path)
    set_permission_confirm_callback(None)

    safe = enforce_tool_permission("execute_command_tool", {"command": "python --version"})
    rm_blocked = enforce_tool_permission("execute_command_tool", {"command": "rm README.md"})
    reset_blocked = enforce_tool_permission("execute_command_tool", {"command": "git reset --hard HEAD"})

    assert safe is None
    assert rm_blocked is not None
    assert "Permission required" in rm_blocked
    assert reset_blocked is not None
    assert "Permission required" in reset_blocked


def test_argument_summary_redacts_sensitive_values():
    summary = summarize_arguments({"api_key": "secret-value", "path": "README.md"})

    assert "secret-value" not in summary
    assert "***" in summary
    assert "README.md" in summary


# ── 危险工具不可自动放行（force-deny）────────────────────────────────────────────


def test_session_rule_cannot_allow_dangerous_tool(tmp_path):
    """session 级 allow 必须被降级为 deny，否则 DANGEROUS_TOOLS 的承诺不成立。"""
    configure_permission_workspace(tmp_path)
    reset_session_permission_rules()
    runtime = _active_runtime()

    set_session_permission_rules({"delete_file": "allow"}, source="test")
    assert runtime.session_rules["delete_file"] == "deny"
    assert runtime.check("delete_file", {"path": "a.txt"}).action == "deny"
    assert runtime.rule_set.stripped_dangerous.get("test")


def test_session_rule_allow_still_works_for_normal_tool(tmp_path):
    configure_permission_workspace(tmp_path)
    reset_session_permission_rules()
    runtime = _active_runtime()

    set_session_permission_rules({"write_file": "allow"}, source="test")

    assert runtime.session_rules["write_file"] == "allow"
    assert runtime.check("write_file", {"path": "a.txt"}).allowed is True


def test_persisting_allow_for_dangerous_tool_is_rejected(tmp_path):
    """把危险工具写进策略文件的 allow 必须被拒绝，避免静默提权。"""
    configure_permission_workspace(tmp_path)
    set_permission_confirm_callback(None)

    for tool_name in sorted(DANGEROUS_TOOLS):
        with pytest.raises(ValueError):
            set_tool_permission(tool_name, "allow", scope="user")


def test_dangerous_tool_can_still_be_explicitly_denied(tmp_path):
    configure_permission_workspace(tmp_path)
    set_permission_confirm_callback(None)

    set_tool_permission("git_push", "deny", scope="project")

    assert enforce_tool_permission("git_push", {}) is not None


# ── 连续拒绝回退模式：逐项询问 ───────────────────────────────────────────────


def test_fallback_mode_upgrades_allow_to_ask(tmp_path):
    configure_permission_workspace(tmp_path)
    reset_session_permission_rules()
    asked = []
    set_permission_confirm_callback(lambda req: asked.append(req.tool_name) or True)
    runtime = _active_runtime()

    assert runtime.check("read_file", {"path": "a.txt"}).action == "allow"

    runtime.is_in_fallback = True
    decision = runtime.check("read_file", {"path": "a.txt"})

    assert decision.action == "ask"
    assert "read_file" in asked
    set_permission_confirm_callback(None)


def test_fallback_mode_fails_closed_without_callback(tmp_path):
    """无交互回调时，回退模式下的 allow 必须按拒绝处理，而不是静默放行。"""
    configure_permission_workspace(tmp_path)
    reset_session_permission_rules()
    set_permission_confirm_callback(None)
    runtime = _active_runtime()

    runtime.is_in_fallback = True
    decision = runtime.check("read_file", {"path": "a.txt"})

    assert decision.allowed is False
    assert decision.action == "ask"


def test_fallback_mode_keeps_deny_unchanged(tmp_path):
    configure_permission_workspace(tmp_path)
    reset_session_permission_rules()
    set_permission_confirm_callback(lambda req: True)
    runtime = _active_runtime()

    set_tool_permission("write_file", "deny", scope="project")
    runtime.is_in_fallback = True

    assert runtime.check("write_file", {"path": "a.txt"}).action == "deny"
    set_permission_confirm_callback(None)


def test_denial_tracker_fallback_flag_is_synced_to_runtime():
    """UI 层的回退态必须同步给权限运行时，否则回退只是打印一行警告。"""
    import lib.cli.permissions as cli_permissions
    from lib.core.permissions import _active_runtime as active_runtime

    cli_permissions.reset_denial_tracker()
    assert active_runtime().is_in_fallback is False

    for _ in range(cli_permissions._denial_tracker.MAX_CONSECUTIVE):
        cli_permissions._denial_tracker.record_denial()
    cli_permissions._denial_tracker.enter_fallback_mode()
    cli_permissions._sync_fallback_flag()

    assert active_runtime().is_in_fallback is True
    cli_permissions.reset_denial_tracker()
    assert active_runtime().is_in_fallback is False


def test_permission_policy_summary_renders(tmp_path):
    set_language("en")
    configure_permission_workspace(tmp_path)

    summary = get_permission_policy_summary()

    assert "Permission Policy" in summary
    assert "write_file" in summary
