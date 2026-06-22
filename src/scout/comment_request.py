from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


class CommentRequestValidationError(ValueError):
    pass


@dataclass(frozen=True)
class CommentRequestClassification:
    review_requested: bool
    reason: str


COMMENT_REQUEST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["review_requested", "reason"],
    "properties": {
        "review_requested": {"type": "boolean"},
        "reason": {"type": "string"},
    },
}

SCOUT_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_@.-])@scout(?![A-Za-z0-9_-])", re.IGNORECASE)


def has_scout_mention(text: str) -> bool:
    return SCOUT_MENTION_RE.search(text or "") is not None


def comment_request_schema_json() -> str:
    return json.dumps(COMMENT_REQUEST_SCHEMA, separators=(",", ":"))


def build_comment_request_prompt(comment: str) -> str:
    return """Classify whether the Bitbucket pull request comment below requests Scout to review the pull request.

Return exactly one JSON object matching the provided schema.

Set review_requested to true only when the comment asks Scout to review, scan, inspect, audit, check, analyze, or otherwise evaluate the pull request, diff, changes, or code.
Set review_requested to false for status checks, thanks, discussion about prior findings, ordinary mentions, or commands that do not ask Scout to perform a review.
Use reason to concisely explain the decision.

Comment:
{comment}
""".format(comment=comment or "")


def extract_comment_request(text: str) -> CommentRequestClassification:
    return _extract_comment_request_from_value(_parse_json_strict(text))


def _extract_comment_request_from_value(value: Any) -> CommentRequestClassification:
    if isinstance(value, dict) and "result" in value and not {"review_requested", "reason"}.issubset(value):
        result = value["result"]
        if isinstance(result, str):
            return extract_comment_request(result)
        return _extract_comment_request_from_value(result)
    if not isinstance(value, dict):
        raise CommentRequestValidationError("comment request output must be a JSON object")
    allowed = {"review_requested", "reason"}
    missing = sorted(allowed - set(value))
    if missing:
        raise CommentRequestValidationError("comment request output missing required keys: {}".format(", ".join(missing)))
    extra = sorted(set(value) - allowed)
    if extra:
        raise CommentRequestValidationError("comment request output contains unsupported keys: {}".format(", ".join(extra)))
    review_requested = value["review_requested"]
    reason = value["reason"]
    if not isinstance(review_requested, bool):
        raise CommentRequestValidationError("comment request output review_requested must be boolean")
    if not isinstance(reason, str) or not reason:
        raise CommentRequestValidationError("comment request output reason must be a non-empty string")
    return CommentRequestClassification(review_requested=review_requested, reason=reason)


def _parse_json_strict(text: str) -> Any:
    try:
        return json.loads((text or "").strip())
    except json.JSONDecodeError as exc:
        raise CommentRequestValidationError("comment request output is not valid JSON: {}".format(exc)) from exc
