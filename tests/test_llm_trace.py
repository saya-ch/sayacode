# 模型调用可观测：每次模型调用的耗时与 token 用量进审计树。

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from lib.core.audit import AuditLogService
from lib.core.tracing import ModelCallTraceHandler, trace_session

from tests.test_agent_run import _agent


def _result(content="ok", usage=None, llm_output=None):
    # on_llm_end 收到的是 LLMResult：generations 是「每个输入一组」的嵌套列表。
    message = AIMessage(content=content, usage_metadata=usage)
    return LLMResult(
        generations=[[ChatGeneration(message=message)]],
        llm_output=llm_output,
    )
class TestUsageExtraction:
    def test_reads_usage_metadata(self):
        handler = ModelCallTraceHandler()
        usage = handler._usage_of(_result(usage={"input_tokens": 3, "output_tokens": 4,
                                             "total_tokens": 7}))
        assert usage == {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}

    def test_falls_back_to_llm_output(self):
        handler = ModelCallTraceHandler()
        usage = handler._usage_of(_result(llm_output={"token_usage": {"input_tokens": 9}}))
        assert usage == {"input_tokens": 9}

    def test_missing_usage_is_empty(self):
        assert ModelCallTraceHandler()._usage_of(_result()) == {}


class TestModelCallEvents:
    def test_records_duration_and_tokens(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
        handler = ModelCallTraceHandler("unit-model")
        with trace_session("tr-llm"):
            handler.on_chat_model_start({}, [[AIMessage(content="q")]], run_id="r1")
            handler.on_llm_end(
                _result(usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}),
                run_id="r1",
            )
    
        events = AuditLogService().read_by_trace("tr-llm")
        llm = [e for e in events if e["type"] == "llm"]
        assert len(llm) == 1
        details = llm[0]["details"]
        assert details["input_tokens"] == 11 and details["output_tokens"] == 7
        assert details["messages"] == 1
        assert details["duration_ms"] >= 0
        assert llm[0]["action"] == "unit-model"

    def test_error_is_recorded_and_marked_blocked(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
        handler = ModelCallTraceHandler("m")
        with trace_session("tr-err"):
            handler.on_chat_model_start({}, [[AIMessage(content="q")]], run_id="r2")
            handler.on_llm_error(RuntimeError("boom"), run_id="r2")
    
        events = AuditLogService().read_by_trace("tr-err")
        llm = [e for e in events if e["type"] == "llm"]
        assert len(llm) == 1
        assert llm[0]["allowed"] is False
        assert llm[0]["details"]["exception_type"] == "RuntimeError"

    def test_audit_can_be_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
        handler = ModelCallTraceHandler("m", audit=False)
        with trace_session("tr-off"):
            handler.on_chat_model_start({}, [[AIMessage(content="q")]], run_id="r3")
            handler.on_llm_end(_result(), run_id="r3")
    
        assert AuditLogService().read_by_trace("tr-off") == []


class TestTurnAudit:
    def test_agent_turn_audits_model_calls(self, tmp_path, monkeypatch):
        """端到端：真跑一个 turn，模型调用必须落进同一条 trace。"""
        monkeypatch.setenv("SAYACODE_HOME", str(tmp_path / "home"))
        agent = _agent(tmp_path, [AIMessage(content="hi", usage_metadata={
            "input_tokens": 5, "output_tokens": 2, "total_tokens": 7,
        })])
        assert agent.run("hello") == "hi"
        traces = AuditLogService().list_recent_traces(limit=5)
        assert traces
        events = AuditLogService().read_by_trace(traces[0]["trace_id"])
        llm = [e for e in events if e["type"] == "llm"]
        assert llm, "模型调用必须进 turn 的调用树"
        assert llm[0]["details"]["input_tokens"] == 5


class TestRendering:
    def test_event_line_marks_model_and_tokens(self):
        from lib.commands.trace import _event_line

        line = _event_line({"type": "llm", "action": "m", "details": {
            "input_tokens": 3, "output_tokens": 4, "duration_ms": 12.5,
        }}, 0)
        assert "m" in line and "3" in line and "4" in line and "12" in line
