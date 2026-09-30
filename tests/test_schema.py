import json
import re
import unittest
from pathlib import Path

from scout.schema import (
    BITBUCKET_COMMENT_MAX_LENGTH,
    ReviewValidationError,
    _changed_lines_by_side,
    summarize_findings,
    parse_review_json,
    report_result_for_recommendation,
    to_bitbucket_annotations,
    to_critical_pr_comment,
    to_inline_pr_comments,
    to_no_findings_pr_comment,
    to_outdated_pr_comment,
    to_pr_comment,
    to_pr_comments,
    to_bitbucket_report,
    filter_annotation_locations,
    validate_review_output,
)


def valid_review():
    return {
        "recommendation": "request_changes",
        "report": {
            "title": "AI Pull Request Review",
            "details": "Found one issue.",
            "report_type": "BUG",
            "reporter": "scout",
            "data": [
                {"title": "Findings", "type": "NUMBER", "value": 1},
                {"title": "Recommendation", "type": "TEXT", "value": "request_changes"},
            ],
        },
        "annotations": [
            {
                "external_id": "finding-001",
                "annotation_type": "BUG",
                "path": "src/app.py",
                "line": 12,
                "line_side": "NEW",
                "summary": "Missing error handling",
                "details": "The changed call can raise and leave state half-updated.",
                "severity": "HIGH",
                "result": "FAILED",
                "reviewer": "correctness",
                "confidence": "HIGH",
                "smallest_fix": "Catch the exception and roll back the state update.",
            }
        ],
    }


class SchemaTests(unittest.TestCase):
    def test_finding_kind_is_optional_for_existing_reviews_and_validated_when_present(self):
        self.assertNotIn("finding_kind", validate_review_output(valid_review()).annotations[0])
        for kind in ("general", "dead_code", "duplicate_code", "low_value_test"):
            with self.subTest(kind=kind):
                payload = valid_review()
                payload["annotations"][0]["finding_kind"] = kind
                review = validate_review_output(payload)
                self.assertEqual(review.annotations[0]["finding_kind"], kind)
                self.assertNotIn("finding_kind", to_bitbucket_annotations(review)[0])
        for kind in ("unknown", None, 1):
            with self.subTest(kind=kind):
                payload = valid_review()
                payload["annotations"][0]["finding_kind"] = kind
                with self.assertRaisesRegex(ReviewValidationError, "finding_kind must be one of"):
                    validate_review_output(payload)

    def test_provider_schemas_require_finding_kind(self):
        root = Path(__file__).resolve().parents[1]
        for relative_path in ("config/review.schema.json", "src/scout/data/review.schema.json"):
            schema = json.loads((root / relative_path).read_text(encoding="utf-8"))
            annotation = schema["properties"]["annotations"]["items"]
            self.assertIn("finding_kind", annotation["required"])
            self.assertEqual(
                annotation["properties"]["finding_kind"]["enum"],
                ["general", "dead_code", "duplicate_code", "low_value_test"],
            )

    def test_dead_code_always_gets_pr_warning_with_truthful_severity_heading(self):
        for side in ("NEW", "OLD"):
            for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
                for severities in (("CRITICAL",), ()):
                    with self.subTest(side=side, severity=severity, severities=severities):
                        payload = valid_review()
                        payload["annotations"][0].update(
                            finding_kind="dead_code", line_side=side, severity=severity,
                            summary="Last caller removed",
                        )
                        review = validate_review_output(payload)
                        comment = to_pr_comment(review, severities=severities)
                        self.assertIn("Last caller removed", comment)
                        self.assertIn("Scout: {} issue found".format(severity.capitalize()), comment)
                        self.assertIn("src/app.py:12", comment)
                        self.assertIn("({})".format(side.lower()), comment)

    def test_dead_code_comment_does_not_promote_other_lower_severity_findings(self):
        payload = valid_review()
        for kind in ("dead_code", "duplicate_code", "low_value_test"):
            annotation = dict(payload["annotations"][0])
            annotation.update(external_id=kind, summary=kind, finding_kind=kind, severity="LOW")
            payload["annotations"].append(annotation)
        payload["annotations"][0]["severity"] = "CRITICAL"
        comment = to_pr_comment(validate_review_output(payload))
        self.assertIn("Scout: Issues found by Codex:", comment)
        self.assertIn("Missing error handling", comment)
        self.assertIn("dead_code", comment)
        self.assertNotIn("duplicate_code", comment)
        self.assertNotIn("low_value_test", comment)

    def test_dead_code_warning_survives_long_earlier_critical_finding(self):
        payload = valid_review()
        payload["annotations"][0].update(severity="CRITICAL", details="d" * 8100)
        dead_code = dict(payload["annotations"][0])
        dead_code.update(
            external_id="dead-code", finding_kind="dead_code", severity="LOW",
            line_side="OLD", summary="Unused old helper", path="src/unused.c", line=23,
        )
        payload["annotations"].append(dead_code)
        review = validate_review_output(payload)

        comments = to_pr_comments(review)

        self.assertEqual(len(comments), 2)
        self.assertIn("Unused old helper", comments[0])
        self.assertIn("src/unused.c:23` (old)", comments[0])
        self.assertIn("Missing error handling", comments[1])
        self.assertNotIn("Unused old helper", comments[1])
        self.assertTrue(all(len(comment) <= BITBUCKET_COMMENT_MAX_LENGTH for comment in comments))

    def test_each_long_dead_code_warning_preserves_summary_and_location(self):
        payload = valid_review()
        payload["annotations"] = []
        for index, side in enumerate(("NEW", "OLD")):
            annotation = dict(valid_review()["annotations"][0])
            annotation.update(
                external_id="dead-{}".format(index), finding_kind="dead_code", severity="LOW",
                summary="Unused helper {} ".format(index) + "s" * 8100,
                details="d" * 8100, smallest_fix="f" * 8100,
                path="src/" + "nested/" * 1500 + "unused{}.c".format(index),
                line=index + 10, line_side=side,
            )
            payload["annotations"].append(annotation)
        review = validate_review_output(payload)

        comments = to_pr_comments(review, severities=())

        self.assertEqual(len(comments), 2)
        for index, comment in enumerate(comments):
            self.assertIn("Unused helper {}".format(index), comment)
            self.assertIn("Location: `src/", comment)
            self.assertIn("[path shortened]", comment)
            self.assertIn("unused{}.c:{}`".format(index, index + 10), comment)
            self.assertIn("({})".format(("new", "old")[index]), comment)
            self.assertLessEqual(len(comment), BITBUCKET_COMMENT_MAX_LENGTH)
        self.assertEqual(len(review.annotations[0]["summary"]), 8116)
        self.assertNotIn("...", review.annotations[0]["path"])

    def test_dead_code_warning_keeps_full_checkout_location_before_long_summary(self):
        payload = valid_review()
        path = "src/" + "nested/" * 500 + "unused.c"
        payload["annotations"][0].update(
            finding_kind="dead_code", path=path, summary="Unused helper " + "s" * 8100,
        )

        comment = to_pr_comments(validate_review_output(payload))[0]

        self.assertIn("Location: `{}:12` (new)".format(path), comment)
        self.assertNotIn("[path shortened]", comment)
        self.assertLess(comment.index("Location:"), comment.index("Unused helper"))
        self.assertLessEqual(len(comment), BITBUCKET_COMMENT_MAX_LENGTH)

    def test_pr_comments_keep_normal_severity_selection_and_aggregate_format(self):
        review = validate_review_output(valid_review())
        self.assertEqual(to_pr_comments(review), [])
        self.assertEqual(
            to_pr_comments(review, provider="claude", source_commit="abc123", severities=("HIGH",)),
            [to_pr_comment(review, provider="claude", source_commit="abc123", severities=("HIGH",))],
        )

    def test_report_mode_preserves_only_valid_old_dead_code_for_pr_comment(self):
        payload = valid_review()
        for kind in ("dead_code", "general", "duplicate_code", "low_value_test"):
            annotation = dict(payload["annotations"][0])
            annotation.update(
                external_id=kind, finding_kind=kind, line=5, line_side="OLD", severity="LOW",
            )
            payload["annotations"].append(annotation)
        invalid = dict(payload["annotations"][1])
        invalid.update(external_id="unchanged-dead-code", line=6)
        payload["annotations"].append(invalid)
        review = validate_review_output(payload)
        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -5,2 +5 @@
-last_call()
 context
"""
        filtered = filter_annotation_locations(review, diff, allow_old_dead_code=True)
        self.assertEqual([a["external_id"] for a in filtered.annotations], ["dead_code"])
        self.assertEqual(filtered.recommendation, "request_changes")
        self.assertIn("(old)", to_pr_comment(filtered))
        self.assertEqual(to_bitbucket_annotations(filtered), [])
        self.assertEqual(to_bitbucket_report(filtered, "Review")["result"], "FAILED")
        self.assertEqual(filter_annotation_locations(review, diff).annotations, [])

    def test_changed_lines_use_physical_lines_on_both_sides(self):
        for separator in ("\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                diff = (
                    "diff --git a/app.py b/app.py\n"
                    "--- a/app.py\n+++ b/app.py\n"
                    "@@ -1,4 +1,4 @@\n"
                    " context\n"
                    "-old{0}content\n-old again\n-old last\n"
                    "+new{0}content\n+new again\n+new last\n"
                ).format(separator)

                self.assertEqual(
                    _changed_lines_by_side(diff),
                    {
                        "OLD": {("app.py", 2), ("app.py", 3), ("app.py", 4)},
                        "NEW": {("app.py", 2), ("app.py", 3), ("app.py", 4)},
                    },
                )

    def test_code_insights_annotations_bound_final_text_without_changing_inline_comments(self):
        payload = valid_review()
        annotation = payload["annotations"][0]
        annotation["summary"] = "s" * 451
        annotation["details"] = "d" * 1950
        annotation["smallest_fix"] = "f" * 100
        review = validate_review_output(payload)

        converted = to_bitbucket_annotations(review)[0]
        inline = to_inline_pr_comments(review)[0]["content"]

        self.assertEqual(len(converted["summary"]), 450)
        self.assertEqual(len(converted["details"]), 2000)
        self.assertTrue(converted["summary"].endswith("..."))
        self.assertTrue(converted["details"].endswith("..."))
        self.assertIn(annotation["summary"], inline)
        self.assertIn(annotation["details"], inline)
        self.assertIn(annotation["smallest_fix"], inline)
        self.assertEqual(review.annotations[0], annotation)

    def test_validate_and_convert_review(self):
        review = validate_review_output(valid_review())
        self.assertEqual(report_result_for_recommendation(review.recommendation), "FAILED")
        report = to_bitbucket_report(review, "Codex PR Review", provider="codex")
        annotations = to_bitbucket_annotations(review, provider="codex")
        self.assertEqual(report["title"], "Codex PR Review")
        self.assertEqual(report["result"], "FAILED")
        self.assertIn("Codex reviewed this pull request and found 1 material issue", report["details"])
        self.assertIn("By category:", report["details"])
        self.assertIn("- Correctness: 1 issue (High: 1)", report["details"])
        self.assertIn("By severity:", report["details"])
        self.assertIn("- High: 1", report["details"])
        self.assertNotIn("Missing error handling", report["details"])
        self.assertIn({"title": "Provider", "type": "TEXT", "value": "Codex"}, report["data"])
        self.assertEqual(
            [item["title"] for item in report["data"][:3]],
            ["Provider", "Findings", "Recommendation"],
        )
        self.assertEqual(annotations[0]["external_id"], "finding-001")
        self.assertIn("Why it matters:\n", annotations[0]["details"])
        self.assertIn("Suggested fix:\n", annotations[0]["details"])
        self.assertIn("Reviewer: Codex / correctness / HIGH confidence", annotations[0]["details"])
        self.assertNotIn("smallest_fix", annotations[0])
        self.assertNotIn("line_side", annotations[0])

    def test_report_data_can_include_model_metadata(self):
        review = validate_review_output(valid_review())

        report = to_bitbucket_report(
            review,
            "Codex PR Review",
            provider="codex",
            model_metadata="gpt-5.5 / high",
        )

        self.assertEqual(
            [item["title"] for item in report["data"][:3]],
            ["Provider", "Findings", "Recommendation"],
        )
        self.assertIn({"title": "High", "type": "NUMBER", "value": 1}, report["data"])
        self.assertEqual(report["data"][-1], {"title": "Model", "type": "TEXT", "value": "gpt-5.5 / high"})

    def test_approve_report_is_readable(self):
        payload = valid_review()
        payload["recommendation"] = "approve"
        payload["annotations"] = []
        review = validate_review_output(payload)
        report = to_bitbucket_report(review, "Codex PR Review", provider="codex")
        self.assertEqual(report["result"], "PASSED")
        self.assertEqual(report["details"], "Codex reviewed this pull request and found no material issues.")

    def test_generic_report_title_gets_provider_prefix(self):
        review = validate_review_output(valid_review())
        report = to_bitbucket_report(review, "AI Pull Request Review", provider="codex")
        self.assertEqual(report["title"], "Codex AI Pull Request Review")

    def test_report_details_do_not_copy_finding_summaries(self):
        payload = valid_review()
        payload["annotations"][0]["summary"] = "Commit a73e93e78b5c misses error handling"
        review = validate_review_output(payload)
        report = to_bitbucket_report(review, "Codex PR Review", provider="codex")
        self.assertNotIn("Commit [commit] misses error handling", report["details"])
        self.assertNotIn("a73e93e78b5c", report["details"])

    def test_report_details_summarize_many_findings_under_bitbucket_limit(self):
        payload = valid_review()
        payload["annotations"] = []
        for index in range(60):
            annotation = dict(valid_review()["annotations"][0])
            annotation["external_id"] = "finding-{:03d}".format(index)
            annotation["summary"] = "Long material finding summary " + ("x" * 80)
            annotation["reviewer"] = [
                "correctness",
                "security",
                "tests",
                "performance",
                "best-practices",
                "compatibility",
            ][index % 6]
            annotation["severity"] = ["CRITICAL", "HIGH", "MEDIUM", "LOW"][index % 4]
            payload["annotations"].append(annotation)
        review = validate_review_output(payload)
        report = to_bitbucket_report(review, "Codex PR Review", provider="codex")
        self.assertLessEqual(len(report["details"]), 2000)
        self.assertIn("- Correctness: 10 issues", report["details"])
        self.assertIn("- Critical: 15", report["details"])
        self.assertNotIn("Long material finding summary", report["details"])

    def test_summarize_findings_counts_by_reviewer_and_severity(self):
        payload = valid_review()
        second = dict(valid_review()["annotations"][0])
        second["external_id"] = "finding-002"
        second["annotation_type"] = "VULNERABILITY"
        second["reviewer"] = "security"
        second["severity"] = "CRITICAL"
        third = dict(valid_review()["annotations"][0])
        third["external_id"] = "finding-003"
        third["severity"] = "MEDIUM"
        payload["annotations"].extend([second, third])
        review = validate_review_output(payload)

        summary = summarize_findings(review)

        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["by_reviewer"], {"correctness": 2, "security": 1})
        self.assertEqual(summary["by_severity"], {"CRITICAL": 1, "HIGH": 1, "MEDIUM": 1})
        self.assertEqual(summary["by_reviewer_and_severity"]["correctness"], {"HIGH": 1, "MEDIUM": 1})

    def test_compatibility_annotations_validate_and_summarize_in_lens_order(self):
        payload = valid_review()
        best_practices = dict(valid_review()["annotations"][0])
        best_practices["external_id"] = "finding-002"
        best_practices["reviewer"] = "best-practices"
        compatibility = dict(valid_review()["annotations"][0])
        compatibility["external_id"] = "finding-003"
        compatibility["reviewer"] = "compatibility"
        payload["annotations"] = [compatibility, best_practices, payload["annotations"][0]]

        review = validate_review_output(payload)
        summary = summarize_findings(review)
        annotations = to_bitbucket_annotations(review, provider="codex")
        report = to_bitbucket_report(review, "Codex PR Review", provider="codex")

        self.assertEqual(
            list(summary["by_reviewer"]),
            ["correctness", "best-practices", "compatibility"],
        )
        self.assertIn("Reviewer: Codex / compatibility / HIGH confidence", annotations[0]["details"])
        self.assertLess(report["details"].index("- Correctness:"), report["details"].index("- Best practices:"))
        self.assertLess(report["details"].index("- Best practices:"), report["details"].index("- Compatibility:"))

    def test_critical_pr_comment_only_includes_critical_findings(self):
        payload = valid_review()
        critical = dict(valid_review()["annotations"][0])
        critical["external_id"] = "finding-002"
        critical["summary"] = "Critical data loss"
        critical["severity"] = "CRITICAL"
        critical["confidence"] = "HIGH"
        payload["annotations"].append(critical)
        review = validate_review_output(payload)

        comment = to_critical_pr_comment(review, provider="codex", source_commit="a" * 40)

        self.assertIn("Scout: Critical issue found by Codex:", comment)
        self.assertIn("Critical data loss", comment)
        self.assertNotIn("Missing error handling", comment)
        self.assertIn("`src/app.py:12`", comment)

    def test_critical_pr_comment_is_empty_without_critical_findings(self):
        review = validate_review_output(valid_review())

        self.assertEqual(to_critical_pr_comment(review, provider="codex", source_commit="a" * 40), "")

    def test_pr_comment_can_include_configured_severities(self):
        payload = valid_review()
        medium = dict(valid_review()["annotations"][0])
        medium["external_id"] = "finding-002"
        medium["summary"] = "Medium issue"
        medium["severity"] = "MEDIUM"
        low = dict(valid_review()["annotations"][0])
        low["external_id"] = "finding-003"
        low["summary"] = "Low issue"
        low["severity"] = "LOW"
        payload["annotations"].extend([medium, low])
        review = validate_review_output(payload)

        comment = to_pr_comment(
            review,
            provider="claude",
            source_commit="a" * 40,
            severities=("HIGH", "MEDIUM"),
        )

        self.assertIn("Scout: Issues found by Claude:", comment)
        self.assertIn("Missing error handling", comment)
        self.assertIn("Severity: High", comment)
        self.assertIn("Medium issue", comment)
        self.assertIn("Severity: Medium", comment)
        self.assertNotIn("Low issue", comment)

    def test_inline_pr_comments_include_every_annotation_without_severity_filter(self):
        payload = valid_review()
        low = dict(valid_review()["annotations"][0])
        low["external_id"] = "finding-002"
        low["summary"] = "Low issue"
        low["severity"] = "LOW"
        low["line"] = 14
        payload["annotations"].append(low)
        review = validate_review_output(payload)

        comments = to_inline_pr_comments(
            review,
            provider="codex",
            source_commit="a" * 40,
            review_run_id="run",
        )

        self.assertEqual(
            [(comment["path"], comment["line"], comment["line_side"]) for comment in comments],
            [("src/app.py", 12, "NEW"), ("src/app.py", 14, "NEW")],
        )
        self.assertEqual(
            "Scout: High issue found by Codex. Reviewer: Correctness / HIGH confidence",
            comments[0]["content"].splitlines()[-1],
        )
        self.assertEqual(
            "Scout: Low issue found by Codex. Reviewer: Correctness / HIGH confidence",
            comments[1]["content"].splitlines()[-1],
        )
        self.assertIn("What I found:\n", comments[0]["content"])
        self.assertNotIn("Why it matters:", comments[0]["content"])
        self.assertIn("Smallest fix:", comments[0]["content"])
        self.assertNotIn("`aaaaaaaaaaaa`", comments[0]["content"])
        self.assertNotIn("Commit:", comments[0]["content"])
        self.assertNotIn("Scout finding:", comments[0]["content"])
        self.assertNotIn("Scout review run:", comments[0]["content"])
        self.assertNotIn("<!-- scout-finding:", comments[0]["content"])
        self.assertNotIn("<!-- scout-review-run:", comments[0]["content"])
        self.assertNotIn("finding-001", comments[0]["content"])

    def test_no_findings_pr_comment_mentions_review_without_metadata(self):
        payload = valid_review()
        payload["recommendation"] = "approve"
        payload["annotations"] = []
        review = validate_review_output(payload)

        comment = to_no_findings_pr_comment(review, provider="codex")

        self.assertEqual(
            comment,
            "Scout: Codex reviewed this pull request and found no material issues.",
        )
        self.assertNotIn("Commit:", comment)
        self.assertNotIn("review run", comment.lower())
        self.assertNotIn("external_id", comment)
        self.assertNotIn("Scout finding:", comment)
        self.assertNotIn("Scout review run:", comment)
        self.assertNotIn("<!-- scout-finding:", comment)
        self.assertNotIn("<!-- scout-review-run:", comment)

    def test_suggested_change_renders_only_in_inline_pr_comments(self):
        payload = valid_review()
        payload["annotations"][0]["suggested_change"] = {"replacement": "return fallback"}
        review = validate_review_output(payload)

        comments = to_inline_pr_comments(review, provider="codex", source_commit="a" * 40)
        report_annotations = to_bitbucket_annotations(review, provider="codex")
        pr_comment = to_pr_comment(review, provider="codex", severities=("HIGH",))

        self.assertIn(
            "Suggested change:\n\n```suggestion\nreturn fallback\n```",
            comments[0]["content"],
        )
        self.assertIn("Smallest fix:", comments[0]["content"])
        self.assertNotIn("Suggested change:", report_annotations[0]["details"])
        self.assertNotIn("```suggestion", pr_comment)

    def test_outdated_comment_preserves_original_location_without_applyable_suggestions(self):
        annotation = valid_review()["annotations"][0]
        annotation["line_side"] = "OLD"
        annotation["suggested_change"] = {"replacement": "old_replacement()"}
        annotation["details"] = "Evidence\n```suggestion\nembedded_replacement()\n```\n" + "Long explanation. " * 1000
        content = to_outdated_pr_comment(annotation, "codex", "a" * 40)
        self.assertLessEqual(len(content), 8000)
        self.assertIn("original commit `{}`".format("a" * 40), content)
        self.assertIn("Original location: `src/app.py:12` (OLD)", content)
        self.assertIn("The PR revision has changed", content)
        self.assertNotIn("```suggestion", content)
        self.assertNotIn("old_replacement()", content)
        annotation["path"] = "long/" * 2000 + "end.py"
        content = to_outdated_pr_comment(annotation, "codex", "a" * 40)
        self.assertLessEqual(len(content), 8000)
        self.assertIn("end.py:12` (OLD) [path shortened]", content)

    def test_suggested_change_replacement_validation(self):
        invalid_values = [
            {"replacement": ""},
            {"replacement": "   "},
            {"replacement": "line one\nline two"},
            {"replacement": "line one\rline two"},
            {"replacement": "value ``` suffix"},
            {"replacement": 42},
            {"replacement": "return fallback", "extra": "unsupported"},
            "return fallback",
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                payload = valid_review()
                payload["annotations"][0]["suggested_change"] = value
                with self.assertRaises(ReviewValidationError):
                    validate_review_output(payload)

    def test_annotation_external_id_rejects_reserved_scout_prefix(self):
        payload = valid_review()
        payload["annotations"][0]["external_id"] = "__scout_no_findings__"

        with self.assertRaises(ReviewValidationError):
            validate_review_output(payload)

    def test_suggested_change_null_is_no_suggestion(self):
        payload = valid_review()
        payload["annotations"][0]["suggested_change"] = None

        review = validate_review_output(payload)
        comments = to_inline_pr_comments(review, provider="codex", source_commit="a" * 40)

        self.assertNotIn("Suggested change:", comments[0]["content"])
        self.assertNotIn("```suggestion", comments[0]["content"])

    def test_inline_suggested_change_is_omitted_when_it_would_exceed_comment_limit(self):
        payload = valid_review()
        payload["annotations"][0]["details"] = "x" * (BITBUCKET_COMMENT_MAX_LENGTH - 400)
        payload["annotations"][0]["suggested_change"] = {"replacement": "y" * 500}
        review = validate_review_output(payload)

        comments = to_inline_pr_comments(review, provider="codex", source_commit="a" * 40)

        self.assertLessEqual(len(comments[0]["content"]), BITBUCKET_COMMENT_MAX_LENGTH)
        self.assertIn("Smallest fix:", comments[0]["content"])
        self.assertNotIn("Suggested change:", comments[0]["content"])
        self.assertNotIn("```suggestion", comments[0]["content"])

    def test_provider_schema_requires_all_object_properties(self):
        root = Path(__file__).resolve().parents[1]
        config_schema = json.loads((root / "config/review.schema.json").read_text(encoding="utf-8"))

        def check_object_schemas(schema, path):
            if isinstance(schema, dict):
                properties = schema.get("properties")
                if properties is not None:
                    required = schema.get("required")
                    self.assertIsInstance(required, list, path)
                    self.assertEqual(set(properties), set(required), path)
                for key, value in schema.items():
                    check_object_schemas(value, "{}.{}".format(path, key))
            elif isinstance(schema, list):
                for index, value in enumerate(schema):
                    check_object_schemas(value, "{}[{}]".format(path, index))

        check_object_schemas(config_schema, "$")

    def test_review_schema_files_declare_required_nullable_suggested_change(self):
        root = Path(__file__).resolve().parents[1]
        config_schema = json.loads((root / "config/review.schema.json").read_text(encoding="utf-8"))
        data_schema = json.loads((root / "src/scout/data/review.schema.json").read_text(encoding="utf-8"))

        self.assertEqual(config_schema, data_schema)
        annotation_schema = config_schema["properties"]["annotations"]["items"]

        self.assertEqual(
            annotation_schema["properties"]["reviewer"]["enum"],
            ["correctness", "security", "tests", "performance", "best-practices", "compatibility"],
        )
        self.assertIn("line_side", annotation_schema["required"])
        self.assertEqual(annotation_schema["properties"]["line_side"]["enum"], ["NEW", "OLD"])

        self.assertIn("suggested_change", annotation_schema["required"])
        self.assertEqual(
            annotation_schema["properties"]["suggested_change"],
            {
                "type": ["object", "null"],
                "additionalProperties": False,
                "required": ["replacement"],
                "properties": {
                    "replacement": {
                        "type": "string",
                        "minLength": 1,
                        "pattern": "^[^\\S\\r\\n`]*(`{1,2}[^\\S\\r\\n`]+)*`{0,2}[^\\s\\r\\n`][^\\r\\n`]*(`{1,2}[^\\r\\n`]+)*`{0,2}$",
                    }
                },
            },
        )

    def test_provider_schema_suggested_change_replacement_pattern(self):
        root = Path(__file__).resolve().parents[1]
        config_schema = json.loads((root / "config/review.schema.json").read_text(encoding="utf-8"))
        data_schema = json.loads((root / "src/scout/data/review.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(config_schema, data_schema)

        pattern = config_schema["properties"]["annotations"]["items"]["properties"]["suggested_change"][
            "properties"
        ]["replacement"]["pattern"]
        regex = re.compile(pattern)

        for value in ("return fallback", "  `  fallback", "value ` suffix", "value `` suffix"):
            with self.subTest(value=value):
                self.assertIsNotNone(regex.fullmatch(value))

        for value in ("", "   ", "line one\nline two", "line one\rline two", "value ``` suffix"):
            with self.subTest(value=value):
                self.assertIsNone(regex.fullmatch(value))

    def test_provider_schema_patterns_do_not_use_lookaround(self):
        root = Path(__file__).resolve().parents[1]
        schema_paths = [
            root / "config/review.schema.json",
            root / "src/scout/data/review.schema.json",
        ]
        lookaround_constructs = ("(?=", "(?!", "(?<=", "(?<!")

        def check_patterns(schema, path):
            if isinstance(schema, dict):
                pattern = schema.get("pattern")
                if pattern is not None:
                    for construct in lookaround_constructs:
                        self.assertNotIn(construct, pattern, "{}.pattern".format(path))
                for key, value in schema.items():
                    check_patterns(value, "{}.{}".format(path, key))
            elif isinstance(schema, list):
                for index, value in enumerate(schema):
                    check_patterns(value, "{}[{}]".format(path, index))

        for schema_path in schema_paths:
            with self.subTest(schema=str(schema_path.relative_to(root))):
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                check_patterns(schema, "$")

    def test_approve_cannot_have_annotations(self):
        payload = valid_review()
        payload["recommendation"] = "approve"
        with self.assertRaises(ReviewValidationError):
            validate_review_output(payload)

    def test_parse_requires_json_object(self):
        with self.assertRaises(ReviewValidationError):
            parse_review_json("[]")

    def test_annotation_line_side_is_required_and_validated(self):
        missing = valid_review()
        del missing["annotations"][0]["line_side"]
        with self.assertRaisesRegex(ReviewValidationError, "line_side"):
            validate_review_output(missing)

        invalid = valid_review()
        invalid["annotations"][0]["line_side"] = "BOTH"
        with self.assertRaisesRegex(ReviewValidationError, "line_side must be one of NEW, OLD"):
            validate_review_output(invalid)

    def test_annotation_location_filter_keeps_only_added_lines_in_primary_diff(self):
        review = validate_review_output(valid_review())
        invalid_annotation = dict(review.annotations[0])
        invalid_annotation["external_id"] = "finding-002"
        invalid_annotation["line"] = 10
        review.annotations.append(invalid_annotation)
        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -10,3 +10,4 @@
 unchanged
-old
+new
+added
 unchanged
"""
        review.annotations[0]["line"] = 12

        filtered = filter_annotation_locations(review, diff)

        self.assertEqual(
            [annotation["external_id"] for annotation in filtered.annotations],
            ["finding-001"],
        )
        self.assertEqual(filtered.recommendation, "request_changes")

    def test_all_invalid_annotation_locations_become_consistent_approval(self):
        review = validate_review_output(valid_review())
        review.annotations[0]["line"] = 10
        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -10,2 +10,2 @@
 unchanged
-old
+new
"""

        filtered = filter_annotation_locations(review, diff)
        report = to_bitbucket_report(filtered, "Codex PR Review", provider="codex")

        self.assertEqual(filtered.recommendation, "approve")
        self.assertEqual(filtered.annotations, [])
        self.assertEqual(report["result"], "PASSED")
        self.assertEqual(
            report["details"],
            "Codex reviewed this pull request and found no material issues.",
        )
        self.assertIn(
            {"title": "Findings", "type": "NUMBER", "value": 0},
            report["data"],
        )
        self.assertIn(
            {"title": "Recommendation", "type": "TEXT", "value": "Approve"},
            report["data"],
        )

    def test_deleted_file_old_side_location_is_retained_for_inline_mode(self):
        payload = valid_review()
        payload["annotations"][0].update(
            {"path": "removed.conf", "line": 2, "line_side": "OLD"}
        )
        review = validate_review_output(payload)
        diff = """diff --git a/removed.conf b/removed.conf
deleted file mode 100644
index 1111111..0000000
--- a/removed.conf
+++ /dev/null
@@ -1,2 +0,0 @@
-keep=true
-release_scan=true
"""

        filtered = filter_annotation_locations(
            review,
            diff,
            allowed_line_sides=("NEW", "OLD"),
        )

        self.assertEqual(len(filtered.annotations), 1)
        self.assertEqual(filtered.annotations[0]["line_side"], "OLD")
        self.assertEqual(filtered.recommendation, "request_changes")

    def test_same_line_number_is_disambiguated_by_declared_side(self):
        payload = valid_review()
        payload["annotations"][0].update({"line": 5, "line_side": "NEW"})
        old = dict(payload["annotations"][0])
        old.update({"external_id": "finding-002", "line_side": "OLD"})
        payload["annotations"].append(old)
        review = validate_review_output(payload)
        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -5 +5 @@
-old
+new
"""

        inline_review = filter_annotation_locations(
            review,
            diff,
            allowed_line_sides=("NEW", "OLD"),
        )
        report_review = filter_annotation_locations(review, diff)
        comments = to_inline_pr_comments(inline_review, provider="codex")

        self.assertEqual(
            {annotation["line_side"] for annotation in inline_review.annotations},
            {"NEW", "OLD"},
        )
        self.assertEqual(
            [annotation["line_side"] for annotation in report_review.annotations],
            ["NEW"],
        )
        self.assertEqual(
            {(comment["line"], comment["line_side"]) for comment in comments},
            {(5, "NEW"), (5, "OLD")},
        )
        self.assertEqual(report_review.recommendation, "request_changes")

    def test_unchanged_context_is_invalid_on_both_sides(self):
        payload = valid_review()
        payload["annotations"][0].update({"line": 5, "line_side": "NEW"})
        old = dict(payload["annotations"][0])
        old.update({"external_id": "finding-002", "line_side": "OLD"})
        payload["annotations"].append(old)
        review = validate_review_output(payload)
        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -5,2 +5,2 @@
 unchanged
-old
+new
"""

        filtered = filter_annotation_locations(
            review,
            diff,
            allowed_line_sides=("NEW", "OLD"),
        )

        self.assertEqual(filtered.annotations, [])
        self.assertEqual(filtered.recommendation, "approve")

    def test_noncanonical_nonempty_diff_is_rejected_instead_of_approving(self):
        review = validate_review_output(valid_review())
        diffs = (
            "external diff helper output\n",
            "diff --git a/src/app.py b/src/app.py\nexternal helper output\n",
            """diff --git old/src/app.py new/src/app.py
--- old/src/app.py
+++ new/src/app.py
@@ -11 +12 @@
-old
+new
""",
            """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
content without a hunk header
""",
        )

        for diff in diffs:
            with self.subTest(diff=diff):
                with self.assertRaisesRegex(ReviewValidationError, "not canonical"):
                    filter_annotation_locations(review, diff)

    def test_canonical_short_submodule_diff_preserves_unrelated_valid_finding(self):
        review = validate_review_output(valid_review())
        diff = """diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -11 +12 @@
-old
+new
diff --git a/vendor/library b/vendor/library
index 3333333..4444444 160000
--- a/vendor/library
+++ b/vendor/library
@@ -1 +1 @@
-Subproject commit 3333333333333333333333333333333333333333
+Subproject commit 4444444444444444444444444444444444444444
"""

        filtered = filter_annotation_locations(review, diff)

        self.assertEqual(
            [annotation["external_id"] for annotation in filtered.annotations],
            ["finding-001"],
        )
        self.assertEqual(filtered.recommendation, "request_changes")

    def test_changed_lines_track_side_across_canonical_git_diff_forms(self):
        cases = {
            "rename": (
                """diff --git a/old.py b/new.py
similarity index 80%
rename from old.py
rename to new.py
index 1111111..2222222 100644
--- a/old.py
+++ b/new.py
@@ -2,2 +2,2 @@
 context
-old name
+new name
""",
                {"OLD": {("old.py", 3)}, "NEW": {("new.py", 3)}},
            ),
            "copy": (
                """diff --git a/source.py b/copied.py
similarity index 75%
copy from source.py
copy to copied.py
index 1111111..2222222 100644
--- a/source.py
+++ b/copied.py
@@ -1 +1 @@
-old copy
+new copy
""",
                {"OLD": {("source.py", 1)}, "NEW": {("copied.py", 1)}},
            ),
            "multiple hunks": (
                """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -2,2 +2,3 @@
 context
-old first
+new first
+new extra
@@ -10,2 +11,2 @@
-old second
+new second
 context
""",
                {
                    "OLD": {("app.py", 3), ("app.py", 10)},
                    "NEW": {("app.py", 3), ("app.py", 4), ("app.py", 11)},
                },
            ),
            "old quoted path": (
                r'''diff --git "a/docs/\303\246\told.py" "b/docs/\303\246\told.py"
--- "a/docs/\303\246\told.py"
+++ "b/docs/\303\246\told.py"
@@ -7 +8 @@
-old
+new
''',
                {
                    "OLD": {("docs/æ\told.py", 7)},
                    "NEW": {("docs/æ\told.py", 8)},
                },
            ),
            "no newline marker": (
                """diff --git a/value.txt b/value.txt
--- a/value.txt
+++ b/value.txt
@@ -1 +1 @@
-old
\\ No newline at end of file
+new
\\ No newline at end of file
""",
                {"OLD": {("value.txt", 1)}, "NEW": {("value.txt", 1)}},
            ),
            "binary and text": (
                """diff --git a/image.png b/image.png
index 1111111..2222222 100644
Binary files a/image.png and b/image.png differ
diff --git a/app.py b/app.py
index 3333333..4444444 100644
--- a/app.py
+++ b/app.py
@@ -4 +4 @@
-old
+new
""",
                {"OLD": {("app.py", 4)}, "NEW": {("app.py", 4)}},
            ),
        }

        for name, (diff, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(_changed_lines_by_side(diff), expected)

    def test_annotation_location_filter_discards_related_repository_path(self):
        review = validate_review_output(valid_review())
        review.annotations[0]["path"] = "contracts/schema.json"
        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -11 +12 @@
-old
+new
"""
        filtered = filter_annotation_locations(review, diff)

        self.assertEqual(filtered.annotations, [])
        self.assertEqual(filtered.recommendation, "approve")

    def test_annotation_location_decodes_git_c_quoted_path(self):
        review = validate_review_output(valid_review())
        review.annotations[0]["path"] = "docs/æ\tline\nbreak.py"
        review.annotations[0]["line"] = 7
        diff = r'''diff --git "a/docs/\303\246\tline\nbreak.py" "b/docs/\303\246\tline\nbreak.py"
--- "a/docs/\303\246\tline\nbreak.py"
+++ "b/docs/\303\246\tline\nbreak.py"
@@ -6 +7 @@
-old
+new
'''

        filtered = filter_annotation_locations(review, diff)

        self.assertEqual(
            [annotation["external_id"] for annotation in filtered.annotations],
            ["finding-001"],
        )
        self.assertEqual(filtered.recommendation, "request_changes")

    def test_added_content_beginning_with_double_plus_is_not_a_file_header(self):
        review = validate_review_output(valid_review())
        review.annotations[0]["line"] = 2
        diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 unchanged
+++ b/not-a-file-header
"""

        filtered = filter_annotation_locations(review, diff)

        self.assertEqual(
            [annotation["external_id"] for annotation in filtered.annotations],
            ["finding-001"],
        )
        self.assertEqual(filtered.recommendation, "request_changes")


if __name__ == "__main__":
    unittest.main()
