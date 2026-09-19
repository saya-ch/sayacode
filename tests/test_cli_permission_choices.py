"""权限确认弹窗的选项可达性。

对应 CODE_REVIEW_FINDINGS.md B2：``save`` 分支的实现与 i18n 文案都已存在，
但它不在 ``_CONFIRM_CHOICES`` 里 —— UI 不显示、上下键选不到，
只有未公开的 ``p`` 键能触发，因此该分支在生产中几乎不可达。
"""

import pytest

import lib.cli.permissions as cli_permissions
from lib.cli.permissions import _CONFIRM_CHOICES, _choice_from_key
from lib.commands import build_default_command_router
from lib.core.modes import apply_agent_mode_permissions
from lib.core.permission_policy import PermissionRequest
from lib.core.permission_session import _active_runtime
from lib.core.permission_workspace import (
    configure_permission_workspace,
    create_permission_runtime,
    reset_session_permission_rules,
    set_permission_confirm_callback,
)
from lib.core.private_io import write_private_json
from lib.runtime import RuntimeContext


class _DummyLive:
    """替掉 rich.Live，避免测试里真正渲染终端。"""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def update(self, *args, **kwargs):
        pass


@pytest.fixture
def interactive_save(monkeypatch):
    """让弹窗立即返回 save 选项。"""
    monkeypatch.setattr(cli_permissions, "_supports_interactive_input", lambda: True)
    monkeypatch.setattr(cli_permissions, "_read_choice_key", lambda: "s")
    monkeypatch.setattr(cli_permissions, "Live", _DummyLive)


def _request(tool_name: str) -> PermissionRequest:
    return PermissionRequest(
        tool_name=tool_name,
        action="ask",
        arguments_preview="{}",
        source="session",
    )


def test_save_choice_is_visible_in_ui():
    """save 必须出现在选项列表里，否则只能靠隐藏快捷键触发。"""
    ids = [choice[0] for choice in _CONFIRM_CHOICES]
    assert "save" in ids
    # 危险选项（deny）排在最后
    assert ids.index("save") < ids.index("deny")


def test_every_choice_has_an_i18n_label():
    from lib.i18n import tr

    for _, label_key, _color in _CONFIRM_CHOICES:
        assert tr(label_key) != label_key, f"缺少 i18n 文案: {label_key}"


def test_shortcut_keys_cover_all_choices():
    assert {_choice_from_key(key) for key in ("y", "a", "s", "n")} == {
        "once",
        "session",
        "save",
        "deny",
    }


def test_legacy_hidden_key_still_maps_to_save():
    assert _choice_from_key("p") == "save"


def test_escape_and_unknown_keys():
    assert _choice_from_key("\x1b") == "deny"
    assert _choice_from_key("esc") == "deny"
    assert _choice_from_key("z") is None


def test_save_writes_policy_for_normal_tool(tmp_path, monkeypatch, interactive_save):
    """普通工具选 save 应真正落盘（这条路径此前不可达）。"""
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    configure_permission_workspace(workspace)

    assert cli_permissions._confirm_tool_permission(_request("write_file")) is True

    # 有工作区时优先写 project scope
    policy_file = workspace / ".sayacode" / "permissions.json"
    assert policy_file.exists()
    assert "write_file" in policy_file.read_text(encoding="utf-8")


def test_dangerous_tool_save_does_not_crash_and_stays_gated(
    tmp_path, monkeypatch, interactive_save
):
    """危险工具选 save 不得崩溃、不得写 allow，且仍然被门控。

    set_tool_permission 对危险工具的 allow 会抛 ValueError，
    且 project→user 的回退同样会抛 —— 若不显式拦截，这里会直接崩溃。
    """
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    configure_permission_workspace(workspace)
    set_permission_confirm_callback(None)

    try:
        assert cli_permissions._confirm_tool_permission(_request("delete_file")) is True

        assert not (workspace / ".sayacode" / "permissions.json").exists()
        assert not (tmp_path / "home" / "permissions.json").exists()

        decision = _active_runtime().check("delete_file", {"path": "a.txt"})
        assert decision.action == "ask"     # 仍受默认 ask 门控，没有被放行
        assert decision.allowed is False    # 无回调 → 拒绝
    finally:
        set_permission_confirm_callback(None)


def test_session_choice_for_dangerous_tool_only_allows_once(
    tmp_path, monkeypatch, capsys
):
    """危险工具选「会话始终允许」既不写策略也不写 session 规则，仅本次放行。"""
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli_permissions, "_supports_interactive_input", lambda: True)
    monkeypatch.setattr(cli_permissions, "_read_choice_key", lambda: "a")
    monkeypatch.setattr(cli_permissions, "Live", _DummyLive)

    runtime = _active_runtime()
    runtime.set_session_rules({}, source="session")

    assert cli_permissions._confirm_tool_permission(_request("delete_file")) is True

    assert "delete_file" not in runtime.session.session_rules
    assert not (tmp_path / "home" / "permissions.json").exists()


# ── 会话授权必须可撤销（F5）──────────────────────────────────────────────────


def test_permissions_reset_command_revokes_session_grants(tmp_path, monkeypatch):
    """``/permissions reset`` 必须真的撤销会话授权。

    此前 ``reset_session_permission_rules`` 只有定义、没有任何 CLI 入口：
    一次误点的「会话始终允许」会一直生效到进程重启。
    """
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy_dir = workspace / ".sayacode"
    policy_dir.mkdir()
    write_private_json(policy_dir / "permissions.json", {
        "default": "ask",
        "tools": {"write_file": "ask"},
    })
    reset_session_permission_rules()
    try:
        runtime = RuntimeContext(
            workspace=workspace,
            model_type="ollama",
            model_name="unit",
            model_config={},
        )
        runtime.permissions = create_permission_runtime(workspace)
        runtime.permissions.set_confirm_callback(None)
        router = build_default_command_router()

        # 等价于用户在确认弹窗里点了「本次会话始终允许」
        runtime.permissions.update_session_rules({"write_file": "allow"}, source="session")
        assert runtime.permissions.check("write_file", {"path": "a.txt"}).allowed is True

        assert router.dispatch("/permissions reset", runtime) is True

        assert runtime.permissions.session_rules == {}
        # 授权撤销后回落到策略文件的 ask，且无回调 → 拒绝
        fallback = runtime.permissions.check("write_file", {"path": "a.txt"})
        assert fallback.action == "ask"
        assert fallback.allowed is False
    finally:
        reset_session_permission_rules()


def test_permissions_reset_keeps_mode_rules(tmp_path, monkeypatch):
    """``/permissions reset`` 只撤会话授权，不能顺手清掉 mode 的只读约束。"""
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    reset_session_permission_rules()
    try:
        runtime = RuntimeContext(
            workspace=workspace,
            model_type="ollama",
            model_name="unit",
            model_config={},
        )
        runtime.permissions = create_permission_runtime(workspace)
        runtime.permissions.set_confirm_callback(None)
        apply_agent_mode_permissions("plan", runtime=runtime.permissions)
        runtime.permissions.update_session_rules({"execute_command_tool": "allow"}, source="session")

        assert build_default_command_router().dispatch("/permissions reset", runtime) is True

        assert runtime.permissions.session.session_rules == {}
        assert runtime.permissions.session.mode_rules, "mode 规则必须保留"
        assert runtime.permissions.check("write_file", {"path": "a.txt"}).action == "deny"
    finally:
        reset_session_permission_rules()
