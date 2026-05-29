import unittest

from scout.schema import (
    ReviewValidationError,
    summarize_findings,
    parse_review_json,
    report_result_for_recommendation,
    to_bitbucket_annotations,
    to_critical_pr_comment,
    to_pr_comment,
    to_bitbucket_report,
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
            annotation["reviewer"] = ["correctness", "security", "tests", "performance", "best-practices"][index % 5]
            annotation["severity"] = ["CRITICAL", "HIGH", "MEDIUM", "LOW"][index % 4]
            payload["annotations"].append(annotation)
        review = validate_review_output(payload)
        report = to_bitbucket_report(review, "Codex PR Review", provider="codex")
        self.assertLessEqual(len(report["details"]), 2000)
        self.assertIn("- Correctness: 12 issues", report["details"])
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

    def test_approve_cannot_have_annotations(self):
        payload = valid_review()
        payload["recommendation"] = "approve"
        with self.assertRaises(ReviewValidationError):
            validate_review_output(payload)

    def test_parse_requires_json_object(self):
        with self.assertRaises(ReviewValidationError):
            parse_review_json("[]")


if __name__ == "__main__":
    unittest.main()
