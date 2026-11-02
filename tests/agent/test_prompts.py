"""系统提示和子任务消息的行为契约。"""

from __future__ import annotations

import pytest

from sayacode.prompts import (
    AgentRole,
    PromptPreferences,
    build_delegated_task_prompt,
    build_system_prompt,
)


def test_main_prompt_has_evidence_execution_and_delivery_contracts() -> None:
    prompt = build_system_prompt(
        r"C:\work\demo",
        PromptPreferences(language="zh"),
    )

    assert "Role: main" in prompt
    assert "Do not guess facts that tools can verify" in prompt
    assert "use the todo tool as the single plan" in prompt
    assert "run focused validation" in prompt
    assert "what changed, why, how it was verified" in prompt
    assert "Respond in Simplified Chinese" in prompt
    assert "Response language" in prompt
    assert "Answer briefly" not in prompt
    assert "Project instructions" not in prompt


@pytest.mark.parametrize(
    ("role", "required"),
    [
        ("builder", "never claim isolation that is not present"),
        ("planner", "concrete, ordered implementation plan"),
        ("reviewer", "actionable findings with file evidence"),
    ],
)
def test_child_role_is_stable_system_context(role: AgentRole, required: str) -> None:
    prompt = build_system_prompt("/workspace", role=role)

    assert f"Role: {role}" in prompt
    assert required in prompt


def test_project_instructions_are_delimited_and_kept_last() -> None:
    project = "Run project-specific checks.\nPrefer local fixtures."
    prompt = build_system_prompt("/workspace", project_instructions=project)

    assert f"<project_instructions>\n{project}\n</project_instructions>" in prompt
    assert prompt.endswith("</project_instructions>")


def test_delegated_message_contains_task_and_snapshot_without_role_boilerplate() -> None:
    message = build_delegated_task_prompt(
        "Inspect the parser",
        {"user_goal": "repair parsing", "parent_plan": [{"content": "verify"}]},
    )

    assert "## Delegated task\n\nInspect the parser" in message
    assert "## Delegation context snapshot" in message
    assert '"user_goal": "repair parsing"' in message
    assert "You are the builder" not in message
    assert "Work only on the delegated objective" not in message


@pytest.mark.parametrize("role", ["main", "builder", "planner", "reviewer"])
def test_prompt_protects_existing_public_api_and_existing_tests(role: AgentRole) -> None:
    """回归护栏：删除这两条规则曾导致官方基准任务因破坏既有接口而失败。

    真实事故：qutebrowser 任务要求新增 `KeyInfo.to_qt`，Agent 却把既有
    `KeyInfo.to_int` 改名并同步改写了既有测试；官方验证器重置测试面后，
    必需测试 `test_key_info_to_int` 缺失，reward 由 1 降为 0。
    """

    prompt = build_system_prompt("/workspace", role=role)

    assert "Never rename or delete an existing public function" in prompt
    assert "keep the old entry point working" in prompt
    assert "Never edit an existing test to make a change pass" in prompt
    assert "never rename its test functions" in prompt


def test_main_prompt_has_operational_dispatch_trigger() -> None:
    """回归护栏：主 Agent 需要可操作的派发触发条件，否则协作能力形同虚设。

    真实证据：22 次权威 SWE-bench Pro 试验中，team 工具全部可用，
    但没有任何一次自主派发子 Agent（task_count=0）。
    """

    prompt = build_system_prompt("/workspace", role="main")

    assert "Dispatch trigger" in prompt
    assert "two or more areas" in prompt
    assert "dispatch bounded read-only subagents" in prompt
    assert "Do not delegate a single small fix" in prompt
    # 触发规则只属于主 Agent；子 Agent 不得再派发。
    for role in ("builder", "planner", "reviewer"):
        child = build_system_prompt("/workspace", role=role)
        assert "Dispatch trigger" not in child


def test_prompt_hides_dispatch_trigger_when_team_is_disabled() -> None:
    """team_enabled=False 时提示不得再教模型派发，必须与真实工具集一致。

    真实事故：single 会话没有团队工具，提示却仍写「可派发/应派发」，
    模型浪费多次 list_skills 与 shell 探测去找派发能力，并有谎称已派发的风险。
    """

    prompt = build_system_prompt("/workspace", role="main", team_enabled=False)

    assert "Dispatch trigger" not in prompt
    assert "dispatch bounded read-only subagents" not in prompt
    assert "Do not delegate a single small fix" not in prompt
    assert "no team tools" in prompt
    assert "never claim you dispatched one" in prompt


def test_prompt_keeps_dispatch_trigger_when_team_is_enabled() -> None:
    prompt = build_system_prompt("/workspace", role="main", team_enabled=True)

    assert "Dispatch trigger" in prompt
    assert "no team tools" not in prompt


def test_child_roles_are_unaffected_by_team_flag() -> None:
    for role in ("builder", "planner", "reviewer"):
        prompt = build_system_prompt("/workspace", role=role, team_enabled=False)
        assert "Dispatch trigger" not in prompt
        assert "no team tools" not in prompt


