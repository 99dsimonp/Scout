import unittest

from scout.review_plan import (
    build_review_plan,
    count_changed_lines,
    effective_subagent_max_per_lens,
)


class ReviewPlanTests(unittest.TestCase):
    def test_count_changed_lines_counts_header_like_hunk_content(self):
        diff = """diff --git a/a.sql b/a.sql
--- a/a.sql
+++ b/a.sql
@@ -1 +1 @@
--- removed comment
+++ added content
diff --git a/b.sql b/b.sql
--- a/b.sql
+++ b/b.sql
@@ -1 +1 @@
-old
+new
"""
        self.assertEqual(count_changed_lines(diff), 4)

    def test_count_changed_lines_does_not_split_content_at_other_separators(self):
        for separator in ("\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                diff = (
                    "diff --git a/a.py b/a.py\n"
                    "--- a/a.py\n+++ b/a.py\n"
                    "@@ -1 +1 @@\n-old{0}-content\n+new{0}+content\n"
                ).format(separator)
                self.assertEqual(count_changed_lines(diff), 2)

    def test_count_changed_lines_ignores_diff_headers(self):
        diff = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
-old
+new
+extra
 context
"""
        self.assertEqual(count_changed_lines(diff), 3)

    def test_scales_by_changed_loc_only(self):
        cases = [
            (150, 1),
            (151, 2),
            (600, 2),
            (601, 3),
            (1500, 3),
            (1501, 4),
        ]
        for changed_lines, expected in cases:
            with self.subTest(changed_lines=changed_lines):
                plan = build_review_plan(
                    changed_lines=changed_lines,
                    description="Risk: high",
                    small_loc_limit=150,
                    medium_loc_limit=600,
                    large_loc_limit=1500,
                    high_risk_bonus=1,
                    max_subagents_per_lens=4,
                    risk="medium",
                )
                self.assertEqual(plan.subagents_per_lens, expected)
                self.assertEqual(plan.total_subagents, expected * 6)
                self.assertEqual(plan.risk, "medium")

    def test_low_risk_uses_one_reviewer_per_lens_regardless_loc(self):
        plan = build_review_plan(
            changed_lines=5000,
            description="",
            small_loc_limit=150,
            medium_loc_limit=600,
            large_loc_limit=1500,
            high_risk_bonus=1,
            max_subagents_per_lens=4,
            risk="low",
        )
        self.assertEqual(plan.subagents_per_lens, 1)
        self.assertEqual(plan.total_subagents, 6)
        self.assertFalse(plan.high_risk)

    def test_high_risk_adds_one_per_lens_with_cap(self):
        plan = build_review_plan(
            changed_lines=601,
            description="",
            small_loc_limit=150,
            medium_loc_limit=600,
            large_loc_limit=1500,
            high_risk_bonus=1,
            max_subagents_per_lens=4,
            risk="high",
        )
        self.assertEqual(plan.subagents_per_lens, 4)
        self.assertEqual(plan.total_subagents, 24)
        self.assertTrue(plan.high_risk)
        self.assertEqual(plan.risk, "high")

    def test_unknown_risk_defaults_to_medium(self):
        plan = build_review_plan(
            changed_lines=151,
            description="",
            small_loc_limit=150,
            medium_loc_limit=600,
            large_loc_limit=1500,
            high_risk_bonus=1,
            max_subagents_per_lens=4,
            risk="urgent",
        )
        self.assertEqual(plan.subagents_per_lens, 2)
        self.assertEqual(plan.risk, "medium")

    def test_reviewers_are_repeated_per_lens(self):
        plan = build_review_plan(
            changed_lines=151,
            description="Risk: low",
            small_loc_limit=150,
            medium_loc_limit=600,
            large_loc_limit=1500,
            high_risk_bonus=1,
            max_subagents_per_lens=4,
            risk="medium",
        )
        self.assertIn("correctness-1", plan.reviewers)
        self.assertIn("correctness-2", plan.reviewers)
        self.assertIn("security-2", plan.reviewers)
        self.assertIn("compatibility-1", plan.reviewers)
        self.assertIn("compatibility-2", plan.reviewers)
        self.assertEqual(len(plan.reviewers), 12)

    def test_effective_lens_cap_preserves_total_provider_limit(self):
        self.assertEqual(effective_subagent_max_per_lens(3, 15), 2)
        self.assertEqual(effective_subagent_max_per_lens(3, 18), 3)


if __name__ == "__main__":
    unittest.main()
