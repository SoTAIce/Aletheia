"""Strict JSON decoding and a bounded, tool-free format repair instruction."""

import json


class JsonFormatError(ValueError):
    """Text is not a JSON document; domain validation remains separate."""


def load_json(text: str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f"non-finite JSON constant: {value}")

    try:
        result = json.loads(text, object_pairs_hook=unique, parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise JsonFormatError(f"Model response must be valid JSON: {exc}") from exc
    # Reject exponent overflow too; duplicate/non-finite values are ambiguous,
    # not syntax repairs. Never silently choose one status over another.
    json.dumps(result, allow_nan=False)
    return result


def unwrap_json_fence(text: str) -> str:
    lines = text.strip().splitlines()
    if len(lines) >= 3 and lines[0].lower() in ("```json", "```") and lines[-1] == "```":
        return "\n".join(lines[1:-1])
    return text


def repair_instruction(contract: str, error: str) -> str:
    return (
        "Repair only the serialization of your preceding response. Tools are disabled. "
        "Do not execute or request any tool, redo work, add evidence, or change the plan. "
        "Reuse all existing observations and preserve the response's meaning, including "
        "any failure or blocker. Do not convert failed/incomplete work into success. "
        "If the preceding text lacks a status, assess it against the existing evidence; "
        "never assume success merely because a format repair was requested. "
        f"Return JSON only, without Markdown or explanations. Contract: {contract}. "
        f"Serialization error: {error}"
    )
