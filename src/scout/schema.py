from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional


class ReviewValidationError(ValueError):
    pass


RECOMMENDATIONS = {"approve", "request_changes"}
REPORT_TYPES = {"BUG", "SECURITY", "TEST", "COVERAGE"}
DATA_TYPES = {"BOOLEAN", "DATE", "DURATION", "LINK", "NUMBER", "PERCENTAGE", "TEXT"}
ANNOTATION_TYPES = {"BUG", "VULNERABILITY", "CODE_SMELL"}
SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
SEVERITIES = set(SEVERITY_ORDER)
REVIEWER_ORDER = ["correctness", "security", "tests", "performance", "best-practices"]
REVIEWERS = set(REVIEWER_ORDER)
CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}
BITBUCKET_REPORT_DETAILS_MAX_LENGTH = 2000
BITBUCKET_COMMENT_MAX_LENGTH = 8000


@dataclass(frozen=True)
class ValidatedReview:
    recommendation: str
    report: Dict[str, Any]
    annotations: List[Dict[str, Any]]


def parse_review_json(text: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReviewValidationError("review output is not valid JSON: {}".format(exc)) from exc
    if not isinstance(parsed, dict):
        raise ReviewValidationError("review output must be a JSON object")
    return parsed


def validate_review_output(obj: Dict[str, Any], max_findings: int = 100) -> ValidatedReview:
    _require_keys(obj, {"recommendation", "report", "annotations"}, "root")
    _reject_extra_keys(obj, {"recommendation", "report", "annotations"}, "root")

    recommendation = _enum(obj["recommendation"], RECOMMENDATIONS, "recommendation")
    report = _validate_report(obj["report"])
    annotations = _validate_annotations(obj["annotations"], max_findings)

    if recommendation == "approve" and annotations:
        raise ReviewValidationError("approve recommendation must not include failed annotations")
    if recommendation == "request_changes" and not annotations:
        raise ReviewValidationError("request_changes recommendation must include at least one annotation")

    return ValidatedReview(recommendation=recommendation, report=report, annotations=annotations)


def report_result_for_recommendation(recommendation: str) -> str:
    if recommendation == "approve":
        return "PASSED"
    if recommendation == "request_changes":
        return "FAILED"
    raise ReviewValidationError("unknown recommendation: {}".format(recommendation))


def to_bitbucket_report(
    review: ValidatedReview,
    title: str,
    provider: str = "codex",
    model_metadata: Optional[str] = None,
) -> Dict[str, Any]:
    provider_label = _provider_label(provider)
    return {
        "title": _format_report_title(title, provider_label),
        "details": _format_report_details(review, provider_label),
        "report_type": _report_type(review),
        "reporter": "scout",
        "result": report_result_for_recommendation(review.recommendation),
        "data": _report_data(review, provider_label, model_metadata),
    }


def to_bitbucket_annotations(review: ValidatedReview, provider: str = "codex") -> List[Dict[str, Any]]:
    provider_label = _provider_label(provider)
    converted = []
    for annotation in review.annotations:
        item = {
            "external_id": annotation["external_id"],
            "annotation_type": annotation["annotation_type"],
            "path": annotation["path"],
            "line": annotation["line"],
            "summary": annotation["summary"],
            "details": _format_details(annotation, provider_label),
            "severity": annotation["severity"],
            "result": annotation["result"],
        }
        converted.append(item)
    return converted


def to_pr_comment(
    review: ValidatedReview,
    provider: str = "codex",
    source_commit: str = "",
    severities: Iterable[str] = ("CRITICAL",),
) -> str:
    allowed_severities = set(severities)
    selected_severities = [severity for severity in SEVERITY_ORDER if severity in allowed_severities]
    if not selected_severities:
        return ""
    selected = sorted(
        (
            annotation for annotation in review.annotations
            if annotation["severity"] in selected_severities
        ),
        key=lambda annotation: (
            SEVERITY_ORDER.index(annotation["severity"]),
            annotation["path"],
            annotation["line"],
            annotation["external_id"],
        ),
    )
    if not selected:
        return ""
    provider_label = _provider_label(provider)
    if len(selected_severities) == 1:
        heading = "Scout: {} issue found by {}:".format(
            _sentence_case(selected_severities[0]),
            provider_label,
        )
    else:
        heading = "Scout: Issues found by {}:".format(provider_label)
    lines = [
        "**{}**".format(heading),
        "",
    ]
    if source_commit:
        lines.extend(["Commit: `{}`".format(source_commit[:12]), ""])
    for index, annotation in enumerate(selected, start=1):
        lines.extend(
            [
                "{}. **{}**".format(index, annotation["summary"]),
                "   Severity: {}".format(_sentence_case(annotation["severity"])),
                "   Location: `{}:{}`".format(annotation["path"], annotation["line"]),
                "   Reviewer: {} / {} confidence".format(
                    _reviewer_label(annotation["reviewer"]),
                    annotation["confidence"],
                ),
                "   Why it matters: {}".format(annotation["details"]),
                "   Smallest fix: {}".format(annotation["smallest_fix"]),
                "",
            ]
        )
    return _truncate("\n".join(lines).rstrip(), BITBUCKET_COMMENT_MAX_LENGTH)


def to_critical_pr_comment(
    review: ValidatedReview,
    provider: str = "codex",
    source_commit: str = "",
) -> str:
    return to_pr_comment(review, provider=provider, source_commit=source_commit, severities=("CRITICAL",))


def summarize_findings(review: ValidatedReview) -> Dict[str, Any]:
    by_reviewer = {reviewer: 0 for reviewer in REVIEWER_ORDER}
    by_severity = {severity: 0 for severity in SEVERITY_ORDER}
    by_reviewer_and_severity = {
        reviewer: {severity: 0 for severity in SEVERITY_ORDER}
        for reviewer in REVIEWER_ORDER
    }
    for annotation in review.annotations:
        reviewer = annotation["reviewer"]
        severity = annotation["severity"]
        by_reviewer[reviewer] += 1
        by_severity[severity] += 1
        by_reviewer_and_severity[reviewer][severity] += 1

    return {
        "total": len(review.annotations),
        "by_reviewer": _nonzero_ordered_counts(by_reviewer, REVIEWER_ORDER),
        "by_severity": _nonzero_ordered_counts(by_severity, SEVERITY_ORDER),
        "by_reviewer_and_severity": {
            reviewer: _nonzero_ordered_counts(by_reviewer_and_severity[reviewer], SEVERITY_ORDER)
            for reviewer in REVIEWER_ORDER
            if by_reviewer[reviewer]
        },
    }


def _validate_report(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ReviewValidationError("report must be an object")
    allowed = {"title", "details", "report_type", "reporter", "data"}
    _require_keys(value, allowed, "report")
    _reject_extra_keys(value, allowed, "report")
    _nonempty_string(value["title"], "report.title")
    _nonempty_string(value["details"], "report.details")
    _enum(value["report_type"], REPORT_TYPES, "report.report_type")
    if value["reporter"] != "scout":
        raise ReviewValidationError("report.reporter must be scout")
    if not isinstance(value["data"], list):
        raise ReviewValidationError("report.data must be an array")
    for idx, item in enumerate(value["data"]):
        _validate_data_item(item, "report.data[{}]".format(idx))
    return deepcopy(value)


def _validate_data_item(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise ReviewValidationError("{} must be an object".format(label))
    allowed = {"title", "type", "value"}
    _require_keys(value, allowed, label)
    _reject_extra_keys(value, allowed, label)
    _nonempty_string(value["title"], "{}.title".format(label))
    data_type = _enum(value["type"], DATA_TYPES, "{}.type".format(label))
    data_value = value["value"]
    if data_type == "BOOLEAN" and not isinstance(data_value, bool):
        raise ReviewValidationError("{}.value must be boolean".format(label))
    if data_type in {"NUMBER", "PERCENTAGE"} and not isinstance(data_value, (int, float)):
        raise ReviewValidationError("{}.value must be numeric".format(label))
    if data_type in {"DATE", "DURATION", "LINK", "TEXT"} and not isinstance(data_value, str):
        raise ReviewValidationError("{}.value must be string".format(label))


def _validate_annotations(value: Any, max_findings: int) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise ReviewValidationError("annotations must be an array")
    if len(value) > max_findings:
        raise ReviewValidationError("annotations exceeds max_findings")
    seen_external_ids = set()
    converted = []
    for idx, item in enumerate(value):
        label = "annotations[{}]".format(idx)
        if not isinstance(item, dict):
            raise ReviewValidationError("{} must be an object".format(label))
        allowed = {
            "external_id",
            "annotation_type",
            "path",
            "line",
            "summary",
            "details",
            "severity",
            "result",
            "reviewer",
            "confidence",
            "smallest_fix",
        }
        _require_keys(item, allowed, label)
        _reject_extra_keys(item, allowed, label)
        external_id = _nonempty_string(item["external_id"], "{}.external_id".format(label))
        if external_id in seen_external_ids:
            raise ReviewValidationError("duplicate annotation external_id: {}".format(external_id))
        seen_external_ids.add(external_id)
        _enum(item["annotation_type"], ANNOTATION_TYPES, "{}.annotation_type".format(label))
        path = _nonempty_string(item["path"], "{}.path".format(label))
        if path.startswith("/") or ".." in path.split("/"):
            raise ReviewValidationError("{}.path must be a relative repository path".format(label))
        line = item["line"]
        if not isinstance(line, int) or line < 1:
            raise ReviewValidationError("{}.line must be a positive integer".format(label))
        _nonempty_string(item["summary"], "{}.summary".format(label))
        _nonempty_string(item["details"], "{}.details".format(label))
        _enum(item["severity"], SEVERITIES, "{}.severity".format(label))
        _enum(item["result"], {"FAILED"}, "{}.result".format(label))
        _enum(item["reviewer"], REVIEWERS, "{}.reviewer".format(label))
        _enum(item["confidence"], CONFIDENCE, "{}.confidence".format(label))
        _nonempty_string(item["smallest_fix"], "{}.smallest_fix".format(label))
        converted.append(deepcopy(item))
    return converted


def _format_report_details(review: ValidatedReview, provider_label: str) -> str:
    if not review.annotations:
        return "{} reviewed this pull request and found no material issues.".format(provider_label)
    summary = summarize_findings(review)
    issue_count = summary["total"]
    header = "{} reviewed this pull request and found {} material {}:".format(
        provider_label,
        issue_count,
        "issue" if issue_count == 1 else "issues",
    )
    lines = [header, "", "By category:"]
    for reviewer, count in summary["by_reviewer"].items():
        lines.append(
            "- {reviewer}: {count} ({severities})".format(
                reviewer=_reviewer_label(reviewer),
                count=_format_issue_count(count),
                severities=_format_count_list(summary["by_reviewer_and_severity"][reviewer]),
            )
        )
    lines.extend(["", "By severity:"])
    for severity, count in summary["by_severity"].items():
        lines.append("- {}: {}".format(_sentence_case(severity), count))
    return _truncate("\n".join(lines), BITBUCKET_REPORT_DETAILS_MAX_LENGTH)


def _format_report_title(title: str, provider_label: str) -> str:
    if provider_label.lower() in title.lower():
        return title
    return "{} {}".format(provider_label, title)


def _report_type(review: ValidatedReview) -> str:
    if any(annotation["annotation_type"] == "VULNERABILITY" for annotation in review.annotations):
        return "SECURITY"
    return review.report["report_type"]


def _report_data(
    review: ValidatedReview,
    provider_label: str,
    model_metadata: Optional[str],
) -> List[Dict[str, Any]]:
    data = [
        {"title": "Provider", "type": "TEXT", "value": provider_label},
        {"title": "Findings", "type": "NUMBER", "value": len(review.annotations)},
        {
            "title": "Recommendation",
            "type": "TEXT",
            "value": "Request changes" if review.recommendation == "request_changes" else "Approve",
        },
    ]
    for severity in SEVERITY_ORDER:
        count = sum(1 for annotation in review.annotations if annotation["severity"] == severity)
        if count:
            data.append({"title": _sentence_case(severity), "type": "NUMBER", "value": count})
    if model_metadata:
        data.append({"title": "Model", "type": "TEXT", "value": model_metadata})
    return data


def _format_details(annotation: Dict[str, Any], provider_label: str) -> str:
    return (
        "Why it matters:\n{details}\n\n"
        "Suggested fix:\n{smallest_fix}\n\n"
        "Reviewer: {provider} / {reviewer} / {confidence} confidence"
    ).format(provider=provider_label, **annotation)


def _provider_label(provider: str) -> str:
    labels = {
        "codex": "Codex",
        "claude": "Claude",
        "gemini": "Gemini",
    }
    normalized = provider.lower()
    if normalized in labels:
        return labels[normalized]
    return provider.replace("_", " ").replace("-", " ").title()


def _sentence_case(value: str) -> str:
    return value[:1].upper() + value[1:].lower()


def _reviewer_label(value: str) -> str:
    words = value.replace("-", " ").split()
    if not words:
        return value
    return "{}{}".format(words[0].capitalize(), "".join(" {}".format(word) for word in words[1:]))


def _format_issue_count(count: int) -> str:
    return "{} {}".format(count, "issue" if count == 1 else "issues")


def _format_count_list(counts: Dict[str, int]) -> str:
    return ", ".join("{}: {}".format(_sentence_case(key), value) for key, value in counts.items())


def _nonzero_ordered_counts(counts: Dict[str, int], order: List[str]) -> Dict[str, int]:
    return {key: counts[key] for key in order if counts[key]}


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3].rstrip() + "..."


def _require_keys(value: Dict[str, Any], required: Iterable[str], label: str) -> None:
    missing = sorted(set(required) - set(value))
    if missing:
        raise ReviewValidationError("{} missing required keys: {}".format(label, ", ".join(missing)))


def _reject_extra_keys(value: Dict[str, Any], allowed: Iterable[str], label: str) -> None:
    extra = sorted(set(value) - set(allowed))
    if extra:
        raise ReviewValidationError("{} contains unsupported keys: {}".format(label, ", ".join(extra)))


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReviewValidationError("{} must be a non-empty string".format(label))
    return value


def _enum(value: Any, allowed: Iterable[str], label: str) -> str:
    allowed_set = set(allowed)
    if not isinstance(value, str) or value not in allowed_set:
        raise ReviewValidationError("{} must be one of {}".format(label, ", ".join(sorted(allowed_set))))
    return value
