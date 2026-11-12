from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from copy import deepcopy
from collections.abc import Sequence
from typing import Any

from openai import APIConnectionError, APIError, APIResponseValidationError, APIStatusError, APITimeoutError, AsyncOpenAI
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCallUnion,
)
from openai.types.chat import ChatCompletion

from app.agent.step_models import (
    AssistantMessage, ModelMessage, ModelTurn, SystemMessage,
    ToolCall, ToolDefinition, ToolMessage, ToolsShownOnly, UserMessage,
)
from app.agent.tool_calling_model import ModelRequestError, ModelResponseError, ModelTimeoutError
from app.agent.tool_calling_model import ModelContextOverflowError, ModelOutputLimitError
from app.agent.tool_calling_model import ModelToolArgumentsError
from app.agent.run_meter import current_meter, record_model_call

logger = logging.getLogger(__name__)

# Statuses worth asking again: request timeout, conflict, rate limit, server errors (D-039).
_RETRYABLE_STATUS = frozenset({408, 409, 429})


def _retryable(error: Exception, value: bool = True) -> Exception:
    """Mark a wrapped provider error as safe to request again (nothing from it was executed)."""
    error.retryable = value
    return error


def _final(message: str) -> ModelResponseError:
    """A response that would come back the same on a retry: filtered, refused or unsupported."""
    return _retryable(ModelResponseError(message), False)


class OpenAICompatibleToolCallingModel:
    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        *,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
        json_mode: bool = False,
        enable_thinking: bool | None = None,
        max_retries: int = 2,
        retry_delay_seconds: float = 0.5,
        prompt_cache_key: str | None = None,
    ) -> None:
        create = getattr(getattr(getattr(client, "chat", None), "completions", None), "create", None)
        if not callable(create):
            raise TypeError("client must provide chat.completions.create()")
        self._require_text(model, "model")
        if type(json_mode) is not bool:
            raise TypeError("json_mode must be a bool")
        if enable_thinking is not None and type(enable_thinking) is not bool:
            raise TypeError("enable_thinking must be a bool or None")
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
        if prompt_cache_key is not None and (
                not isinstance(prompt_cache_key, str) or not prompt_cache_key.strip() or len(prompt_cache_key) > 64):
            raise ValueError("prompt_cache_key must be a nonempty string of at most 64 characters or None")
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        if (isinstance(retry_delay_seconds, bool) or not isinstance(retry_delay_seconds, (int, float))
                or not math.isfinite(retry_delay_seconds) or retry_delay_seconds < 0):
            raise ValueError("retry_delay_seconds must be a finite non-negative number")

        self._client = client
        self._model = model.strip()
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._timeout_seconds = timeout_seconds
        self._json_mode = json_mode
        self._enable_thinking = enable_thinking
        self._max_retries = max_retries
        self._retry_delay_seconds = float(retry_delay_seconds)
        # Requests with the same key are routed together by OpenAI-style providers, which
        # raises prompt-cache hits (D-057); None sends nothing.
        self._prompt_cache_key = prompt_cache_key
        # The last request sent, serialized, to measure how much of the next one starts the
        # same way (a provider prompt cache can only reuse a shared start; D-056).
        self._previous_request: str | None = None

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
            if isinstance(tools, ToolsShownOnly):
                request_kwargs["tool_choice"] = "none"     # shown for the cache, not callable (D-057)
        if self._json_mode:
            request_kwargs["response_format"] = {"type": "json_object"}
        extra_body = {}
        if self._enable_thinking is not None:
            extra_body["enable_thinking"] = self._enable_thinking
        if self._prompt_cache_key is not None:
            extra_body["prompt_cache_key"] = self._prompt_cache_key
        if extra_body:
            request_kwargs["extra_body"] = extra_body

        if self._temperature is not None:
            request_kwargs["temperature"] = self._temperature

        if self._max_output_tokens is not None:
            request_kwargs["max_tokens"] = self._max_output_tokens
        if self._timeout_seconds is not None:
            request_kwargs["timeout"] = self._timeout_seconds

        attempt = 0
        while True:
            try:
                shared = self._shared_start(request_kwargs)
                return await self._request_once(request_kwargs, shared)
            except (ModelRequestError, ModelResponseError) as exc:
                delay = self._retry_delay(exc, attempt)
                if delay is None:
                    raise
                attempt += 1
                # Type name only, never provider text (D-039).
                logger.warning('Model call failed (%s); retry %d/%d in %.1fs',
                               type(exc).__name__, attempt, self._max_retries, delay)
                await asyncio.sleep(delay)

    def _retry_delay(self, error: Exception, attempt: int) -> float | None:
        """Seconds to wait before asking again, or None to give up (D-039).

        Only errors marked retryable qualify: nothing from such a response was executed,
        so asking again repeats no work. The run's token and time budget still apply.
        """
        if attempt >= self._max_retries or getattr(error, 'retryable', False) is not True:
            return None
        delay = self._retry_delay_seconds * (2 ** attempt)
        meter = current_meter()
        if meter is not None:
            if meter.budget_problem() is not None:
                return None
            remaining = meter.remaining_seconds()
            if remaining is not None and remaining <= delay:
                return None
        return delay

    def _shared_start(self, request_kwargs: dict[str, Any]) -> float | None:
        """Share of the previous request (tools, then messages) that this one starts with
        unchanged, or None for the first request; only the ratio is kept (D-056). A request
        that only appends to the previous one gives about 1, whatever it appends."""
        current = json.dumps([request_kwargs.get("tools"), request_kwargs["messages"]],
                             ensure_ascii=False, default=str)
        previous, self._previous_request = self._previous_request, current
        if not previous:
            return None
        # The serialized previous request ends with its closing brackets, which an appended
        # request replaces; they do not count as a change.
        kept = len(os.path.commonprefix([previous, current]))
        return min(1.0, kept / max(1, len(previous.rstrip(']'))))

    async def _request_once(self, request_kwargs: dict[str, Any], shared: float | None = None) -> ModelTurn:
        try:
            try:
                raw_response = await self._client.chat.completions.create(
                    **request_kwargs
                )
            except BaseException:
                # the request was sent; its usage is unknown (D-034)
                record_model_call(None, shared)
                raise
            record_model_call(getattr(raw_response, 'usage', None), shared, getattr(raw_response, 'model', None))
        except APIResponseValidationError as exc:
            raise _retryable(ModelResponseError("provider returned an invalid response")) from exc
        except APITimeoutError as exc:
            raise _retryable(ModelTimeoutError("model request timed out")) from exc
        except APIConnectionError as exc:
            raise _retryable(ModelRequestError("model request failed")) from exc
        except APIError as exc:
            body = getattr(exc, "body", None)
            detail = body.get("error", body) if isinstance(body, dict) else {}
            code = detail.get("code") if isinstance(detail, dict) else None
            code = code or getattr(exc, "code", None)
            if getattr(exc, "status_code", None) in (400, 413) and code in {
                "context_length_exceeded", "context_window_exceeded", "prompt_too_long",
            }:
                raise ModelContextOverflowError("provider context length exceeded") from exc
            status = getattr(exc, "status_code", None) if isinstance(exc, APIStatusError) else None
            transient = isinstance(status, int) and (status in _RETRYABLE_STATUS or status >= 500)
            raise _retryable(ModelRequestError("model request failed"), transient) from exc

        try:
            return self._parse_response(raw_response)
        except ModelResponseError as exc:
            # A malformed or empty reply is usually a one-off; truncation, filtering, refusals
            # and undecodable tool arguments are not retried here.
            if type(exc) is ModelResponseError and not hasattr(exc, 'retryable'):
                exc.retryable = True
            raise

    @classmethod
    def _build_message_payload(
        cls, messages: Sequence[ModelMessage]
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
                cls._require_text(message.call_id, "tool call_id")
            else:
                cls._require_text(message.content, "message content")

            item: dict[str, Any] = {"role": message.role, "content": message.content}
            if isinstance(message, AssistantMessage) and message.tool_calls:
                calls = []
                for call in message.tool_calls:
                    if not isinstance(call, ToolCall):
                        raise TypeError("assistant tool_calls must contain ToolCall objects")
                    cls._require_text(call.call_id, "call_id")
                    cls._require_text(call.tool_name, "tool_name")
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

    @classmethod
    def _build_tool_payload(
        cls, tools: tuple[ToolDefinition, ...]
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
            cls._require_text(tool.name, f"tools[{index}].name")
            cls._require_text(tool.description, f"tools[{index}].description")
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

    def _parse_arguments(self, raw_arguments: str, tool_name: str | None = None) -> dict[str, Any]:
        if not isinstance(raw_arguments, str):
            raise ModelToolArgumentsError('not_string', tool_name=tool_name)

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
            raise ModelToolArgumentsError('invalid_json', tool_name=tool_name,
                                          argument_chars=len(raw_arguments)) from e

        if not isinstance(arguments, dict):
            raise ModelToolArgumentsError('not_object', tool_name=tool_name,
                                          argument_chars=len(raw_arguments))

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

            arguments = self._parse_arguments(raw_arguments, tool_name)
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
            usage = getattr(response, 'usage', None)
            raise ModelOutputLimitError(prompt_tokens=getattr(usage, 'prompt_tokens', None),
                completion_tokens=getattr(usage, 'completion_tokens', None),
                output_token_limit=self._max_output_tokens)
        if finish_reason == "content_filter":
            raise _final("model response was filtered (finish_reason=content_filter)")
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
                raise _final("model refused the request")
        if getattr(message, "function_call", None) is not None:
            raise _final("legacy function_call is not supported; use tool_calls")

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


def profile_model(client, profile, prompt_cache_key: str | None = None) -> OpenAICompatibleToolCallingModel:
    """The model a profile describes, on an already open client (stage 4d-1). The cache key is
    sent only when the profile supports it (D-057)."""
    return OpenAICompatibleToolCallingModel(
        client, profile.model, temperature=profile.temperature,
        json_mode=profile.json_mode,
        enable_thinking=profile.enable_thinking,
        max_output_tokens=profile.max_output_tokens,
        prompt_cache_key=prompt_cache_key if profile.prompt_cache_key else None,
    )
