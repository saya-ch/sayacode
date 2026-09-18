"""运行追踪测试：trace_id 传播、span 耗时、调用树读取与 /trace 渲染。"""

from types import SimpleNamespace

from lib.core.audit import AuditLogService, append_audit_event
from lib.core.tracing import (
    current_span,
    current_trace_id,
    new_trace_id,
    span,
    span_depth,
    trace_session,
    traced,
)


def test_trace_session_assigns_and_restores():
    assert current_trace_id() == ""
    with trace_session() as first:
        assert first.startswith("tr-")
        assert current_trace_id() == first
        with trace_session() as nested:
            assert nested == first, "嵌套必须复用外层 trace_id"
        with trace_session("tr-explicit") as explicit:
            assert explicit == "tr-explicit"
        assert current_trace_id() == first
    assert current_trace_id() == ""


def test_span_records_duration_and_nesting(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    with trace_session("tr-span") as trace_id:
        with span("outer"):
            assert span_depth() == 1
            with span("inner") as inner_id:
                assert span_depth() == 2
                assert current_span()[0] == inner_id
            assert span_depth() == 1
        assert span_depth() == 0
    events = [e for e in AuditLogService().read_by_trace(trace_id) if e["type"] == "span"]
    assert [e["details"]["span"] for e in events] == ["inner", "outer"]
    for event in events:
        assert event["details"]["duration_ms"] >= 0
        assert event["details"]["status"] == "ok"
    inner_event = events[0]
    outer_event = events[1]
    assert inner_event["details"]["parent_span"] == outer_event["details"]["span_id"]
    assert "parent_span" not in outer_event["details"]


def test_span_marks_failure_and_reraises(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    with trace_session("tr-fail"):
        try:
            with span("boom"):
                raise RuntimeError("炸了")
        except RuntimeError:
            pass
    events = AuditLogService().read_by_trace("tr-fail")
    span_event = next(e for e in events if e["type"] == "span")
    assert span_event["details"]["status"] == "error"
    assert "炸了" in span_event["details"]["error"]


def test_span_can_skip_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    with trace_session("tr-quiet"):
        with span("quiet", audit=False):
            pass
    assert AuditLogService().read_by_trace("tr-quiet") == []


def test_events_in_one_trace_share_trace_id(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    with trace_session("tr-shared"):
        append_audit_event("tool", "read_file", allowed=True)
        with span("step"):
            append_audit_event("tool", "grep_search", allowed=False)
    events = AuditLogService().read_by_trace("tr-shared")
    assert [e["action"] for e in events] == ["read_file", "grep_search", "step"]
    assert all(e["trace_id"] == "tr-shared" for e in events)
    span_event = events[-1]
    assert events[1]["details"]["span_id"] == span_event["details"]["span_id"]


def test_trace_id_falls_back_without_session(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    append_audit_event("tool", "one")
    append_audit_event("tool", "two")
    events = AuditLogService().read_recent(limit=10)
    ids = [e["trace_id"] for e in events if e["action"] in {"one", "two"}]
    assert len(ids) == 2
    assert all(ids), "无 trace 上下文时也必须写入 id"
    assert ids[0] != ids[1], "无 trace 上下文时每条事件各自成 id"


def test_read_by_trace_isolates_other_traces(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    with trace_session("tr-a"):
        append_audit_event("tool", "in_a")
    with trace_session("tr-b"):
        append_audit_event("tool", "in_b")
    service = AuditLogService()
    assert [e["action"] for e in service.read_by_trace("tr-a")] == ["in_a"]
    assert [e["action"] for e in service.read_by_trace("tr-b")] == ["in_b"]
    assert service.read_by_trace("") == []
    assert service.read_by_trace("tr-missing") == []


def test_list_recent_traces_groups_and_marks_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    with trace_session("tr-ok"):
        append_audit_event("tool", "read_file", allowed=True)
    with trace_session("tr-bad"):
        append_audit_event("tool", "delete_file", allowed=False)
        append_audit_event("tool", "read_file", allowed=True)
    entries = {e["trace_id"]: e for e in AuditLogService().list_recent_traces(limit=10)}
    assert entries["tr-ok"]["failed"] is False
    assert entries["tr-bad"]["failed"] is True
    assert entries["tr-bad"]["events"] == 2
    assert sorted(entries["tr-bad"]["tools"]) == ["delete_file", "read_file"]


def test_parallel_batch_propagates_trace(tmp_path, monkeypatch):
    """并行工具在线程池里必须仍看到同一个 trace_id。"""
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    from lib.core.tool_meta import ToolMeta, register_tool_meta
    from lib.tools.batch_executor import ToolBatchExecutor, ToolCallRequest

    register_tool_meta(ToolMeta.safe_default("probe", is_concurrency_safe=True, tool_group="test"))
    seen = []

    def probe(**kwargs):
        seen.append(current_trace_id())
        append_audit_event("tool", "probe")
        return "ok"

    executor = ToolBatchExecutor(tool_map={"probe": probe})
    with trace_session("tr-parallel"):
        result = executor.execute_batch([
            ToolCallRequest(tool_name="probe", arguments={}, tool_call_id="1"),
            ToolCallRequest(tool_name="probe", arguments={}, tool_call_id="2"),
        ])
    assert all(not r.is_error for r in result.results)
    assert seen == ["tr-parallel", "tr-parallel"]
    assert len(AuditLogService().read_by_trace("tr-parallel")) == 2


def test_traced_decorator_wraps_plain_function(tmp_path, monkeypatch):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))

    @traced("unit")
    def work():
        return current_trace_id()

    inner = work()
    assert inner.startswith("tr-")
    assert current_trace_id() == "", "退出后必须恢复"
    assert AuditLogService().read_by_trace(inner)[0]["details"]["span"] == "unit"


def test_traced_decorator_covers_generator(tmp_path, monkeypatch):
    """生成器方法（stream_run 的形状）必须在迭代期间保持 trace，而非创建时。"""
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    observed = []

    @traced("stream")
    def stream():
        observed.append(current_trace_id())
        yield 1
        observed.append(current_trace_id())
        yield 2

    iterator = stream()
    assert observed == [], "生成器未迭代时不应产生副作用"
    assert list(iterator) == [1, 2]
    assert len(set(observed)) == 1 and observed[0].startswith("tr-")
    assert current_trace_id() == ""


def test_new_trace_id_is_unique():
    assert new_trace_id() != new_trace_id()
    assert new_trace_id().startswith("tr-")


def test_trace_command_lists_and_shows(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    from lib.commands.base import CommandContext
    from lib.commands.trace import TraceCommandHandler

    with trace_session("tr-cmd"):
        with span("step"):
            append_audit_event("tool", "read_file", allowed=True)

    handler = TraceCommandHandler()
    runtime = SimpleNamespace()
    assert handler.handle(CommandContext(raw="/trace", name="trace", args=""), runtime) is True
    assert handler.handle(
        CommandContext(raw="/trace tr-cmd", name="trace", args="tr-cmd"), runtime
    ) is True
    assert handler.handle(
        CommandContext(raw="/trace nope", name="trace", args="tr-missing"), runtime
    ) is True
    assert capsys.readouterr() is not None


def test_agent_turn_produces_a_complete_trace(tmp_path, monkeypatch):
    """端到端：真跑一个 turn，审计里应当出现带耗时的 turn span。"""
    monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from lib.agent import SAIAgent

    class FakeModel(BaseChatModel):
        model_name: str = "fake"
        model_type: str = "fake"
        context_window: int = 4096

        @property
        def _llm_type(self):
            return "fake"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="hi"))])

        def chat(self, messages):
            return "hi"

        def bind_tools(self, tools, **kwargs):
            return self

    agent = SAIAgent(model=FakeModel(), workspace=tmp_path / "ws")
    assert agent.run("hello") == "hi"
    traces = AuditLogService().list_recent_traces(limit=5)
    assert traces, "一次 turn 必须留下至少一条 trace"
    turn_events = AuditLogService().read_by_trace(traces[0]["trace_id"])
    spans = [e for e in turn_events if e["type"] == "span"]
    assert spans, "turn 必须产生 span 事件"
    assert spans[0]["details"]["span"] == "turn"
    assert spans[0]["details"]["duration_ms"] >= 0


def test_trace_tree_renders_nesting():
    from lib.commands.trace import _build_tree

    events = [
        {"type": "span", "action": "outer", "details": {"span_id": "s1", "span": "outer", "duration_ms": 5.0}},
        {"type": "tool", "action": "read_file", "allowed": True, "details": {"span_id": "s2", "parent_span": "s1", "duration_ms": 1.5}},
        {"type": "tool", "action": "loose", "details": {}},
    ]
    lines = _build_tree(events)
    assert len(lines) == 3
    assert "outer" in lines[1]
    assert lines[2].startswith("  ") and "read_file" in lines[2]
    assert lines[0].startswith("• ") and "loose" in lines[0]
