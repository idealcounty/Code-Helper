from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
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

    async def complete_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str | None = None,
        on_delta: Callable[[str], Awaitable[None] | None],
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

    async def complete_stream(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        reasoning_effort: str | None = None,
        on_delta: Callable[[str], Awaitable[None] | None],
    ) -> ModelResponse:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "tools": tools, "tool_choice": "auto", "stream": True}
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort
        if self.provider == "deepseek" and self.thinking_mode:
            body["thinking"] = {"type": self.thinking_mode}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        usage: dict[str, Any] = {}
        finish_reason: str | None = None
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                async with client.stream("POST", f"{self.base_url}/chat/completions", headers=headers, json=body) as response:
                    if response.is_error:
                        # Streaming responses are not buffered automatically. Read
                        # the error body before leaving the context so diagnostics
                        # remain available to _safe_error_detail.
                        await response.aread()
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            chunk = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        usage.update(chunk.get("usage") or {})
                        choice = (chunk.get("choices") or [{}])[0]
                        finish_reason = choice.get("finish_reason") or finish_reason
                        delta = choice.get("delta") or {}
                        piece = delta.get("content") or ""
                        if piece:
                            content.append(piece)
                            callback_result = on_delta(piece)
                            if hasattr(callback_result, "__await__"):
                                await callback_result
                        if delta.get("reasoning_content"):
                            reasoning.append(delta["reasoning_content"])
                        for position, raw_call in enumerate(delta.get("tool_calls") or []):
                            raw_index = raw_call.get("index", position)
                            try:
                                index = int(raw_index)
                            except (TypeError, ValueError):
                                index = position
                            item = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                            item["id"] = _merge_stream_piece(
                                item["id"], str(raw_call.get("id") or "")
                            )
                            function = raw_call.get("function") or {}
                            item["name"] = _merge_stream_piece(
                                item["name"], str(function.get("name") or "")
                            )
                            item["arguments"] = _merge_stream_piece(
                                item["arguments"], str(function.get("arguments") or "")
                            )
        except httpx.TimeoutException as exc:
            raise ModelError("Model request timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise ModelError(f"Model API returned HTTP {exc.response.status_code}: {_safe_error_detail(exc.response)}") from exc
        except httpx.HTTPError as exc:
            raise ModelError(f"Model request failed: {exc}") from exc
        try:
            parsed_calls = _parse_streamed_tool_calls(calls)
        except ModelProtocolError:
            # Providers document that models may occasionally emit invalid JSON.
            # Retry once without streaming: the complete response often contains
            # a coherent tool call and prevents a recoverable formatting glitch
            # from terminating the whole Agent turn.
            try:
                return await self.complete(
                    messages=messages,
                    tools=tools,
                    reasoning_effort=reasoning_effort,
                )
            except ModelError as fallback_error:
                raise ModelProtocolError(
                    "Model returned invalid tool call arguments after one retry"
                ) from fallback_error
        if not usage:
            usage = {}
        return ModelResponse(
            "".join(content),
            parsed_calls,
            usage,
            finish_reason=finish_reason,
            reasoning_content="".join(reasoning),
        )


def _merge_stream_piece(current: str, incoming: str) -> str:
    """Merge fragment-style and cumulative-style streaming fields."""
    if not incoming:
        return current
    if not current:
        return incoming
    if incoming == current or current.endswith(incoming):
        return current
    if incoming.startswith(current):
        return incoming
    return current + incoming


def _parse_streamed_tool_calls(
    calls: dict[int, dict[str, str]],
) -> list[ToolCall]:
    parsed: list[ToolCall] = []
    for index in sorted(calls):
        item = calls[index]
        if not item["id"] or not item["name"]:
            raise ModelProtocolError("Streamed tool call is missing id or name")
        arguments = _decode_tool_arguments(item["arguments"])
        parsed.append(ToolCall(item["id"], item["name"], arguments))
    return parsed


def _decode_tool_arguments(raw: str) -> dict[str, Any]:
    """Decode JSON with only conservative, non-semantic repairs."""
    candidate = (raw or "{}").strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
    candidates = [candidate]
    without_trailing_commas = re.sub(r",\s*([}\]])", r"\1", candidate)
    if without_trailing_commas != candidate:
        candidates.append(without_trailing_commas)
    for value in candidates:
        try:
            decoded = json.loads(value)
            if isinstance(decoded, str):
                decoded = json.loads(decoded)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(decoded, dict):
            return decoded
    raise ModelProtocolError("Invalid streamed tool call arguments")


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
