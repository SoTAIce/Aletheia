from __future__ import annotations

import json
import math
from copy import deepcopy
from collections.abc import Sequence
from typing import Any

from openai import APIError, APIResponseValidationError, AsyncOpenAI
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCallUnion,
)
from openai.types.chat import ChatCompletion

from app.agent.step_models import (
    AssistantMessage, ModelMessage, ModelTurn, SystemMessage,
    ToolCall, ToolDefinition, ToolMessage, UserMessage,
)
from app.agent.tool_calling_model import ModelRequestError, ModelResponseError


class OpenAICompatibleToolCallingModel:
    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        *,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        create = getattr(getattr(getattr(client, "chat", None), "completions", None), "create", None)
        if not callable(create):
            raise TypeError("client must provide chat.completions.create()")
        self._require_text(model, "model")
        for name, value in (("temperature", temperature), ("timeout_seconds", timeout_seconds)):
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise TypeError(f"{name} must be a number or None")
                if not math.isfinite(value):
                    raise ValueError(f"{name} must be finite")
        if temperature is not None and not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if max_output_tokens is not None:
            if type(max_output_tokens) is not int:
                raise TypeError("max_output_tokens must be an integer or None")
            if max_output_tokens <= 0:
                raise ValueError("max_output_tokens must be greater than zero")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

        self._client = client
        self._model = model.strip()
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds

    @staticmethod
    def _require_text(value: object, field: str) -> None:
        if not isinstance(value, str):
            raise TypeError(f"{field} must be a string")
        if not value.strip():
            raise ValueError(f"{field} must not be empty")

    async def generate(
        self,
        messages: Sequence[ModelMessage],
        tools: tuple[ToolDefinition, ...],
    ) -> ModelTurn:
        provider_messages = self._build_message_payload(messages)
        provider_tools = self._build_tool_payload(tools)
        request_kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": provider_messages,
        }
        if provider_tools:
            request_kwargs["tools"] = provider_tools

        if self._temperature is not None:
            request_kwargs["temperature"] = self._temperature

        if self._max_output_tokens is not None:
            request_kwargs["max_tokens"] = self._max_output_tokens
        if self._timeout_seconds is not None:
            request_kwargs["timeout"] = self._timeout_seconds

        try:
            raw_response = await self._client.chat.completions.create(
                **request_kwargs
            )
        except APIResponseValidationError as exc:
            raise ModelResponseError("provider returned an invalid response") from exc
        except APIError as exc:
            raise ModelRequestError("model request failed") from exc

        return self._parse_response(raw_response)

    def _build_message_payload(
        self, messages: Sequence[ModelMessage]
    ) -> list[dict[str, Any]]:
        if isinstance(messages, (str, bytes)) or not isinstance(messages, Sequence):
            raise TypeError("messages must be a sequence of ModelMessage objects")
        if not messages:
            raise ValueError("messages must not be empty")
        payload: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, (SystemMessage, UserMessage, AssistantMessage, ToolMessage)):
                raise TypeError("messages contains an unsupported message type")
            if isinstance(message, AssistantMessage):
                if message.content is not None and not isinstance(message.content, str):
                    raise TypeError("assistant content must be a string or None")
                if not isinstance(message.tool_calls, tuple):
                    raise TypeError("assistant tool_calls must be a tuple")
                if not message.tool_calls and not (message.content and message.content.strip()):
                    raise ValueError("assistant message must contain text or tool calls")
            elif isinstance(message, ToolMessage):
                if not isinstance(message.content, str):
                    raise TypeError("tool content must be a string")
                self._require_text(message.call_id, "tool call_id")
            else:
                self._require_text(message.content, "message content")

            item: dict[str, Any] = {"role": message.role, "content": message.content}
            if isinstance(message, AssistantMessage) and message.tool_calls:
                calls = []
                for call in message.tool_calls:
                    if not isinstance(call, ToolCall):
                        raise TypeError("assistant tool_calls must contain ToolCall objects")
                    self._require_text(call.call_id, "call_id")
                    self._require_text(call.tool_name, "tool_name")
                    if not isinstance(call.arguments, dict) or not all(isinstance(k, str) for k in call.arguments):
                        raise TypeError("tool arguments must be a dict with string keys")
                    try:
                        arguments = json.dumps(call.arguments, ensure_ascii=False, allow_nan=False)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("tool arguments must be JSON serializable") from exc
                    calls.append({
                        "id": call.call_id,
                        "type": "function",
                        "function": {"name": call.tool_name, "arguments": arguments},
                    })
                item["tool_calls"] = calls
            if isinstance(message, ToolMessage):
                item["tool_call_id"] = message.call_id
            payload.append(item)
        return payload

    def _build_tool_payload(
        self, tools: tuple[ToolDefinition, ...]
    ) -> list[dict[str, Any]]:
        """Convert tool definitions without modifying their names or schemas.

        Check basic schema shape and JSON serialization, not full JSON Schema
        semantics. An empty tuple is valid and produces an empty payload.
        """
        if not isinstance(tools, tuple):
            raise TypeError("tools must be a tuple of ToolDefinition objects")

        payload: list[dict[str, Any]] = []
        names: set[str] = set()
        for index, tool in enumerate(tools):
            if not isinstance(tool, ToolDefinition):
                raise TypeError(f"tools[{index}] must be a ToolDefinition")
            self._require_text(tool.name, f"tools[{index}].name")
            self._require_text(tool.description, f"tools[{index}].description")
            if tool.name != tool.name.strip():
                raise ValueError("tool name must not contain surrounding whitespace")
            if tool.name in names:
                raise ValueError(f"duplicate tool name: {tool.name}")

            schema = tool.input_schema
            if not isinstance(schema, dict):
                raise TypeError(f"tools[{index}].input_schema must be a dict")
            if schema.get("type") != "object":
                raise ValueError(f"tools[{index}].input_schema must have type 'object'")
            try:
                json.dumps(schema, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"tools[{index}].input_schema must be JSON serializable") from exc

            payload.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description.strip(),
                    "parameters": deepcopy(schema),
                },
            })
            names.add(tool.name)
        return payload

    def _parse_arguments(self, raw_arguments: str) -> dict[str, Any]:
        if not isinstance(raw_arguments, str):
            raise ModelResponseError(
                "tool call arguments must be a JSON string"
            )

        def reject_constant(value: str) -> None:
            raise ValueError(f"non-finite JSON constant: {value}")

        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON key: {key}")
                result[key] = value
            return result

        try:
            arguments = json.loads(
                raw_arguments, parse_constant=reject_constant, object_pairs_hook=unique_object
            )
            # Also reject exponent overflow such as 1e999, including nested values.
            json.dumps(arguments, allow_nan=False)
        except (ValueError, RecursionError) as e:
            raise ModelResponseError(
                "tool call arguments are not valid JSON"
            ) from e

        if not isinstance(arguments, dict):
            raise ModelResponseError(
                "tool call arguments must decode to a JSON object"
            )

        return arguments

    def _parse_tool_calls(
        self,
        raw_tool_calls: Sequence[ChatCompletionMessageToolCallUnion] | None,
    ) -> tuple[ToolCall, ...]:
        """Normalize SDK function calls, preserving provider IDs and order."""
        if raw_tool_calls is None:
            return ()
        if isinstance(raw_tool_calls, (str, bytes)) or not isinstance(raw_tool_calls, Sequence):
            raise ModelResponseError("tool_calls must be a sequence or None")

        calls: list[ToolCall] = []
        seen_ids: set[str] = set()
        for index, raw_call in enumerate(raw_tool_calls):
            if getattr(raw_call, "type", None) != "function":
                raise ModelResponseError(f"tool_calls[{index}] must be a function call")
            call_id = getattr(raw_call, "id", None)
            function = getattr(raw_call, "function", None)
            tool_name = getattr(function, "name", None)
            raw_arguments = getattr(function, "arguments", None)

            if not isinstance(call_id, str) or not call_id.strip():
                raise ModelResponseError(f"tool_calls[{index}].id must be a non-empty string")
            if not isinstance(tool_name, str) or not tool_name.strip():
                raise ModelResponseError(f"tool_calls[{index}].function.name must be a non-empty string")
            if call_id in seen_ids:
                raise ModelResponseError(f"duplicate tool call ID: {call_id}")

            arguments = self._parse_arguments(raw_arguments)
            calls.append(ToolCall(
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
            ))
            seen_ids.add(call_id)
        return tuple(calls)

    def _parse_response(self, response: ChatCompletion) -> ModelTurn:
        """Parse a single completed choice without accepting partial output.

        Some compatible providers use 'stop' even when tool calls are present;
        valid calls remain authoritative. 'tool_calls' requires actual calls.
        """
        choices = getattr(response, "choices", None)
        if not isinstance(choices, list) or len(choices) != 1:
            raise ModelResponseError("response.choices must contain exactly one choice")

        choice = choices[0]
        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason == "length":
            raise ModelResponseError("model response was truncated (finish_reason=length)")
        if finish_reason == "content_filter":
            raise ModelResponseError("model response was filtered (finish_reason=content_filter)")
        if finish_reason not in ("stop", "tool_calls"):
            raise ModelResponseError(f"unsupported finish_reason: {finish_reason!r}")

        message = getattr(choice, "message", None)
        if message is None or getattr(message, "role", None) != "assistant":
            raise ModelResponseError("response must contain an assistant message")
        refusal = getattr(message, "refusal", None)
        if refusal is not None:
            if not isinstance(refusal, str):
                raise ModelResponseError("message.refusal must be a string or None")
            if refusal.strip():
                raise ModelResponseError("model refused the request")
        if getattr(message, "function_call", None) is not None:
            raise ModelResponseError("legacy function_call is not supported; use tool_calls")

        content = getattr(message, "content", None)
        if content is not None and not isinstance(content, str):
            raise ModelResponseError("message.content must be a string or None")
        # Preserve meaningful text verbatim; whitespace-only text is absent.
        if content is not None and not content.strip():
            content = None
        tool_calls = self._parse_tool_calls(getattr(message, "tool_calls", None))
        if finish_reason == "tool_calls" and not tool_calls:
            raise ModelResponseError("finish_reason=tool_calls requires at least one tool call")
        if content is None and not tool_calls:
            raise ModelResponseError("model response contains neither text nor tool calls")
        return ModelTurn(content=content, tool_calls=tool_calls)
