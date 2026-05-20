from __future__ import annotations

import json
import re
from typing import Any

from .review_plan import DEFAULT_RISK, RISK_LEVELS, normalize_risk


RISK_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["risk"],
    "properties": {
        "risk": {
            "type": "string",
            "enum": list(RISK_LEVELS),
        },
    },
}


def risk_schema_json() -> str:
    return json.dumps(RISK_SCHEMA, separators=(",", ":"))


def build_risk_prompt(description: str) -> str:
    return """Classify the pull request review risk using only the PR description below.

Return exactly one JSON object matching the provided schema. The risk value must be one of low, medium, or high.

Use low for routine, narrow, low-impact changes.
Use medium for normal product or implementation changes.
Use high for changes involving security, authentication, authorization, data loss, migrations, concurrency, payments, broad behavior changes, or other unusually risky rollout concerns.

PR description:
{description}
""".format(description=description or "")


def extract_risk(text: str) -> str:
    parsed = _parse_json(text)
    if parsed is not None:
        return _extract_risk_from_value(parsed)
    stripped = (text or "").strip()
    direct = normalize_risk(stripped)
    if direct != DEFAULT_RISK or stripped.lower() == DEFAULT_RISK:
        return direct
    for candidate in _fenced_json_candidates(stripped):
        risk = _extract_risk_from_value(_parse_json(candidate))
        if risk in RISK_LEVELS:
            return risk
    for candidate in _json_object_candidates(stripped):
        risk = _extract_risk_from_value(_parse_json(candidate))
        if risk in RISK_LEVELS:
            return risk
    return DEFAULT_RISK


def _extract_risk_from_value(value: Any) -> str:
    if isinstance(value, dict):
        if "risk" in value:
            return normalize_risk(value.get("risk"))
        if "result" in value:
            result = value["result"]
            if isinstance(result, str):
                return extract_risk(result)
            return _extract_risk_from_value(result)
    if isinstance(value, str):
        return normalize_risk(value)
    return DEFAULT_RISK


def _parse_json(text: str):
    try:
        return json.loads((text or "").strip())
    except (TypeError, json.JSONDecodeError):
        return None


def _fenced_json_candidates(text: str) -> list:
    candidates = []
    for match in re.finditer(r"```(?:json)?[ \t\r\n]*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL):
        candidate = match.group(1).strip()
        if candidate:
            candidates.append(candidate)
    return candidates


def _json_object_candidates(text: str) -> list:
    candidates = []
    start = text.find("{")
    while start >= 0:
        candidate = _json_object_at(text, start)
        if candidate is not None:
            candidates.append(candidate)
            start = text.find("{", start + len(candidate))
        else:
            start = text.find("{", start + 1)
    return candidates


def _json_object_at(text: str, start: int):
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : index + 1]
                if _parse_json(candidate) is not None:
                    return candidate
                return None
    return None
