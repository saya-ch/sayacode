from lib.core.modes import (
    agent_mode_label,
    apply_agent_mode_permissions,
    get_agent_mode_prompt_overlay,
    normalize_agent_mode,
)
from lib.core.permissions import enforce_tool_permission, set_permission_confirm_callback
from lib.state import UserConfig


def test_mode_aliases_and_labels():
    assert normalize_agent_mode("规划") == "plan"
    assert normalize_agent_mode("review") == "review"
    assert normalize_agent_mode("开发") == "build"
    assert agent_mode_label("plan") == "规划"


def test_plan_mode_denies_mutating_tools():
    set_permission_confirm_callback(None)
    try:
        apply_agent_mode_permissions("plan")

        result = enforce_tool_permission("write_file", {"path": "x.txt"})

        assert result is not None
        assert "Permission denied" in result
        assert "mode:plan" in result
    finally:
        apply_agent_mode_permissions("build")


def test_build_mode_restores_default_permission_policy(tmp_path):
    from lib.core.permissions import configure_permission_workspace

    configure_permission_workspace(tmp_path)
    set_permission_confirm_callback(None)
    apply_agent_mode_permissions("build")

    result = enforce_tool_permission("write_file", {"path": "x.txt"})

    assert result is None


def test_review_mode_prompt_overlay_is_read_only():
    overlay = get_agent_mode_prompt_overlay("review")

    assert "Review" in overlay
    assert "只读审查模式" in overlay


def test_user_config_round_trips_agent_mode():
    config = UserConfig.from_dict({"agent_mode": "只读"})

    assert config.agent_mode == "plan"
    assert config.to_dict()["agent_mode"] == "plan"
