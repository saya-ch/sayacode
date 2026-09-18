# 工具结果 artifact 契约：声明的形状、校验，以及审计这个真实消费者。

import logging
from pathlib import Path

from langchain_core.messages import AIMessage

from lib.core.audit import AuditLogService
from lib.core.tool_result import (
    OUTCOMES,
    build_tool_artifact,
    validate_tool_artifact,
)

from tests.test_agent_run import _agent


class TestBuilder:
    def test_drops_none_fields(self):
        artifact = build_tool_artifact("t", "ok", chars=None, spill_path="p")
        assert artifact == {"tool": "t", "outcome": "ok", "spill_path": "p"}

    def test_accepts_every_declared_outcome(self):
        for outcome in OUTCOMES:
            assert validate_tool_artifact(build_tool_artifact("t", outcome)) == []


class TestValidator:
    def test_empty_artifacts_are_valid(self):
        assert validate_tool_artifact(None) == []
        assert validate_tool_artifact({}) == []

    def test_valid_artifact_passes(self):
        assert validate_tool_artifact(
            {"tool": "t", "outcome": "spilled", "chars": 5, "spill_path": "p"}
        ) == []

    def test_unknown_extra_fields_are_allowed(self):
        assert validate_tool_artifact({"outcome": "ok", "future": 1}) == []

    def test_non_dict_is_rejected(self):
        problems = validate_tool_artifact(["nope"])
        assert problems and "dict" in problems[0]

    def test_wrong_types_are_reported(self):
        problems = validate_tool_artifact({"chars": "5", "spill_path": 3})
        assert len(problems) == 2

    def test_bool_is_not_an_int(self):
        problems = validate_tool_artifact({"chars": True})
        assert problems and "bool" in problems[0]

    def test_unknown_outcome_is_reported(self):
        problems = validate_tool_artifact({"outcome": "bogus"})
        assert problems and "outcome" in problems[0]


class TestSpillProducesContract:
    def test_spilled_artifact_validates(self, tmp_path, monkeypatch):
        from lib.core.mcp_runtime import _spill_oversized_result
        from lib.tools.file_tools import set_default_workspace

        set_default_workspace(tmp_path)
        text = "x" * 25000
        _content, artifact = _spill_oversized_result(text, "mcp_s_tool")
        assert validate_tool_artifact(artifact) == []
        assert artifact["outcome"] == "spilled" and artifact["tool"] == "mcp_s_tool"
        assert artifact["chars"] == len(text)
        assert Path(artifact["spill_path"]).is_file()


class TestAuditConsumesContract:
    def test_audit_records_the_artifact(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
        from lib.core.middleware import SayaHookMiddleware

        SayaHookMiddleware._audit(
            "read_file", {}, allowed=True,
            artifact={"tool": "read_file", "outcome": "ok", "chars": 3},
            trace_id="tr-art",
        )
        events = [e for e in AuditLogService().read_by_trace("tr-art")
                  if e["type"] == "tool"]
        assert events[-1]["details"]["artifact"]["outcome"] == "ok"

    def test_malformed_artifact_is_warned_but_still_recorded(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
        from lib.core.middleware import SayaHookMiddleware

        with caplog.at_level(logging.WARNING):
            SayaHookMiddleware._audit(
                "t", {}, allowed=True, artifact={"outcome": "bogus", "chars": "x"},
                trace_id="tr-bad",
            )
        assert "不合契约" in caplog.text
        events = [e for e in AuditLogService().read_by_trace("tr-bad")
                  if e["type"] == "tool"]
        assert events[-1]["details"]["artifact"]["outcome"] == "bogus"

    def test_agent_tool_call_carries_the_contract(self, tmp_path, monkeypatch):
        """端到端：真跑一次工具调用，审计里的 artifact 必须合规。"""
        monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
        script = [
            AIMessage(content="", tool_calls=[{
                "name": "echo_tool", "args": {"text": "hi"}, "id": "c1", "type": "tool_call",
            }]),
            AIMessage(content="done"),
        ]
        assert _agent(tmp_path, script).run("go") == "done"

        events = []
        for trace in AuditLogService().list_recent_traces(limit=5):
            events.extend(AuditLogService().read_by_trace(trace["trace_id"]))
        calls = [e for e in events if e["type"] == "tool" and e["action"] == "echo_tool"]
        assert calls, "工具调用必须落进审计"
        artifact = calls[-1]["details"]["artifact"]
        assert validate_tool_artifact(artifact) == []
        assert artifact["outcome"] == "ok"

class TestToolInventory:
    def test_every_tool_declares_sane_metadata(self, tmp_path):
        """广度回归：新增工具必须带名称、描述与参数 schema，且名称唯一。"""
        from types import SimpleNamespace

        from lib.tools.registry import ToolFactory

        context = SimpleNamespace(
            workspace=tmp_path, permissions=None, hooks=None, agent_mode="build"
        )
        tools = ToolFactory(context)
        assert tools, "工具工厂必须至少产出一个工具"
        names = [str(getattr(tool, "name", "")) for tool in tools]
        assert all(names), "工具名不能为空"
        assert len(set(names)) == len(names), f"工具名重复: {names}"
        for tool in tools:
            assert str(getattr(tool, "description", "") or "").strip(), f"{tool.name} 缺描述"
            assert getattr(tool, "args_schema", None) is not None, f"{tool.name} 缺参数 schema"
