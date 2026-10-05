from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence


class ReviewValidationError(ValueError):
    pass


RECOMMENDATIONS = {"approve", "request_changes"}
REPORT_TYPES = {"BUG", "SECURITY", "TEST", "COVERAGE"}
DATA_TYPES = {"BOOLEAN", "DATE", "DURATION", "LINK", "NUMBER", "PERCENTAGE", "TEXT"}
ANNOTATION_TYPES = {"BUG", "VULNERABILITY", "CODE_SMELL"}
SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
SEVERITIES = set(SEVERITY_ORDER)
REVIEWER_ORDER = [
    "correctness",
    "security",
    "tests",
    "performance",
    "best-practices",
    "compatibility",
]
REVIEWERS = set(REVIEWER_ORDER)
CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}
LINE_SIDES = {"NEW", "OLD"}
FINDING_KINDS = {"general", "dead_code", "duplicate_code", "low_value_test"}
INTERNAL_EXTERNAL_ID_PREFIX = "__scout_"
BITBUCKET_REPORT_DETAILS_MAX_LENGTH = 2000
BITBUCKET_ANNOTATION_SUMMARY_MAX_LENGTH = 450
BITBUCKET_ANNOTATION_DETAILS_MAX_LENGTH = 2000
BITBUCKET_COMMENT_MAX_LENGTH = 8000
_NONCANONICAL_DIFF_ERROR = "primary PR diff is not canonical git diff output"
_DIFF_METADATA_PREFIXES = (
    "index ",
    "old mode ",
    "new mode ",
    "deleted file mode ",
    "new file mode ",
    "similarity index ",
    "dissimilarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "Binary files ",
)


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


def filter_annotation_locations(
    review: ValidatedReview,
    diff: str,
    allowed_line_sides: Iterable[str] = ("NEW",),
    allow_old_dead_code: bool = False,
) -> ValidatedReview:
    """Return a review containing only annotations publishable in this mode.

    Native inline comments can target either side of the PR diff. Code Insights
    annotations are attached to the source commit and currently support only
    new-side locations, so callers select which sides their output mode can
    publish. Report mode can retain old-side dead code for a separate PR warning;
    Code Insights conversion omits these old-side annotations. If no findings
    remain, the recommendation must also become an approval so every downstream
    summary is derived from the same state.
    """
    allowed_sides = set(allowed_line_sides)
    if not allowed_sides or not allowed_sides.issubset(LINE_SIDES):
        raise ReviewValidationError("allowed_line_sides must contain only NEW or OLD")
    for annotation in review.annotations:
        if annotation.get("line_side") not in LINE_SIDES:
            raise ReviewValidationError("annotation line_side must be NEW or OLD")
    changed_lines = _changed_lines_by_side(diff)
    annotations = [
        annotation
        for annotation in review.annotations
        if (
            annotation["line_side"] in allowed_sides
            or (
                allow_old_dead_code
                and annotation["line_side"] == "OLD"
                and annotation.get("finding_kind") == "dead_code"
            )
        )
        and (annotation["path"], annotation["line"])
        in changed_lines[annotation["line_side"]]
    ]
    if len(annotations) == len(review.annotations):
        return review
    return ValidatedReview(
        recommendation="request_changes" if annotations else "approve",
        report=review.report,
        annotations=annotations,
    )


def _changed_lines_by_side(diff: str) -> Dict[str, set]:
    if not diff:
        return {"NEW": set(), "OLD": set()}

    changed = {"NEW": set(), "OLD": set()}
    old_path = None
    new_path = None
    old_line = None
    new_line = None
    expect_new_path = False
    expect_hunk = False
    saw_new_path = False
    in_hunk = False
    saw_file = False
    section_has_structure = False
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            if (
                expect_new_path
                or expect_hunk
                or (saw_file and not section_has_structure)
                or not _is_canonical_diff_header(line)
            ):
                raise ReviewValidationError(_NONCANONICAL_DIFF_ERROR)
            saw_file = True
            section_has_structure = False
            old_path = None
            new_path = None
            old_line = None
            new_line = None
            expect_new_path = False
            expect_hunk = False
            saw_new_path = False
            in_hunk = False
            continue
        if not saw_file:
            if line:
                raise ReviewValidationError(_NONCANONICAL_DIFF_ERROR)
            continue
        if not in_hunk and line.startswith("--- "):
            if expect_new_path or not _is_canonical_diff_path(line[4:], "a/"):
                raise ReviewValidationError(_NONCANONICAL_DIFF_ERROR)
            old_path = _decode_diff_path(line[4:])
            if old_path == "/dev/null":
                old_path = None
            elif old_path.startswith("a/"):
                old_path = old_path[2:]
            expect_new_path = True
            section_has_structure = True
            continue
        if not in_hunk and line.startswith("+++ "):
            if not expect_new_path or not _is_canonical_diff_path(line[4:], "b/"):
                raise ReviewValidationError(_NONCANONICAL_DIFF_ERROR)
            new_path = _decode_diff_path(line[4:])
            if new_path == "/dev/null":
                new_path = None
            elif new_path.startswith("b/"):
                new_path = new_path[2:]
            expect_new_path = False
            expect_hunk = True
            saw_new_path = True
            section_has_structure = True
            continue
        if line.startswith("@@ "):
            match = re.fullmatch(
                r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(?: .*)?",
                line,
            )
            if match is None or expect_new_path or not saw_new_path:
                raise ReviewValidationError(_NONCANONICAL_DIFF_ERROR)
            old_line = int(match.group(1)) if old_path is not None else None
            new_line = int(match.group(2)) if new_path is not None else None
            expect_hunk = False
            in_hunk = True
            section_has_structure = True
            continue
        if line.startswith("diff --git") or line.startswith("@@"):
            raise ReviewValidationError(_NONCANONICAL_DIFF_ERROR)
        if not in_hunk:
            if not line:
                continue
            if line.startswith(_DIFF_METADATA_PREFIXES):
                section_has_structure = True
                continue
            raise ReviewValidationError(_NONCANONICAL_DIFF_ERROR)
        if line.startswith("+"):
            if new_path is not None and new_line is not None:
                changed["NEW"].add((new_path, new_line))
                new_line += 1
        elif line.startswith("-"):
            if old_path is not None and old_line is not None:
                changed["OLD"].add((old_path, old_line))
                old_line += 1
        elif line.startswith("\\"):
            continue
        else:
            if old_line is not None:
                old_line += 1
            if new_line is not None:
                new_line += 1
    if expect_new_path or expect_hunk or not saw_file or not section_has_structure:
        raise ReviewValidationError(_NONCANONICAL_DIFF_ERROR)
    return changed


def _is_canonical_diff_header(line: str) -> bool:
    paths = line[len("diff --git ") :]
    if paths.startswith('"a/'):
        return '" "b/' in paths and paths.endswith('"')
    return paths.startswith("a/") and " b/" in paths


def _is_canonical_diff_path(value: str, prefix: str) -> bool:
    path = value.split("\t", 1)[0]
    if path == "/dev/null":
        return True
    if path.startswith('"'):
        return path.startswith('"' + prefix) and path.endswith('"')
    return path.startswith(prefix)


def _decode_diff_path(value: str) -> str:
    if not value.startswith('"'):
        return value.split("\t", 1)[0]

    encoded = bytearray()
    index = 1
    escapes = {
        "a": 7,
        "b": 8,
        "t": 9,
        "n": 10,
        "v": 11,
        "f": 12,
        "r": 13,
        '"': 34,
        "\\": 92,
    }
    while index < len(value):
        char = value[index]
        if char == '"':
            break
        if char != "\\":
            encoded.extend(char.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(value):
            encoded.append(92)
            break
        escaped = value[index]
        if escaped in "01234567":
            end = index + 1
            while end < min(index + 3, len(value)) and value[end] in "01234567":
                end += 1
            encoded.append(int(value[index:end], 8))
            index = end
            continue
        encoded.append(escapes.get(escaped, ord(escaped)))
        index += 1
    return encoded.decode("utf-8", errors="replace")


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
        # Removed lines refer to the target side, not the source commit that
        # owns Code Insights. Dead-code findings still appear in PR comments.
        if annotation["line_side"] == "OLD":
            continue
        item = {
            "external_id": annotation["external_id"],
            "annotation_type": annotation["annotation_type"],
            "path": annotation["path"],
            "line": annotation["line"],
            "summary": _truncate(annotation["summary"], BITBUCKET_ANNOTATION_SUMMARY_MAX_LENGTH),
            "details": _truncate(
                _format_details(annotation, provider_label), BITBUCKET_ANNOTATION_DETAILS_MAX_LENGTH
            ),
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
    selected = sorted(
        (
            annotation for annotation in review.annotations
            if annotation["severity"] in allowed_severities
            or annotation.get("finding_kind") == "dead_code"
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
    selected_severities = [
        severity for severity in SEVERITY_ORDER
        if any(annotation["severity"] == severity for annotation in selected)
    ]
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
                "   Location: `{}:{}` ({})".format(
                    annotation["path"],
                    annotation["line"],
                    annotation["line_side"].lower(),
                ),
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


def to_pr_comments(
    review: ValidatedReview,
    provider: str = "codex",
    source_commit: str = "",
    severities: Iterable[str] = ("CRITICAL",),
) -> List[str]:
    """Keep every dead-code warning visible even when another finding is long."""
    comments = []
    ordinary = []
    for annotation in review.annotations:
        if annotation.get("finding_kind") != "dead_code":
            ordinary.append(annotation)
            continue
        path = annotation["path"]
        path_note = ""
        # Normal checkout paths fit in this budget. An exceptional longer path
        # must not consume the summary or disguise the shortening as a real path.
        path_budget = BITBUCKET_COMMENT_MAX_LENGTH - 2000
        if len(path) > path_budget:
            prefix_length = (path_budget - 3) // 2
            path = path[:prefix_length] + "..." + path[-(path_budget - 3 - prefix_length):]
            path_note = " [path shortened]"
        lines = [
            "**Scout: {} issue found by {}:**".format(
                _sentence_case(annotation["severity"]), _provider_label(provider)
            ),
            "",
            "Location: `{}:{}` ({}){}".format(
                path, annotation["line"], annotation["line_side"].lower(), path_note
            ),
            "**{}**".format(_truncate(annotation["summary"], BITBUCKET_ANNOTATION_SUMMARY_MAX_LENGTH)),
        ]
        if source_commit:
            lines.append("Commit: `{}`".format(source_commit[:12]))
        lines.extend([
            "Reviewer: {} / {} confidence".format(
                _reviewer_label(annotation["reviewer"]), annotation["confidence"]
            ),
            "Why it matters: {}".format(annotation["details"]),
            "Smallest fix: {}".format(annotation["smallest_fix"]),
        ])
        comments.append(_truncate("\n".join(lines), BITBUCKET_COMMENT_MAX_LENGTH))
    ordinary_comment = to_pr_comment(
        ValidatedReview(review.recommendation, review.report, ordinary),
        provider=provider, source_commit=source_commit, severities=severities,
    )
    if ordinary_comment:
        comments.append(ordinary_comment)
    return comments


def to_critical_pr_comment(
    review: ValidatedReview,
    provider: str = "codex",
    source_commit: str = "",
) -> str:
    return to_pr_comment(review, provider=provider, source_commit=source_commit, severities=("CRITICAL",))


def to_no_findings_pr_comment(review: ValidatedReview, provider: str = "codex") -> str:
    if review.annotations:
        return ""
    provider_label = _provider_label(provider)
    return "Scout: {} reviewed this pull request and found no material issues.".format(provider_label)


def to_inline_pr_comments(
    review: ValidatedReview,
    provider: str = "codex",
    source_commit: str = "",
    review_run_id: str = "",
) -> List[Dict[str, Any]]:
    provider_label = _provider_label(provider)
    comments = []
    for annotation in sorted(
        review.annotations,
        key=lambda item: (
            item["path"],
            item["line"],
            item["line_side"],
            item["external_id"],
        ),
    ):
        comments.append(
            {
                "external_id": annotation["external_id"],
                "path": annotation["path"],
                "line": annotation["line"],
                "line_side": annotation["line_side"],
                "content": _format_inline_comment(annotation, provider_label, source_commit, review_run_id),
            }
        )
    return comments


def to_outdated_pr_comment(
    annotation: Dict[str, Any], provider: str, source_commit: str, publication_marker: str = "",
) -> str:
    """Describe a saved finding without anchoring its old line to the current diff."""
    path = annotation["path"]
    path_note = ""
    if len(path) > 2000:
        path = path[:1000] + "..." + path[-1000:]
        path_note = " [path shortened]"
    header = (
        "Scout saved this finding for original commit `{}`. The PR revision has changed; "
        "this location refers to the original review.\n"
        "Original location: `{}:{}` ({}){}\n\n"
    ).format(source_commit, path, annotation["line"], annotation["line_side"], path_note)
    # A saved replacement targets the old diff; it must not be offered as an
    # applyable suggestion against the PR's newer source.
    original = {key: value for key, value in annotation.items() if key != "suggested_change"}
    body = _format_inline_comment(original, _provider_label(provider), source_commit, "")
    body = re.sub(r"(?m)^([ \t]*)(`{3,}|~{3,})suggestion\b", r"\1\2text", body)
    # Truncation must never remove the marker that publication recovery searches for.
    suffix = "\n" + publication_marker if publication_marker else ""
    return header + _truncate(body, BITBUCKET_COMMENT_MAX_LENGTH - len(header) - len(suffix)) + suffix


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
        required = {
            "external_id",
            "annotation_type",
            "path",
            "line",
            "line_side",
            "summary",
            "details",
            "severity",
            "result",
            "reviewer",
            "confidence",
            "smallest_fix",
        }
        allowed = required | {"suggested_change", "finding_kind"}
        _require_keys(item, required, label)
        _reject_extra_keys(item, allowed, label)
        external_id = _nonempty_string(item["external_id"], "{}.external_id".format(label))
        if external_id.startswith(INTERNAL_EXTERNAL_ID_PREFIX):
            raise ReviewValidationError(
                "{} must not use reserved Scout prefix: {}".format(label, INTERNAL_EXTERNAL_ID_PREFIX)
            )
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
        _enum(item["line_side"], LINE_SIDES, "{}.line_side".format(label))
        _nonempty_string(item["summary"], "{}.summary".format(label))
        _nonempty_string(item["details"], "{}.details".format(label))
        _enum(item["severity"], SEVERITIES, "{}.severity".format(label))
        _enum(item["result"], {"FAILED"}, "{}.result".format(label))
        _enum(item["reviewer"], REVIEWERS, "{}.reviewer".format(label))
        _enum(item["confidence"], CONFIDENCE, "{}.confidence".format(label))
        _nonempty_string(item["smallest_fix"], "{}.smallest_fix".format(label))
        if "finding_kind" in item:
            _enum(item["finding_kind"], FINDING_KINDS, "{}.finding_kind".format(label))
        if "suggested_change" in item:
            _validate_suggested_change(item["suggested_change"], "{}.suggested_change".format(label))
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


def _format_inline_comment(
    annotation: Dict[str, Any],
    provider_label: str,
    source_commit: str,
    review_run_id: str,
    publication_marker: str = "",
) -> str:
    body = "\n".join(
        [
            "**{}**".format(annotation["summary"]),
            "",
            "What I found:",
            annotation["details"],
            "",
            "Smallest fix:",
            annotation["smallest_fix"],
        ]
    )
    footer = "Scout: {} issue found by {}. Reviewer: {} / {} confidence".format(
        _sentence_case(annotation["severity"]),
        provider_label,
        _reviewer_label(annotation["reviewer"]),
        annotation["confidence"],
    )
    if publication_marker:
        footer += "\n" + publication_marker
    suggested_change = annotation.get("suggested_change")
    if isinstance(suggested_change, dict) and isinstance(suggested_change.get("replacement"), str):
        replacement = suggested_change["replacement"]
        suggestion = "\n\nSuggested change:\n\n```suggestion\n{}\n```".format(replacement)
        content_with_suggestion = _format_inline_comment_parts(body + suggestion, footer)
        if len(content_with_suggestion) <= BITBUCKET_COMMENT_MAX_LENGTH:
            return content_with_suggestion
    return _format_inline_comment_parts(body, footer, limit=BITBUCKET_COMMENT_MAX_LENGTH)


def to_round_notice(round_record: Dict[str, Any], kind: str, covered: Sequence[Dict[str, Any]] = ()) -> str:
    """`covered` lists the open comments (path, line, title) that cover a covered_review round."""
    succeeded = [_provider_label(o["provider"]) for o in round_record["outcomes"] if o["status"] == "succeeded"]
    failed = [_provider_label(o["provider"]) for o in round_record["outcomes"] if o["status"] == "failed"]
    snapshot = "`{}` (base `{}`)".format(round_record["source_commit_hash"][:12], (round_record.get("merge_base_hash") or "unknown")[:12])
    if kind == "clean_review":
        content = "Scout: {} found no material issues on {}.".format(", ".join(succeeded), snapshot)
    elif kind == "covered_review":
        content = ("Scout: {} reviewed {}. Every finding is already reported in an open Scout comment, "
                   "so no new comments were posted.").format(", ".join(succeeded), snapshot)
    else:
        content = "Scout reviewed {} with {}. Findings reflect these providers' reviews only.".format(snapshot, ", ".join(succeeded))
    if failed:
        content += " {} did not complete this review (provider unavailable).".format(", ".join(failed))
    if covered:
        content += "\n\nStill open:\n" + "\n".join(
            "- `{}:{}` {}".format(item["path"], item["line"], item["title"]) for item in covered)
    return _truncate(content, BITBUCKET_COMMENT_MAX_LENGTH - 200)


def _format_inline_comment_parts(
    body: str,
    footer: str,
    limit: Optional[int] = None,
) -> str:
    suffix = "\n\n" + footer
    if limit is None or len(body) + len(suffix) <= limit:
        return body + suffix
    body_limit = limit - len(suffix)
    if body_limit <= 0:
        return _truncate(footer, limit)
    return _truncate(body, body_limit) + suffix


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


def _validate_suggested_change(value: Any, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ReviewValidationError("{} must be an object".format(label))
    _require_keys(value, {"replacement"}, label)
    _reject_extra_keys(value, {"replacement"}, label)
    replacement = value["replacement"]
    if not isinstance(replacement, str):
        raise ReviewValidationError("{}.replacement must be string".format(label))
    if not replacement.strip():
        raise ReviewValidationError("{}.replacement must not be empty".format(label))
    if "\n" in replacement or "\r" in replacement:
        raise ReviewValidationError("{}.replacement must be a single line".format(label))
    if "```" in replacement:
        raise ReviewValidationError("{}.replacement must not contain triple backticks".format(label))


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
