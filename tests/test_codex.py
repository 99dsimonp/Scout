import tempfile
import unittest
from pathlib import Path

from scout.codex import CodexRunner, ProviderError, _build_diagnostics
from scout.config import CodexConfig, CredentialStore
from scout.provider import DEFAULT_PROVIDER_COOLDOWN_SECONDS


def codex_config(**overrides):
    values = {
        "enabled": True,
        "auth_mode": "logged_in",
        "credential": "codex",
        "home_dir": "/tmp/codex-home",
        "max_parallel": 1,
        "timeout_seconds": 1200,
        "command": "codex",
        "model": "gpt-5.5",
        "reasoning_effort": "xhigh",
        "fast_mode": True,
        "max_subagents": 20,
        "subagent_max_per_lens": 4,
    }
    values.update(overrides)
    return CodexConfig(**values)


def valid_review_json():
    return """{"recommendation":"request_changes","report":{"title":"Codex PR Review","details":"Found one issue.","report_type":"BUG","reporter":"scout","data":[{"title":"Findings","type":"NUMBER","value":1}]},"annotations":[{"external_id":"finding-001","annotation_type":"BUG","path":"src/app.py","line":12,"summary":"Missing error handling","details":"The changed call can fail.","severity":"HIGH","result":"FAILED","reviewer":"correctness","confidence":"HIGH","smallest_fix":"Handle the failure before updating state."}]}"""


class CodexRunnerTests(unittest.TestCase):
    def test_build_command_includes_model_reasoning_and_fast_mode(self):
        config = codex_config()
        runner = CodexRunner(config, CredentialStore("/tmp/unused"))
        cmd = runner.build_command("/repo", "/schema.json", "/out.json", "secret prompt")
        self.assertEqual(cmd[:2], ["codex", "exec"])
        self.assertIn("--enable", cmd)
        self.assertIn("fast_mode", cmd)
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "gpt-5.5")
        self.assertIn("--config", cmd)
        self.assertIn('model_reasoning_effort="xhigh"', cmd)
        self.assertNotIn("secret prompt", cmd)

    def test_build_command_can_disable_fast_mode(self):
        config = codex_config(model="gpt-5.4", reasoning_effort="high", fast_mode=False)
        runner = CodexRunner(config, CredentialStore("/tmp/unused"))
        cmd = runner.build_command("/repo", "/schema.json", "/out.json", "prompt")
        self.assertIn("--disable", cmd)
        self.assertIn("fast_mode", cmd)

    def test_build_risk_command_uses_classifier_model_and_reasoning(self):
        config = codex_config(model="gpt-5.5", reasoning_effort="xhigh")
        runner = CodexRunner(config, CredentialStore("/tmp/unused"))
        cmd = runner.build_risk_command(
            "/repo",
            "/risk.schema.json",
            "/risk-output.json",
            model="gpt-5.4",
            reasoning_effort="low",
        )
        self.assertEqual(cmd[cmd.index("--model") + 1], "gpt-5.4")
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertIn('model_reasoning_effort="low"', cmd)
        self.assertIn("/risk.schema.json", cmd)
        self.assertIn("/risk-output.json", cmd)

    def test_build_comment_request_command_uses_classifier_model_and_reasoning(self):
        config = codex_config(model="gpt-5.5", reasoning_effort="xhigh")
        runner = CodexRunner(config, CredentialStore("/tmp/unused"))
        cmd = runner.build_comment_request_command(
            "/repo",
            "/comment-request.schema.json",
            "/comment-request-output.json",
            model="gpt-5.4",
            reasoning_effort="low",
        )
        self.assertEqual(cmd[cmd.index("--model") + 1], "gpt-5.4")
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertIn('model_reasoning_effort="low"', cmd)
        self.assertIn("/comment-request.schema.json", cmd)
        self.assertIn("/comment-request-output.json", cmd)

    def test_assess_risk_passes_description_on_stdin_and_parses_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "fake-codex"
            run_dir = Path(tmp) / "risk"
            stdin_capture = Path(tmp) / "stdin.txt"
            argv_capture = Path(tmp) / "argv.txt"
            command.write_text(
                """#!/bin/sh
output=""
printf '%s\n' "$@" > '"""
                + str(argv_capture)
                + """'
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output-last-message" ]; then
    shift
    output="$1"
  fi
  shift
done
cat > '"""
                + str(stdin_capture)
                + """'
cat > "$output" <<'JSON'
{"risk":"high"}
JSON
""",
                encoding="utf-8",
            )
            command.chmod(0o755)
            runner = CodexRunner(
                codex_config(command=str(command), timeout_seconds=5),
                CredentialStore("/tmp/unused"),
            )

            risk = runner.assess_risk(
                description="Sensitive auth change",
                model="gpt-5.4",
                reasoning_effort="low",
                timeout_seconds=5,
                run_dir=str(run_dir),
                is_superseded=lambda: False,
            )

            self.assertEqual(risk, "high")
            self.assertIn("Sensitive auth change", stdin_capture.read_text(encoding="utf-8"))
            self.assertNotIn("Sensitive auth change", argv_capture.read_text(encoding="utf-8"))

    def test_classify_review_request_passes_comment_on_stdin_and_parses_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "fake-codex"
            run_dir = Path(tmp) / "comment-request"
            stdin_capture = Path(tmp) / "stdin.txt"
            argv_capture = Path(tmp) / "argv.txt"
            command.write_text(
                """#!/bin/sh
output=""
printf '%s\n' "$@" > '"""
                + str(argv_capture)
                + """'
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--output-last-message" ]; then
    shift
    output="$1"
  fi
  shift
done
cat > '"""
                + str(stdin_capture)
                + """'
cat > "$output" <<'JSON'
{"review_requested":true,"reason":"explicit request"}
JSON
""",
                encoding="utf-8",
            )
            command.chmod(0o755)
            runner = CodexRunner(
                codex_config(command=str(command), timeout_seconds=5),
                CredentialStore("/tmp/unused"),
            )

            result = runner.classify_review_request(
                comment="@scout review this PR",
                model="gpt-5.4",
                reasoning_effort="low",
                timeout_seconds=5,
                run_dir=str(run_dir),
                is_superseded=lambda: False,
            )

            self.assertTrue(result.review_requested)
            self.assertEqual(result.reason, "explicit request")
            self.assertIn("@scout review this PR", stdin_capture.read_text(encoding="utf-8"))
            self.assertNotIn("@scout review this PR", argv_capture.read_text(encoding="utf-8"))

    def test_run_passes_prompt_on_stdin_without_argv_exposure(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "fake-codex"
            run_dir = Path(tmp) / "run"
            stdin_capture = Path(tmp) / "stdin.txt"
            argv_capture = Path(tmp) / "argv.txt"
            command.write_text(
                """#!/bin/sh
cat > '"""
                + str(stdin_capture)
                + """'
printf '%s\n' "$@" > '"""
                + str(argv_capture)
                + """'
cat > '"""
                + str(run_dir / "codex-final-message.json")
                + """' <<'JSON'
"""
                + valid_review_json()
                + """
JSON
echo 'tokens used' >&2
echo '12,345' >&2
""",
                encoding="utf-8",
            )
            command.chmod(0o755)
            runner = CodexRunner(
                codex_config(command=str(command), timeout_seconds=5),
                CredentialStore("/tmp/unused"),
            )

            result = runner.run(
                worktree=tmp,
                prompt="secret prompt",
                schema_path="/schema.json",
                run_dir=str(run_dir),
                is_superseded=lambda: False,
            )

            self.assertEqual(result.final_message.strip(), valid_review_json())
            self.assertEqual(result.usage["total_tokens"], 12345)
            self.assertEqual(stdin_capture.read_text(encoding="utf-8"), "secret prompt")
            self.assertNotIn("secret prompt", argv_capture.read_text(encoding="utf-8"))

    def test_run_rejects_stream_disconnect_even_with_schema_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "fake-codex"
            command.write_text(
                """#!/bin/sh
cat >&2 <<'LOG'
collab: SpawnAgent
ERROR: Reconnecting... 1/10
codex
"""
                + valid_review_json()
                + """
ERROR: stream disconnected before completion
LOG
exit 1
""",
                encoding="utf-8",
            )
            command.chmod(0o755)
            runner = CodexRunner(
                codex_config(command=str(command), timeout_seconds=5),
                CredentialStore("/tmp/unused"),
            )

            with self.assertRaises(ProviderError) as raised:
                runner.run(
                    worktree=tmp,
                    prompt="prompt",
                    schema_path="/schema.json",
                    run_dir=str(Path(tmp) / "run"),
                    is_superseded=lambda: False,
                )

            self.assertIn("Codex exited with status 1", str(raised.exception))
            self.assertIn("spawned_subagents=1", str(raised.exception))
            self.assertIn("reconnects=1", str(raised.exception))
            self.assertIn("stream_disconnected=True", str(raised.exception))
            self.assertIn("final_message_present=False", str(raised.exception))

    def test_run_error_includes_diagnostics_when_final_message_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "fake-codex"
            command.write_text(
                """#!/bin/sh
echo 'collab: SpawnAgent' >&2
echo 'agent thread limit reached' >&2
exit 1
""",
                encoding="utf-8",
            )
            command.chmod(0o755)
            runner = CodexRunner(
                codex_config(command=str(command), timeout_seconds=5),
                CredentialStore("/tmp/unused"),
            )

            with self.assertRaises(ProviderError) as raised:
                runner.run(
                    worktree=tmp,
                    prompt="prompt",
                    schema_path="/schema.json",
                    run_dir=str(Path(tmp) / "run"),
                    is_superseded=lambda: False,
                )

            self.assertIn("spawned_subagents=1", str(raised.exception))
            self.assertIn("thread_limit_errors=1", str(raised.exception))
            self.assertIn("final_message_present=False", str(raised.exception))

    def test_run_marks_usage_limit_errors_for_provider_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "fake-codex"
            command.write_text(
                """#!/bin/sh
echo "You've reached your usage limit. Try again after your 5-hour window resets." >&2
exit 1
""",
                encoding="utf-8",
            )
            command.chmod(0o755)
            runner = CodexRunner(
                codex_config(command=str(command), timeout_seconds=5),
                CredentialStore("/tmp/unused"),
            )

            with self.assertRaises(ProviderError) as raised:
                runner.run(
                    worktree=tmp,
                    prompt="prompt",
                    schema_path="/schema.json",
                    run_dir=str(Path(tmp) / "run"),
                    is_superseded=lambda: False,
                )

            self.assertEqual(raised.exception.cooldown_seconds, DEFAULT_PROVIDER_COOLDOWN_SECONDS)
            self.assertEqual(raised.exception.provider_status, "quota_exhausted")

    def test_diagnostics_detect_stream_instability(self):
        diagnostics = _build_diagnostics(
            "",
            "collab: SpawnAgent\nERROR: Reconnecting... 1/10\nERROR: stream disconnected\n",
            "",
        )
        self.assertEqual(diagnostics.spawned_subagents, 1)
        self.assertEqual(diagnostics.reconnects, 1)
        self.assertTrue(diagnostics.stream_disconnected)


if __name__ == "__main__":
    unittest.main()
