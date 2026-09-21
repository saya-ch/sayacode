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
        PromptPreferences(language="zh", style="concise"),
    )

    assert "Role: main" in prompt
    assert "Do not guess facts that tools can verify" in prompt
    assert "use the todo tool as the single plan" in prompt
    assert "run focused validation" in prompt
    assert "what changed, why, how it was verified" in prompt
    assert "Respond in Simplified Chinese" in prompt
    assert "Answer briefly" in prompt
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
