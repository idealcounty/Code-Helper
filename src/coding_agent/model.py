from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


class ModelError(RuntimeError):
    """Base error for model communication and protocol failures."""


class ModelProtocolError(ModelError):
    pass


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def to_message_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass(slots=True)
class ModelResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str | None = None
    reasoning_content: str = field(default="", repr=False)

    def to_assistant_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": self.content or None,
        }
        if self.tool_calls:
            message["tool_calls"] = [call.to_message_dict() for call in self.tool_calls]
        if self.reasoning_content:
            # DeepSeek thinking-mode tool calls must return this protocol state on
            # the next request. It stays internal and is never emitted to the UI.
            message["reasoning_content"] = self.reasoning_content
        return message


class ModelClient(Protocol):
    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str | None = None,
    ) -> ModelResponse: ...


class OpenAICompatibleModelClient:
    """Minimal native tool-calling client; it contains no agent orchestration."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout: float = 120.0,
        provider: str = "openai-compatible",
        thinking_mode: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("An API key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.provider = provider
        self.thinking_mode = thinking_mode
        self.transport = transport

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str | None = None,
    ) -> ModelResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "stream": False,
        }
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        if self.provider == "deepseek" and self.thinking_mode:
            body["thinking"] = {"type": self.thinking_mode}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self.transport
            ) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=body,
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ModelError("Model request timed out") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            detail = _safe_error_detail(exc.response)
            raise ModelError(f"Model API returned HTTP {status}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise ModelError(f"Model request failed: {exc}") from exc

        return _parse_chat_completion(response.json())


def _parse_chat_completion(payload: dict[str, Any]) -> ModelResponse:
    try:
        choice = payload["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelProtocolError("Model response does not contain a message") from exc

    calls: list[ToolCall] = []
    for raw_call in message.get("tool_calls") or []:
        try:
            function = raw_call["function"]
            arguments = json.loads(function.get("arguments") or "{}")
            if not isinstance(arguments, dict):
                raise TypeError("tool arguments must be a JSON object")
            calls.append(
                ToolCall(
                    id=str(raw_call["id"]),
                    name=str(function["name"]),
                    arguments=arguments,
                )
            )
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ModelProtocolError(f"Invalid tool call: {raw_call!r}") from exc

    return ModelResponse(
        content=message.get("content") or "",
        tool_calls=calls,
        usage=payload.get("usage") or {},
        finish_reason=choice.get("finish_reason"),
        reasoning_content=message.get("reasoning_content") or "",
    )


def _safe_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error", payload)
        if isinstance(error, dict):
            return str(error.get("message", error))[:500]
        return str(error)[:500]
    except (ValueError, TypeError):
        return response.text[:500]
