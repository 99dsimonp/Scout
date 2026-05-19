import json
import tempfile
import unittest
from pathlib import Path

from scout.usage import parse_claude_usage, parse_codex_usage, summarize_usage_log


class UsageTests(unittest.TestCase):
    def test_parse_claude_usage_prefers_model_usage_for_total_pr_cost(self):
        usage = parse_claude_usage(
            json.dumps(
                {
                    "type": "result",
                    "total_cost_usd": 1.25,
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 20,
                        "cache_creation_input_tokens": 30,
                        "cache_read_input_tokens": 40,
                    },
                    "modelUsage": {
                        "claude-sonnet-4-6": {
                            "inputTokens": 100,
                            "outputTokens": 200,
                            "cacheCreationInputTokens": 300,
                            "cacheReadInputTokens": 400,
                            "costUSD": 1.5,
                        },
                        "claude-haiku-4-5": {
                            "inputTokens": 1,
                            "outputTokens": 2,
                            "cacheCreationInputTokens": 3,
                            "cacheReadInputTokens": 4,
                            "costUSD": 0.05,
                        },
                    },
                }
            )
        )

        self.assertEqual(usage["input_tokens"], 101)
        self.assertEqual(usage["output_tokens"], 202)
        self.assertEqual(usage["cache_creation_input_tokens"], 303)
        self.assertEqual(usage["cache_read_input_tokens"], 404)
        self.assertEqual(usage["total_tokens"], 1010)
        self.assertAlmostEqual(usage["cost_usd"], 1.55)
        self.assertEqual(usage["source"], "claude_stdout_json")

    def test_parse_codex_usage_reads_last_tokens_used_block(self):
        usage = parse_codex_usage("", "tokens used\n1,234\n...\ntokens used\n56,789\n")

        self.assertEqual(usage["total_tokens"], 56789)
        self.assertEqual(usage["source"], "codex_tokens_used")

    def test_summarize_usage_log_groups_by_pr_and_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "provider-usage.jsonl"
            path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-05-18T10:00:00+00:00",
                                "workspace": "ws",
                                "repo": "repo-a",
                                "pr": 1,
                                "provider": "codex",
                                "commit": "a",
                                "usage": {"total_tokens": 100},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-05-18T11:00:00+00:00",
                                "workspace": "ws",
                                "repo": "repo-a",
                                "pr": 1,
                                "provider": "codex",
                                "commit": "b",
                                "usage": {"total_tokens": 300},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-05-18T09:00:00+00:00",
                                "workspace": "ws",
                                "repo": "repo-a",
                                "pr": 2,
                                "provider": "claude",
                                "commit": "c",
                                "usage": {"total_tokens": 50, "cost_usd": 0.1},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            rows = summarize_usage_log(path)

            self.assertEqual(rows[0]["pr"], 1)
            self.assertEqual(rows[0]["provider"], "codex")
            self.assertEqual(rows[0]["runs"], 2)
            self.assertEqual(rows[0]["total_tokens"], 400)
            self.assertEqual(rows[0]["latest_commit"], "b")
            self.assertEqual(rows[1]["pr"], 2)


if __name__ == "__main__":
    unittest.main()
