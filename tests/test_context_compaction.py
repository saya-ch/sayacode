"""上下文压缩产物（摘要、边界标记）必须进入模型 prompt。

背景：压缩把被汇总的轮次从 session.messages 删除，替换为 role="system" 的摘要。
而 PromptBuilder 曾用 get_messages(include_system=False) 取历史，把摘要一并过滤，
导致压缩等价于静默丢弃历史——既无原文、也无摘要、且无任何报错。
"""

import pytest
from langchain_core.messages import SystemMessage

from lib.core.agent_runtime import PromptBuilder
from lib.core.context import ProjectContext
from lib.core.context_packager import ContextPackager
from lib.core.session import SessionManager


def _session_with_compaction(tmp_path, rounds: int = 12) -> SessionManager:
    session = SessionManager(enable_summary=True, session_id="compact-test")
    for index in range(rounds):
        session.add_message("user", f"USER-TURN-{index} " + "x" * 4000)
        session.add_message("assistant", f"ASSIST-TURN-{index} " + "y" * 4000)
    summary = session.compact()
    assert "压缩" in summary
    return session


def test_compaction_creates_system_summary(tmp_path):
    """前置条件：压缩确实产出 system 角色的摘要。"""
    session = _session_with_compaction(tmp_path)

    system_messages = [m for m in session.messages if m.role == "system"]

    assert system_messages, "压缩应产出 summary/boundary 消息"
    assert any(m.metadata.get("compressed") for m in system_messages)


def test_default_history_view_still_hides_system_messages(tmp_path):
    """默认语义不变：include_system=False 不应把系统消息混进普通历史视图。"""
    session = _session_with_compaction(tmp_path)

    history = session.get_messages(include_system=False)

    assert history
    assert all(m["role"] != "system" for m in history)


def test_compaction_summaries_are_retained_on_request(tmp_path):
    """显式要求时必须保留压缩摘要，否则压缩等于丢弃历史。"""
    session = _session_with_compaction(tmp_path)

    history = session.get_messages(
        include_system=False,
        include_compaction_summaries=True,
    )

    system_entries = [m for m in history if m["role"] == "system"]
    assert system_entries, "压缩摘要必须可被取回"
    assert all(m["metadata"].get("compressed") for m in system_entries)

    joined = " ".join(str(m.get("content") or "") for m in history)
    assert "早期对话摘要" in joined or "摘要" in joined


def test_prompt_builder_includes_compaction_summary(tmp_path):
    """端到端：PromptBuilder 构建的消息里必须含压缩摘要。"""
    session = _session_with_compaction(tmp_path)
    builder = PromptBuilder(
        workspace=tmp_path,
        project_context=ProjectContext(tmp_path),
        prompt_style="standard",
        agent_mode="build",
        context_packager=ContextPackager(),
    )

    messages = builder.build_messages(
        effective_input="继续",
        session=session,
        system_prompt="SYSTEM-PROMPT",
        include_context=False,
    )

    system_texts = [m.content for m in messages if isinstance(m, SystemMessage)]
    assert any("SYSTEM-PROMPT" in text for text in system_texts)
    assert any(
        "摘要" in text or "上下文压缩" in text
        for text in system_texts
    ), "压缩摘要必须进入 prompt，否则压缩会静默丢弃历史"


def test_prompt_builder_does_not_duplicate_system_prompt(tmp_path):
    """原始系统提示词每轮重建，不应从历史重复注入。"""
    session = _session_with_compaction(tmp_path)
    builder = PromptBuilder(
        workspace=tmp_path,
        project_context=ProjectContext(tmp_path),
        prompt_style="standard",
        agent_mode="build",
        context_packager=ContextPackager(),
    )

    messages = builder.build_messages(
        effective_input="继续",
        session=session,
        system_prompt="SYSTEM-PROMPT",
        include_context=False,
    )

    occurrences = sum(
        1 for m in messages if isinstance(m, SystemMessage) and "SYSTEM-PROMPT" in m.content
    )
    assert occurrences == 1


# ── 超限恢复必须用 force_compact ────────────────────────────────────────────────


def _recovery_agent(session):
    """构造只带恢复路径所需属性的 SAIAgent（不跑 __init__，避免真实模型依赖）。"""
    from lib.agent import SAIAgent

    agent = object.__new__(SAIAgent)
    agent.session = session
    agent._recovery_state = {}
    return agent


def test_force_compact_session_prefers_force_compact():
    """存在 force_compact 时必须用它；compact() 在轮数不足时谎报成功。"""
    called = []

    class _Session:
        def force_compact(self, reason=""):
            called.append(("force_compact", reason))
            return "forced"

        def compact(self, focus=None):
            called.append(("compact", focus))
            return "普通压缩"

    agent = _recovery_agent(_Session())
    agent._force_compact_session()

    assert called == [("force_compact", "prompt_too_long")]
    assert agent._recovery_state["compact_api"] == "force_compact"


def test_force_compact_session_falls_back_to_compact_without_silent_failure():
    """自定义 session 无 force_compact 时降级到 compact，并如实记录实际路径。"""
    called = []

    class _LegacySession:
        def compact(self, focus=None):
            called.append(("compact", focus))
            return "普通压缩"

    agent = _recovery_agent(_LegacySession())
    agent._force_compact_session()

    assert called == [("compact", None)]
    assert agent._recovery_state["compact_api"] == "compact_fallback"


def test_force_compact_session_propagates_error_for_caller_to_report():
    """压缩不可用时错误应向上抛，由调用方决定如何提示，而不是在这里静默吞掉。"""

    class _BrokenSession:
        def force_compact(self, reason=""):
            raise RuntimeError("压缩不可用")

    agent = _recovery_agent(_BrokenSession())

    with pytest.raises(RuntimeError):
        agent._force_compact_session()

