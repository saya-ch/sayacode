"""Protocol contracts through official LangChain adapters and local HTTP servers.

These tests deliberately inspect requests at the wire boundary. They use no
provider credentials or internet access, and exercise the actual adapters that
SAYACODE constructs for each explicit protocol.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Iterator

import anthropic
import ollama
import openai
import pytest
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import ToolMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_google_genai.chat_models import GoogleAuthenticationError
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI

from sayacode.agent import AgentRuntime
from sayacode.agent.models import model_for
from sayacode.application import SayacodeApp
from sayacode.config import Config, ConfigRepository, Profile
from sayacode.paths import AppPaths

DUMMY_KEY = "local-test-key-not-a-credential"


@dataclass
class WireReply:
    body: dict[str, Any] | str
    content_type: str = "application/json"
    status: int = 200


@dataclass
class WireRequest:
    path: str
    headers: dict[str, str]
    body: dict[str, Any]


@contextmanager
def local_provider(replies: list[WireReply]) -> Iterator[tuple[str, list[WireRequest]]]:
    requests: list[WireRequest] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            requests.append(
                WireRequest(
                    self.path,
                    dict(self.headers),
                    json.loads(self.rfile.read(length)),
                )
            )
            if len(requests) > len(replies):
                self.send_error(500, "unexpected provider request")
                return
            reply = replies[len(requests) - 1]
            data = (
                json.dumps(reply.body).encode()
                if isinstance(reply.body, dict)
                else reply.body.encode()
            )
            self.send_response(reply.status)
            self.send_header("Content-Type", reply.content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def profile(protocol: str, base_url: str, *, api_key: str | None = DUMMY_KEY) -> Profile:
    return Profile(
        name="local-contract",
        protocol=protocol,
        base_url=base_url,
        api_key=api_key,
        model_id="wire-model",
        context_length=8192,
        max_output_tokens=128,
    )


def response(output: list[dict[str, Any]], *, number: int = 1) -> dict[str, Any]:
    return {
        "id": f"resp_{number}",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "wire-model",
        "output": output,
        "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
    }


def anthropic_message(content: list[dict[str, Any]], *, number: int = 1) -> dict[str, Any]:
    return {
        "id": f"msg_{number}",
        "type": "message",
        "role": "assistant",
        "model": "wire-model",
        "content": content,
        "stop_reason": "tool_use" if content[0]["type"] == "tool_use" else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }


def gemini_content(parts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {"role": "model", "parts": parts},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 4,
            "candidatesTokenCount": 2,
            "totalTokenCount": 6,
        },
        "modelVersion": "wire-model",
    }


def sse(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps({'type': name, **data})}\n\n"


async def test_chat_completions_protocol_uses_chat_endpoint() -> None:
    completion = {
        "id": "chatcmpl-wire",
        "object": "chat.completion",
        "created": 1,
        "model": "wire-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    }
    with local_provider([WireReply(completion)]) as (url, requests):
        model = model_for(profile("openai_chat_completions", url + "/custom/v1"))
        assert isinstance(model, ChatOpenAI)
        assert model.use_responses_api is False
        answer = await model.ainvoke("Say hello")
    assert answer.content == "hello"
    assert answer.usage_metadata["total_tokens"] == 6
    assert requests[0].path == "/custom/v1/chat/completions"
    assert {key.lower(): value for key, value in requests[0].headers.items()}[
        "authorization"
    ] == f"Bearer {DUMMY_KEY}"
    assert requests[0].body["model"] == "wire-model"
    assert requests[0].body["max_completion_tokens"] == 128


async def test_keyless_custom_endpoint_never_receives_ambient_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-private-key-must-not-leak")
    completion = {
        "id": "chatcmpl-keyless",
        "object": "chat.completion",
        "created": 1,
        "model": "wire-model",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    with local_provider([WireReply(completion)]) as (url, requests):
        model = model_for(profile("openai_chat_completions", url + "/v1", api_key=None))
        answer = await model.ainvoke("ping")
    assert answer.content == "ok"
    headers = {key.lower(): value for key, value in requests[0].headers.items()}
    assert headers["authorization"] == "Bearer sayacode-keyless-endpoint"
    assert "sk-private-key-must-not-leak" not in str(requests[0])


async def test_responses_api_tool_roundtrip_uses_responses_wire_format() -> None:
    seen: list[str] = []

    @tool
    def lookup_symbol(name: str) -> str:
        """Look up a symbol in the local test catalog."""
        seen.append(name)
        return "src/main.py:7"

    first = response(
        [
            {
                "id": "fc_wire",
                "type": "function_call",
                "call_id": "call_wire",
                "name": "lookup_symbol",
                "arguments": '{"name":"main"}',
                "status": "completed",
            }
        ]
    )
    second = response(
        [
            {
                "id": "msg_wire",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Found main", "annotations": []}],
            }
        ],
        number=2,
    )
    with local_provider([WireReply(first), WireReply(second)]) as (url, requests):
        model = model_for(profile("openai_responses", url + "/custom/v1"))
        assert isinstance(model, ChatOpenAI)
        assert model.use_responses_api is True
        result = await create_agent(model, [lookup_symbol]).ainvoke(
            {"messages": [{"role": "user", "content": "Find main"}]}
        )
    assert seen == ["main"]
    assert len(requests) == 2
    assert [item.path for item in requests] == [
        "/custom/v1/responses",
        "/custom/v1/responses",
    ]
    assert {key.lower(): value for key, value in requests[0].headers.items()}[
        "authorization"
    ] == f"Bearer {DUMMY_KEY}"
    assert requests[0].body["model"] == "wire-model"
    assert requests[0].body["max_output_tokens"] == 128
    assert requests[0].body["tools"][0]["name"] == "lookup_symbol"
    assert requests[1].body["input"][-1] == {
        "type": "function_call_output",
        "output": "src/main.py:7",
        "call_id": "call_wire",
    }
    assert any(
        isinstance(message, ToolMessage) and message.tool_call_id == "call_wire"
        for message in result["messages"]
    )
    assert result["messages"][-1].usage_metadata["total_tokens"] == 6


async def test_responses_api_sse_emits_text_and_usage() -> None:
    finished = response(
        [
            {
                "id": "msg_wire",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "hello", "annotations": []}],
            }
        ]
    )
    stream = (
        sse(
            "response.created",
            {"response": {**finished, "output": [], "status": "in_progress", "usage": None}},
        )
        + sse(
            "response.output_item.added",
            {
                "output_index": 0,
                "item": {
                    "id": "msg_wire",
                    "type": "message",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [],
                },
            },
        )
        + sse(
            "response.content_part.added",
            {
                "output_index": 0,
                "content_index": 0,
                "item_id": "msg_wire",
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
        )
        + sse(
            "response.output_text.delta",
            {"output_index": 0, "content_index": 0, "item_id": "msg_wire", "delta": "hello"},
        )
        + sse("response.completed", {"response": finished})
    )
    with local_provider([WireReply(stream, "text/event-stream")]) as (url, requests):
        model = model_for(profile("openai_responses", url + "/custom/v1"))
        chunks = [chunk async for chunk in model.astream("Say hello")]
    assert requests[0].path == "/custom/v1/responses"
    assert requests[0].body["stream"] is True
    assert any(
        block.get("text") == "hello"
        for chunk in chunks
        if isinstance(chunk.content, list)
        for block in chunk.content
        if isinstance(block, dict)
    )
    assert any(
        chunk.usage_metadata and chunk.usage_metadata["total_tokens"] == 6 for chunk in chunks
    )


async def test_anthropic_messages_tool_roundtrip_uses_native_blocks() -> None:
    seen: list[str] = []

    @tool
    def lookup_symbol(name: str) -> str:
        """Look up a symbol in the local test catalog."""
        seen.append(name)
        return "src/main.py:7"

    first = anthropic_message(
        [
            {
                "type": "tool_use",
                "id": "toolu_wire",
                "name": "lookup_symbol",
                "input": {"name": "main"},
            }
        ]
    )
    second = anthropic_message([{"type": "text", "text": "Found main"}], number=2)
    with local_provider([WireReply(first), WireReply(second)]) as (url, requests):
        model = model_for(profile("anthropic_messages", url))
        assert isinstance(model, ChatAnthropic)
        result = await create_agent(model, [lookup_symbol]).ainvoke(
            {"messages": [{"role": "user", "content": "Find main"}]}
        )
    assert seen == ["main"]
    assert [item.path for item in requests] == ["/v1/messages", "/v1/messages"]
    assert {key.lower(): value for key, value in requests[0].headers.items()}[
        "x-api-key"
    ] == DUMMY_KEY
    assert requests[0].body["max_tokens"] == 128
    assert requests[0].body["tools"][0]["name"] == "lookup_symbol"
    assert requests[1].body["messages"][-1]["content"][0] == {
        "type": "tool_result",
        "content": "src/main.py:7",
        "tool_use_id": "toolu_wire",
        "is_error": False,
    }
    assert any(
        isinstance(message, ToolMessage) and message.tool_call_id == "toolu_wire"
        for message in result["messages"]
    )
    assert result["messages"][-1].usage_metadata["total_tokens"] == 6


async def test_anthropic_messages_sse_emits_text_and_usage() -> None:
    initial = {
        **anthropic_message([{"type": "text", "text": ""}]),
        "content": [],
        "stop_reason": None,
    }
    initial["usage"] = {"input_tokens": 4, "output_tokens": 0}
    stream = (
        sse("message_start", {"message": initial})
        + sse("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}})
        + sse("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "hello"}})
        + sse("content_block_stop", {"index": 0})
        + sse(
            "message_delta",
            {
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 2},
            },
        )
        + sse("message_stop", {})
    )
    with local_provider([WireReply(stream, "text/event-stream")]) as (url, requests):
        model = model_for(profile("anthropic_messages", url))
        chunks = [chunk async for chunk in model.astream("Say hello")]
    assert requests[0].path == "/v1/messages"
    assert requests[0].body["stream"] is True
    assert "".join(chunk.content for chunk in chunks if isinstance(chunk.content, str)) == "hello"
    assert any(
        chunk.usage_metadata and chunk.usage_metadata["output_tokens"] == 2 for chunk in chunks
    )


async def test_gemini_native_tool_roundtrip_uses_generate_content() -> None:
    seen: list[str] = []

    @tool
    def lookup_symbol(name: str) -> str:
        """Look up a symbol in the local test catalog."""
        seen.append(name)
        return "src/main.py:7"

    first = gemini_content([{"functionCall": {"name": "lookup_symbol", "args": {"name": "main"}}}])
    second = gemini_content([{"text": "Found main"}])
    with local_provider([WireReply(first), WireReply(second)]) as (url, requests):
        model = model_for(profile("gemini_generate_content", url))
        assert isinstance(model, ChatGoogleGenerativeAI)
        result = await create_agent(model, [lookup_symbol]).ainvoke(
            {"messages": [{"role": "user", "content": "Find main"}]}
        )
    assert seen == ["main"]
    assert [item.path for item in requests] == [
        "/v1beta/models/wire-model:generateContent",
        "/v1beta/models/wire-model:generateContent",
    ]
    assert {key.lower(): value for key, value in requests[0].headers.items()}[
        "x-goog-api-key"
    ] == DUMMY_KEY
    assert requests[0].body["generationConfig"]["maxOutputTokens"] == 128
    assert requests[0].body["tools"][0]["functionDeclarations"][0]["name"] == "lookup_symbol"
    assert requests[1].body["contents"][-1]["parts"][0] == {
        "functionResponse": {"name": "lookup_symbol", "response": {"output": "src/main.py:7"}}
    }
    assert any(isinstance(message, ToolMessage) for message in result["messages"])
    assert result["messages"][-1].usage_metadata["total_tokens"] == 6


async def test_gemini_native_sse_emits_text_and_usage() -> None:
    stream = "data: " + json.dumps(gemini_content([{"text": "hello"}])) + "\n\n"
    with local_provider([WireReply(stream, "text/event-stream")]) as (url, requests):
        model = model_for(profile("gemini_generate_content", url))
        chunks = [chunk async for chunk in model.astream("Say hello")]
    assert requests[0].path == "/v1beta/models/wire-model:streamGenerateContent?alt=sse"
    assert "".join(chunk.content for chunk in chunks if isinstance(chunk.content, str)) == "hello"
    assert any(
        chunk.usage_metadata and chunk.usage_metadata["total_tokens"] == 6 for chunk in chunks
    )


def ollama_message(content: str, *, tool_call: bool = False) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_call:
        message["tool_calls"] = [
            {"function": {"name": "lookup_symbol", "arguments": {"name": "main"}}}
        ]
    return {
        "model": "wire-model",
        "created_at": "2024-01-01T00:00:00Z",
        "message": message,
        "done": True,
        "done_reason": "stop",
        "total_duration": 1,
        "load_duration": 1,
        "prompt_eval_count": 4,
        "prompt_eval_duration": 1,
        "eval_count": 2,
        "eval_duration": 1,
    }


async def test_ollama_native_tool_roundtrip_uses_api_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OLLAMA_API_KEY", "private-ambient-ollama-key")
    seen: list[str] = []

    @tool
    def lookup_symbol(name: str) -> str:
        """Look up a symbol in the local test catalog."""
        seen.append(name)
        return "src/main.py:7"

    replies = [
        WireReply(json.dumps(ollama_message("", tool_call=True)) + "\n", "application/x-ndjson"),
        WireReply(json.dumps(ollama_message("Found main")) + "\n", "application/x-ndjson"),
    ]
    with local_provider(replies) as (url, requests):
        model = model_for(profile("ollama_native_chat", url, api_key=None))
        assert isinstance(model, ChatOllama)
        result = await create_agent(model, [lookup_symbol]).ainvoke(
            {"messages": [{"role": "user", "content": "Find main"}]}
        )
    assert seen == ["main"]
    assert [item.path for item in requests] == ["/api/chat", "/api/chat"]
    headers = {key.lower(): value for key, value in requests[0].headers.items()}
    assert headers["authorization"] == "Bearer sayacode-keyless-endpoint"
    assert "private-ambient-ollama-key" not in str(requests[0])
    assert requests[0].body["model"] == "wire-model"
    assert requests[0].body["options"]["num_predict"] == 128
    assert requests[0].body["options"]["num_ctx"] == 8192
    assert requests[0].body["tools"][0]["function"]["name"] == "lookup_symbol"
    assert requests[1].body["messages"][-1] == {"role": "tool", "content": "src/main.py:7"}
    assert any(isinstance(message, ToolMessage) for message in result["messages"])
    assert result["messages"][-1].usage_metadata["total_tokens"] == 6


async def test_ollama_native_uses_explicit_api_key_for_authenticated_endpoint() -> None:
    reply = WireReply(json.dumps(ollama_message("ready")) + "\n", "application/x-ndjson")
    with local_provider([reply]) as (url, requests):
        model = model_for(profile("ollama_native_chat", url))
        answer = await model.ainvoke("ping")
    assert answer.content == "ready"
    headers = {key.lower(): value for key, value in requests[0].headers.items()}
    assert headers["authorization"] == f"Bearer {DUMMY_KEY}"


async def test_ollama_native_stream_preserves_text_and_usage() -> None:
    stream = json.dumps(ollama_message("hello")) + "\n"
    with local_provider([WireReply(stream, "application/x-ndjson")]) as (url, requests):
        model = model_for(profile("ollama_native_chat", url, api_key=None))
        chunks = [chunk async for chunk in model.astream("Say hello")]
    assert requests[0].path == "/api/chat"
    assert requests[0].body["stream"] is True
    assert "".join(chunk.content for chunk in chunks if isinstance(chunk.content, str)) == "hello"
    assert any(
        chunk.usage_metadata and chunk.usage_metadata["total_tokens"] == 6 for chunk in chunks
    )


@pytest.mark.parametrize(
    ("protocol", "error_body", "error_type"),
    [
        (
            "openai_responses",
            {"error": {"message": "invalid test key", "type": "authentication_error"}},
            openai.AuthenticationError,
        ),
        (
            "anthropic_messages",
            {
                "type": "error",
                "error": {"type": "authentication_error", "message": "invalid test key"},
            },
            anthropic.AuthenticationError,
        ),
        (
            "gemini_generate_content",
            {"error": {"code": 401, "message": "invalid test key", "status": "UNAUTHENTICATED"}},
            GoogleAuthenticationError,
        ),
        ("ollama_native_chat", {"error": "invalid test key"}, ollama.ResponseError),
    ],
)
async def test_native_protocol_auth_errors_propagate(
    protocol: str, error_body: dict[str, Any], error_type: type[Exception]
) -> None:
    with local_provider([WireReply(error_body, status=401)]) as (url, requests):
        model = model_for(
            profile(protocol, url, api_key=None if protocol == "ollama_native_chat" else DUMMY_KEY)
        )
        with pytest.raises(error_type):
            await model.ainvoke("Authenticate")
    assert len(requests) == 1


async def test_anthropic_parent_notification_stays_in_single_system_prompt(
    tmp_path: Path,
) -> None:
    first = anthropic_message([{"type": "text", "text": "Initial response"}])
    second = anthropic_message([{"type": "text", "text": "Parent continued"}], number=2)
    with local_provider([WireReply(first), WireReply(second)]) as (url, requests):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        paths = AppPaths.resolve(tmp_path / "state")
        chosen = profile("anthropic_messages", url)
        chosen.model_retries = 0
        chosen.file_search = False
        config = Config(default_profile=chosen.name, profiles={chosen.name: chosen})
        runtime = await AgentRuntime.open(paths.home)
        app = SayacodeApp(
            paths=paths,
            repository=ConfigRepository(paths.home),
            config=config,
            runtime=runtime,
            workspace=workspace,
            session_id="parent-thread",
            trust_level="ask",
            profile_name=chosen.name,
        )
        await app.initialize()
        try:
            assert (await app.run("Start the task"))["ok"] is True
            event_id = "child-contract:1"
            await runtime.store.aput(
                ("sayacode", "parent_events"),
                event_id,
                {
                    "event_id": event_id,
                    "parent_thread_id": app.session_id,
                    "task_id": "child-contract",
                    "role": "reviewer",
                    "status": "completed",
                    "state": "pending",
                    "created_at": "2026-01-01T00:00:00Z",
                },
                index=False,
            )
            await app._schedule_pending_wakes(app.session_id)
            await app.wait_for_tasks()
            stored = await runtime.store.aget(("sayacode", "parent_events"), event_id)
            assert stored.value["state"] == "delivered"
        finally:
            await app.aclose()
    assert len(requests) == 2
    assert "SAYACODE internal background-task event" in str(requests[1].body["system"])
    assert requests[1].body["messages"][-1]["role"] == "assistant"
