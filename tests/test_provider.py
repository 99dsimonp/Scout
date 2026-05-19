import unittest
from datetime import datetime, timezone

from scout.provider import (
    DEFAULT_PROVIDER_COOLDOWN_SECONDS,
    provider_quota_cooldown_seconds,
)


class ProviderQuotaDetectionTests(unittest.TestCase):
    def test_detects_claude_usage_limit_lockout(self):
        self.assertEqual(
            provider_quota_cooldown_seconds(
                "claude",
                "Claude usage limit reached. Your limit will reset at 7 PM.",
            ),
            DEFAULT_PROVIDER_COOLDOWN_SECONDS,
        )

    def test_detects_codex_usage_limit_lockout(self):
        self.assertEqual(
            provider_quota_cooldown_seconds(
                "codex",
                "You've reached your usage limit. Try again after your 5-hour window resets.",
            ),
            DEFAULT_PROVIDER_COOLDOWN_SECONDS,
        )

    def test_does_not_treat_thread_limits_as_quota_lockout(self):
        self.assertIsNone(
            provider_quota_cooldown_seconds("codex", "agent thread limit reached")
        )

    def test_detects_five_hour_limit_without_usage_wording(self):
        self.assertEqual(
            provider_quota_cooldown_seconds(
                "claude",
                "You have reached the 5 hour limit. It resets later.",
            ),
            DEFAULT_PROVIDER_COOLDOWN_SECONDS,
        )

    def test_detects_codex_usage_limit_reset_time_from_message(self):
        now = datetime(2024, 5, 18, 9, 0, 0, tzinfo=timezone.utc)
        cooldown_seconds = provider_quota_cooldown_seconds(
            "codex",
            "You've hit your usage limit. Try again at 1:09 PM.",
            now=now,
        )
        reset = datetime(2024, 5, 18, 13, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(cooldown_seconds, int((reset - now).total_seconds()))

    def test_rolls_explicit_reset_time_to_next_day(self):
        now = datetime(2024, 5, 18, 14, 0, 0, tzinfo=timezone.utc)
        cooldown_seconds = provider_quota_cooldown_seconds(
            "codex",
            "You've hit your usage limit. Try again at 1:09 PM.",
            now=now,
        )
        reset = datetime(2024, 5, 19, 13, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(cooldown_seconds, int((reset - now).total_seconds()))

    def test_detects_compact_codex_reset_time(self):
        now = datetime(2024, 5, 18, 9, 0, 0, tzinfo=timezone.utc)
        cooldown_seconds = provider_quota_cooldown_seconds(
            "codex",
            "You've hit your usage limit. Try again at 1:09PM.",
            now=now,
        )
        reset = datetime(2024, 5, 18, 13, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(cooldown_seconds, int((reset - now).total_seconds()))

    def test_handles_naive_now_for_explicit_reset_time(self):
        now = datetime(2024, 5, 18, 9, 0, 0)
        cooldown_seconds = provider_quota_cooldown_seconds(
            "codex",
            "You've hit your usage limit. Try again at 1 PM.",
            now=now,
        )
        reset = datetime(2024, 5, 18, 13, 0, 0)
        self.assertEqual(cooldown_seconds, int((reset - now).total_seconds()))


if __name__ == "__main__":
    unittest.main()
