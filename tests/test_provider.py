import os
import unittest
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, call, patch

from scout.claude import ClaudeRunner
from scout.codex import CodexRunner
from scout.config import CredentialStore
from scout.provider import (
    DEFAULT_PROVIDER_COOLDOWN_SECONDS,
    ProviderError,
    provider_quota_cooldown_seconds,
    run_provider_command,
    terminate_process_group,
)
from test_claude import claude_config
from test_codex import codex_config


class RunProviderCommandTests(unittest.TestCase):
    def run_command(self, directory, cmd, **overrides):
        prompt_file = Path(directory) / "prompt.txt"
        prompt_file.write_text("prompt", encoding="utf-8")
        args = dict(
            provider="claude", label="Claude test", cmd=cmd, prompt_file=prompt_file,
            stdout_file=Path(directory) / "stdout.log", stderr_file=Path(directory) / "stderr.log",
            env={}, timeout_seconds=5, is_superseded=lambda: False, superseded_message="superseded",
            cwd=directory,
        )
        args.update(overrides)
        return run_provider_command(**args)

    def test_missing_command_raises_provider_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ProviderError, "Claude test could not start"):
                self.run_command(tmp, [str(Path(tmp) / "missing")])

    def test_runners_report_missing_command_as_provider_error(self):
        for config_factory, runner_type in ((claude_config, ClaudeRunner), (codex_config, CodexRunner)):
            with self.subTest(runner=runner_type.__name__), tempfile.TemporaryDirectory() as tmp:
                schema = Path(tmp) / "schema.json"
                schema.write_text("{}", encoding="utf-8")
                runner = runner_type(config_factory(command=str(Path(tmp) / "missing")), CredentialStore("/tmp/unused"))
                with self.assertRaisesRegex(ProviderError, "could not start"):
                    runner.run(tmp, "review", str(schema), str(Path(tmp) / "run"), is_superseded=lambda: False)

    def test_failing_supersede_check_still_reaps_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "pid"
            script = "import os, pathlib, time; pathlib.Path({!r}).write_text(str(os.getpid())); time.sleep(60)"

            def is_superseded():
                if pid_file.exists():
                    raise RuntimeError("state unavailable")
                return False

            with patch("scout.provider.POLL_INTERVAL_SECONDS", 0.05):
                with self.assertRaisesRegex(RuntimeError, "state unavailable"):
                    self.run_command(tmp, [sys.executable, "-c", script.format(str(pid_file))], is_superseded=is_superseded)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 0)

    def test_returns_as_soon_as_process_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            started = time.monotonic()
            output = self.run_command(tmp, [sys.executable, "-c", "print('done')"])
            self.assertLess(time.monotonic() - started, 0.9)
            self.assertEqual(output.returncode, 0)
            self.assertEqual(output.final_message, "done\n")



class ProviderQuotaDetectionTests(unittest.TestCase):
    def test_killed_process_is_reaped_after_ignoring_termination(self):
        proc = Mock(pid=123)
        proc.wait.side_effect = [subprocess.TimeoutExpired("provider", 10), None]
        with patch("scout.provider.os.killpg") as killpg:
            terminate_process_group(proc)
        self.assertEqual(killpg.call_args_list, [call(123, signal.SIGTERM), call(123, signal.SIGKILL)])
        self.assertEqual(proc.wait.call_args_list, [call(timeout=10), call()])

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
