import io
import json
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

from lib.agent import SAIAgent
from lib.runtime.events import (
    HEADLESS_EVENT_SCHEMA_VERSION,
    JsonlEventWriter,
    extract_public_tool_events,
    public_event_identity,
)
from lib.tools.context import ToolAbortController


class FlushTrackingStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flush_count = 0

    def flush(self):
        self.flush_count += 1
        super().flush()


def test_jsonl_event_writer_versions_sequences_flushes_and_protects_envelope():
    stream = FlushTrackingStream()
    writer = JsonlEventWriter(stream, run_id="run-1")

    writer.emit("run.started", type="overridden", sequence=999, api_key="hidden")
    writer.emit("assistant.delta", delta="hello")
    events = [json.loads(line) for line in stream.getvalue().splitlines()]

    assert events[0]["type"] == "run.started"
    assert events[0]["sequence"] == 1
    assert events[0]["run_id"] == "run-1"
    assert events[0]["schema_version"] == HEADLESS_EVENT_SCHEMA_VERSION
    assert events[0]["api_key"] == "***"
    assert events[1]["sequence"] == 2
    assert stream.flush_count == 2


def test_extract_public_tool_events_supports_provider_shapes_without_reasoning():
    ai_message = AIMessage(
        content="",
        additional_kwargs={
            "reasoning_content": "never public",
            "tool_calls": [{
                "id": "call-openai",
                "type": "function",
                "function": {
                    "name": "shell_command",
                    "arguments": '{"command":"curl -H Authorization:Bearer very-secret-token"}',
                },
            }],
        },
    )
    tool_message = ToolMessage(
        content="Authorization: Bearer another-secret-token",
        name="shell_command",
        tool_call_id="call-openai",
        status="error",
    )

    events = extract_public_tool_events({
        "agent": {"messages": [ai_message]},
        "tools": {"messages": [tool_message]},
    })
    serialized = json.dumps(events, ensure_ascii=False)

    assert [event["type"] for event in events] == ["tool.started", "tool.completed"]
    assert events[0]["tool_name"] == "shell_command"
    assert events[1]["is_error"] is True
    assert "never public" not in serialized
    assert "very-secret-token" not in serialized
    assert "another-secret-token" not in serialized


def test_public_event_identity_only_deduplicates_provider_identified_calls():
    assert public_event_identity({"type": "tool.started", "tool_call_id": "call-1"}) == (
        "tool.started:call-1"
    )
    assert public_event_identity({"type": "tool.started", "tool_name": "read_file"}) == ""


def test_agent_stream_exposes_raw_chunks_and_can_suppress_display_tool_markers(tmp_path):
    chunks = [
        {"agent": {"messages": [AIMessage(
            content="",
            tool_calls=[{
                "name": "read_file",
                "args": {"path": "README.md"},
                "id": "call-1",
                "type": "tool_call",
            }],
        )]}},
        {"tools": {"messages": [ToolMessage(
            content="file contents",
            name="read_file",
            tool_call_id="call-1",
        )]}},
        {"agent": {"messages": [AIMessage(content="visible answer")]}},
    ]
    observed = []
    finished = []
    agent = object.__new__(SAIAgent)
    agent._turn_count = 0
    agent._abort_controller = ToolAbortController()
    agent._last_extra = {}
    agent._recovery_state = {}
    agent._permissions_runtime = None
    agent._hooks_runtime = None
    agent.workspace = tmp_path
    agent.agent_mode = "review"
    agent.stream_callback = None
    agent.model = SimpleNamespace()
    recorded = []
    agent.session = SimpleNamespace(
        compact=lambda: None,
        maybe_compact=lambda: None,
        add_user_message=lambda *a, **k: None,
        add_assistant_message=lambda *a, **k: recorded.append((a, k)),
    )
    agent._recorded_turns = recorded
    agent.memory = SimpleNamespace()
    agent.conversation_manager = SimpleNamespace(
        finish_turn=lambda *args, **kwargs: finished.append((args, kwargs)),
    )
    agent._prepare_messages = lambda *args, **kwargs: ("prompt", [])
    agent._iter_agent_stream = lambda messages: iter(chunks)
    agent._record_stream_usage = lambda chunk: None

    output = list(agent.stream_run(
        "prompt",
        event_callback=observed.append,
        emit_tool_status=False,
    ))

    assert observed == chunks
    assert output == ["visible answer"]
    assert len(agent._recorded_turns) == 1
