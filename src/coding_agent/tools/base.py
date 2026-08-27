from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class ToolRisk(StrEnum):
    READ = "read"
    WRITE = "write"
    COMMAND = "command"
    DESTRUCTIVE = "destructive"


class ToolError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


@dataclass(slots=True)
class ToolResult:
    ok: bool
    code: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def success(
        cls,
        message: str,
        *,
        data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(True, "OK", message, data or {}, metadata or {})

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        *,
        data: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "ToolResult":
        return cls(False, code, message, data or {}, metadata or {})


ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    risk: ToolRisk
    handler: ToolHandler
    timeout: float = 60.0

    def as_model_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def validate(self, arguments: dict[str, Any]) -> None:
        if not isinstance(arguments, dict):
            raise ToolError("INVALID_ARGUMENTS", "Tool arguments must be an object")

        required = self.parameters.get("required", [])
        missing = [name for name in required if name not in arguments]
        if missing:
            raise ToolError(
                "INVALID_ARGUMENTS",
                f"Missing required arguments: {', '.join(missing)}",
            )

        properties = self.parameters.get("properties", {})
        extra = sorted(set(arguments) - set(properties))
        if extra and self.parameters.get("additionalProperties") is False:
            raise ToolError(
                "INVALID_ARGUMENTS",
                f"Unknown arguments: {', '.join(extra)}",
            )

        for name, value in arguments.items():
            schema = properties.get(name)
            if schema is None:
                continue
            expected = schema.get("type")
            if expected and not _matches_json_type(value, expected):
                raise ToolError(
                    "INVALID_ARGUMENTS",
                    f"Argument {name!r} must be {expected}",
                )
            if "enum" in schema and value not in schema["enum"]:
                allowed = ", ".join(map(str, schema["enum"]))
                raise ToolError(
                    "INVALID_ARGUMENTS",
                    f"Argument {name!r} must be one of: {allowed}",
                )


def _matches_json_type(value: Any, expected: str) -> bool:
    mapping: dict[str, type | tuple[type, ...]] = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    wanted = mapping.get(expected)
    if wanted is None:
        return True
    if expected in {"integer", "number"} and isinstance(value, bool):
        return False
    return isinstance(value, wanted)
