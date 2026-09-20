"""Exercise the real OpenAI integration with an entirely local HTTP transport."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
import openai
import pytest
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import AIMessage, ToolMessage
from langchain_openai import ChatOpenAI

from sayacode.config import Profile
from sayacode.runtime import AgentRuntime

BASE_URL = "https://mock-provider.invalid/custom/v1"
DUMMY_KEY = "test-only-not-a-real-credential"


def completion(message: dict[str, Any], *, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "id": "chatcmpl-local-contract",
        "object": "chat.completion",
        "created": 1,
        "model": "custom-coder",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 7,
            "total_tokens": 19,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    }


@asynccontextmanager
async def mock_provider(responses: list[httpx.Response], **options: Any):
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == BASE_URL + "/chat/completions"
        assert request.method == "POST"
        assert request.headers["authorization"] == f"Bearer {DUMMY_KEY}"
        requests.append(request)
        assert len(requests) <= len(responses), "Unexpected extra provider request or retry"
        return responses[len(requests) - 1]

    transport = httpx.MockTransport(respond)
    with httpx.Client(transport=transport) as sync_client:
        async with httpx.AsyncClient(transport=transport) as async_client:
            profile = Profile(
                name="mock-compatible",
                provider="openai",
                model="custom-coder",
                base_url=BASE_URL,
                api_key=DUMMY_KEY,
                model_retries=0,
                config_fields={
                    "http_client": sync_client,
                    "http_async_client": async_client,
                    "max_retries": 0,
                    **options,
                },
            )
            model = AgentRuntime._model_for(profile)
            assert isinstance(model, ChatOpenAI)
            yield model, requests


async def test_custom_base_url_plain_response_and_standard_usage():
    payload = completion({"role": "assistant", "content": "mock response"})
    async with mock_provider([httpx.Response(200, json=payload)], temperature=0.25) as (
        model,
        requests,
    ):
        response = await model.ainvoke("Inspect the workspace")
        assert isinstance(response, AIMessage)
        assert response.content == "mock response"
        assert response.usage_metadata["input_tokens"] == 12
        assert response.usage_metadata["output_tokens"] == 7
        assert response.usage_metadata["total_tokens"] == 19
        assert response.usage_metadata["input_token_details"]["cache_read"] == 3
        assert response.usage_metadata["output_token_details"]["reasoning"] == 2
        assert response.response_metadata["finish_reason"] == "stop"
        body = json.loads(requests[0].content)
        assert body["model"] == "custom-coder"
        assert body["temperature"] == 0.25
        assert body["messages"][-1]["content"] == "Inspect the workspace"


async def test_real_provider_tool_call_roundtrip_through_create_agent():
    invoked = []

    @tool
    def lookup_symbol(name: str) -> str:
        """Look up a symbol in the test's in-memory catalog."""
        invoked.append(name)
        return "src/main.py:7"

    first = completion(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-native-1",
                    "type": "function",
                    "function": {"name": "lookup_symbol", "arguments": '{"name":"main"}'},
                }
            ],
        },
        finish_reason="tool_calls",
    )
    second = completion({"role": "assistant", "content": "main is at src/main.py:7"})
    async with mock_provider(
        [httpx.Response(200, json=first), httpx.Response(200, json=second)]
    ) as (model, requests):
        graph = create_agent(model, tools=[lookup_symbol])
        result = await graph.ainvoke({"messages": [{"role": "user", "content": "Find main"}]})
        assert invoked == ["main"]
        assert result["messages"][-1].content == "main is at src/main.py:7"
        results = [message for message in result["messages"] if isinstance(message, ToolMessage)]
        assert len(results) == 1 and results[0].tool_call_id == "call-native-1"
        first_request = json.loads(requests[0].content)
        assert first_request["tools"][0]["function"]["name"] == "lookup_symbol"
        assert (
            first_request["tools"][0]["function"]["parameters"]["properties"]["name"]["type"]
            == "string"
        )
        second_request = json.loads(requests[1].content)
        assert any(
            message.get("role") == "tool" and message.get("tool_call_id") == "call-native-1"
            for message in second_request["messages"]
        )
        assert result["messages"][-1].usage_metadata["total_tokens"] == 19


@pytest.mark.parametrize(
    "status,error_class",
    [
        (401, openai.AuthenticationError),
        (429, openai.RateLimitError),
        (503, openai.InternalServerError),
    ],
)
async def test_http_errors_remain_typed_and_do_not_become_successful_messages(status, error_class):
    response = httpx.Response(
        status,
        json={
            "error": {
                "message": "mock provider failure",
                "type": "contract_error",
                "code": "mock_error",
            }
        },
    )
    async with mock_provider([response]) as (model, requests):
        with pytest.raises(error_class) as error:
            await model.ainvoke("test failure")
        assert error.value.status_code == status
        assert "mock provider failure" in str(error.value)
        assert len(requests) == 1


async def test_real_stream_parses_text_and_final_usage_from_mock_sse():
    def chunk(delta, *, finish_reason=None, usage=None):
        return {
            "id": "chatcmpl-stream-contract",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "custom-coder",
            "choices": []
            if usage
            else [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            **({"usage": usage} if usage else {}),
        }

    chunks = [
        chunk({"role": "assistant", "content": "hello "}),
        chunk({"content": "world"}),
        chunk({}, finish_reason="stop"),
        chunk({}, usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}),
    ]
    sse = "".join("data: " + json.dumps(item) + "\n\n" for item in chunks) + "data: [DONE]\n\n"
    response = httpx.Response(
        200, content=sse.encode(), headers={"content-type": "text/event-stream"}
    )
    async with mock_provider([response], stream_usage=True) as (model, requests):
        streamed = [part async for part in model.astream("stream the result")]
        assert "".join(part.content for part in streamed) == "hello world"
        usage = next(part.usage_metadata for part in streamed if part.usage_metadata)
        assert usage["total_tokens"] == 7
        body = json.loads(requests[0].content)
        assert body["stream"] is True
        assert body["stream_options"]["include_usage"] is True
