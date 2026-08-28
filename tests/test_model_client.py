from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from coding_agent.model import OpenAICompatibleModelClient


def test_deepseek_request_and_thinking_tool_state_round_trip() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "I will inspect the project.",
                            "reasoning_content": "internal protocol state",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "list_files",
                                        "arguments": '{"path":"."}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    client = OpenAICompatibleModelClient(
        api_key="test-key",
        base_url="https://api.deepseek.com/",
        model="deepseek-v4-flash",
        provider="deepseek",
        thinking_mode="enabled",
        transport=httpx.MockTransport(handler),
    )

    response = asyncio.run(
        client.complete(
            messages=[{"role": "user", "content": "Inspect this project"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "list_files",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            reasoning_effort="high",
        )
    )

    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["authorization"] == "Bearer test-key"
    assert captured["body"]["model"] == "deepseek-v4-flash"
    assert captured["body"]["thinking"] == {"type": "enabled"}
    assert captured["body"]["reasoning_effort"] == "high"
    assert captured["body"]["stream"] is False
    assert response.tool_calls[0].name == "list_files"
    assert response.to_assistant_message()["reasoning_content"] == (
        "internal protocol state"
    )


def test_thinking_switch_is_not_sent_to_other_compatible_providers() -> None:
    captured_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "done"}}
                ]
            },
        )

    client = OpenAICompatibleModelClient(
        api_key="test-key",
        base_url="https://compatible.example/v1",
        model="test-model",
        provider="openai-compatible",
        thinking_mode="enabled",
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(client.complete(messages=[], tools=[]))

    assert "thinking" not in captured_body


def test_streaming_client_emits_text_deltas() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        lines = "\n".join([
            'data: {"choices":[{"delta":{"content":"hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo"}}]}',
            "data: [DONE]",
            "",
        ])
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=lines.encode())

    client = OpenAICompatibleModelClient(
        api_key="test-key", base_url="https://api.example/v1", model="test",
        transport=httpx.MockTransport(handler),
    )
    deltas: list[str] = []
    response = asyncio.run(client.complete_stream(messages=[], tools=[], on_delta=deltas.append))
    assert captured["body"]["stream"] is True
    assert deltas == ["hel", "lo"]
    assert response.content == "hello"


def test_streaming_client_merges_interleaved_tool_calls_by_protocol_index() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        lines = "\n".join([
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_0","function":{"name":"read_file","arguments":"{\\"path\\":\\""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"call_1","function":{"name":"search_text","arguments":"{\\"query\\":\\""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"app.py\\"}"}},{"index":1,"function":{"arguments":"TODO\\"}"}}]},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
            "",
        ])
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=lines.encode(),
        )

    client = OpenAICompatibleModelClient(
        api_key="test-key",
        base_url="https://api.example/v1",
        model="test",
        transport=httpx.MockTransport(handler),
    )
    response = asyncio.run(
        client.complete_stream(messages=[], tools=[], on_delta=lambda _: None)
    )

    assert response.finish_reason == "tool_calls"
    assert [(call.id, call.name, call.arguments) for call in response.tool_calls] == [
        ("call_0", "read_file", {"path": "app.py"}),
        ("call_1", "search_text", {"query": "TODO"}),
    ]


def test_streaming_client_conservatively_repairs_trailing_comma() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        lines = "\n".join([
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_0","function":{"name":"list_files","arguments":"{\\"path\\":\\".\\",}"}}]},"finish_reason":"tool_calls"}]}',
            "data: [DONE]",
            "",
        ])
        return httpx.Response(200, content=lines.encode())

    client = OpenAICompatibleModelClient(
        api_key="test-key",
        base_url="https://api.example/v1",
        model="test",
        transport=httpx.MockTransport(handler),
    )
    response = asyncio.run(
        client.complete_stream(messages=[], tools=[], on_delta=lambda _: None)
    )

    assert response.tool_calls[0].arguments == {"path": "."}


def test_streaming_client_retries_invalid_arguments_without_streaming() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if body["stream"]:
            lines = "\n".join([
                'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_bad","function":{"name":"read_file","arguments":"{not-json"}}]},"finish_reason":"tool_calls"}]}',
                "data: [DONE]",
                "",
            ])
            return httpx.Response(200, content=lines.encode())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call_retry",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path":"app.py"}',
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
        )

    client = OpenAICompatibleModelClient(
        api_key="test-key",
        base_url="https://api.example/v1",
        model="test",
        transport=httpx.MockTransport(handler),
    )
    response = asyncio.run(
        client.complete_stream(messages=[], tools=[], on_delta=lambda _: None)
    )

    assert [request["stream"] for request in requests] == [True, False]
    assert response.tool_calls[0].id == "call_retry"
    assert response.tool_calls[0].arguments == {"path": "app.py"}
