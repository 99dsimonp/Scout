import unittest

from scout.review_plan import (
    build_review_plan,
    count_changed_lines,
    description_has_high_risk,
)


class ReviewPlanTests(unittest.TestCase):
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
                    description="Risk: medium",
                    small_loc_limit=150,
                    medium_loc_limit=600,
                    large_loc_limit=1500,
                    high_risk_bonus=1,
                    max_subagents_per_lens=4,
                )
                self.assertEqual(plan.subagents_per_lens, expected)
                self.assertEqual(plan.total_subagents, expected * 5)

    def test_high_risk_description_adds_one_per_lens_with_cap(self):
        self.assertTrue(description_has_high_risk("Risk: high\n\nDetails"))
        self.assertTrue(description_has_high_risk("- **Risk**: **high**\n\nDetails"))
        plan = build_review_plan(
            changed_lines=601,
            description="Risk: high",
            small_loc_limit=150,
            medium_loc_limit=600,
            large_loc_limit=1500,
            high_risk_bonus=1,
            max_subagents_per_lens=4,
        )
        self.assertEqual(plan.subagents_per_lens, 4)
        self.assertEqual(plan.total_subagents, 20)

    def test_reviewers_are_repeated_per_lens(self):
        plan = build_review_plan(
            changed_lines=151,
            description="Risk: low",
            small_loc_limit=150,
            medium_loc_limit=600,
            large_loc_limit=1500,
            high_risk_bonus=1,
            max_subagents_per_lens=4,
        )
        self.assertIn("correctness-1", plan.reviewers)
        self.assertIn("correctness-2", plan.reviewers)
        self.assertIn("security-2", plan.reviewers)
        self.assertEqual(len(plan.reviewers), 10)


if __name__ == "__main__":
    unittest.main()
