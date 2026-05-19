import unittest

from scout.prompt import build_claude_prompt, build_codex_prompt, build_provider_prompt
from scout.review_plan import ReviewPlan


def context():
    return {
        "workspace": "ws",
        "repo_slug": "repo",
        "pr_id": "12",
        "title": "Title",
        "description": "Description",
        "source_branch": "feature",
        "source_commit": "abc123",
        "target_branch": "main",
        "target_commit": "def456",
        "merge_base": "fedcba",
        "changed_lines": "240",
        "context_path": "/tmp/context.json",
        "files_path": "/tmp/files.txt",
        "diff_path": "/tmp/diff.patch",
    }


class PromptTests(unittest.TestCase):
    def test_prompt_requires_inherited_subagents_and_single_final_json(self):
        prompt = build_codex_prompt(
            context(),
            "/tmp/schema.json",
            ReviewPlan(changed_lines=240, high_risk=False, subagents_per_lens=2),
        )
        self.assertIn("Changed LOC: 240", prompt)
        self.assertIn("Subagents per review category: 2", prompt)
        self.assertIn("Total reviewer subagents: 10", prompt)
        self.assertIn("correctness-1", prompt)
        self.assertIn("correctness-2", prompt)
        self.assertIn("best-practices-2", prompt)
        self.assertIn("keep the current Codex model and reasoning effort", prompt)
        self.assertIn("Do not override agent type, model, or reasoning effort", prompt)
        self.assertIn("all actionable findings it", prompt)
        self.assertIn("can support, not only the first", prompt)
        self.assertIn("until every listed subagent has completed", prompt)
        self.assertIn("Return exactly one schema-shaped JSON object", prompt)
        self.assertIn("Do not emit progress, status, or placeholder JSON", prompt)

    def test_claude_prompt_uses_claude_specific_subagent_wording(self):
        prompt = build_claude_prompt(
            context(),
            "/tmp/schema.json",
            ReviewPlan(changed_lines=240, high_risk=False, subagents_per_lens=2),
        )
        self.assertIn("You are Claude reviewing", prompt)
        self.assertIn("keep the current Claude model and effort configuration", prompt)
        self.assertIn("Do not override agent type, model, or effort", prompt)
        self.assertNotIn("Codex model and reasoning effort", prompt)

    def test_provider_prompt_selects_requested_provider(self):
        prompt = build_provider_prompt(
            "claude",
            context(),
            "/tmp/schema.json",
            ReviewPlan(changed_lines=240, high_risk=False, subagents_per_lens=2),
        )
        self.assertIn("Claude", prompt)


if __name__ == "__main__":
    unittest.main()
