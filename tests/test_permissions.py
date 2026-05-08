from lib.core.permissions import (
    configure_permission_workspace,
    enforce_tool_permission,
    get_permission_policy_summary,
    set_permission_confirm_callback,
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


def test_all_tools_default_to_allow(tmp_path):
    configure_permission_workspace(tmp_path)
    set_permission_confirm_callback(None)

    result = enforce_tool_permission("delete_file", {"path": "a.txt"})

    assert result is None


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
    also_allowed = enforce_tool_permission("delete_file", {"path": "src/app.py"})

    assert allowed is None
    assert also_allowed is None


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
    also_allowed = enforce_tool_permission("execute_command_tool", {"command": "git push"})

    assert allowed is None
    assert also_allowed is None


def test_argument_summary_redacts_sensitive_values():
    summary = summarize_arguments({"api_key": "secret-value", "path": "README.md"})

    assert "secret-value" not in summary
    assert "***" in summary
    assert "README.md" in summary


def test_permission_policy_summary_renders(tmp_path):
    set_language("en")
    configure_permission_workspace(tmp_path)

    summary = get_permission_policy_summary()

    assert "Permission Policy" in summary
    assert "write_file" in summary
