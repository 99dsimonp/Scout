import json
import tempfile
import unittest
from pathlib import Path

from scout.claude import CLAUDE_DENIED_TOOLS, CLAUDE_READONLY_TOOLS, ClaudeRunner
from scout.config import ClaudeConfig, CredentialStore
from scout.provider import DEFAULT_PROVIDER_COOLDOWN_SECONDS, ProviderError


def claude_config(**overrides):
    values = {
        "enabled": True,
        "auth_mode": "logged_in",
        "credential": "claude",
        "home_dir": "/tmp/claude-home",
        "max_parallel": 1,
        "timeout_seconds": 1800,
        "command": "claude",
        "model": "claude-sonnet-4-6",
        "effort": "max",
        "max_subagents": 20,
        "subagent_max_per_lens": 1,
    }
    values.update(overrides)
    return ClaudeConfig(**values)


def valid_review_json():
    return """{"recommendation":"approve","report":{"title":"Claude PR Review","details":"No issues.","report_type":"BUG","reporter":"scout","data":[{"title":"Findings","type":"NUMBER","value":0}]},"annotations":[]}"""


class ClaudeRunnerTests(unittest.TestCase):
    def test_build_command_includes_schema_content_and_model_when_configured(self):
        runner = ClaudeRunner(
            claude_config(),
            CredentialStore("/tmp/unused"),
        )
        cmd = runner.build_command("secret prompt", '{"type":"object"}')
        self.assertEqual(cmd[:5], ["claude", "-p", "--output-format", "json", "--json-schema"])
        self.assertEqual(cmd[5], '{"type":"object"}')
        self.assertNotIn("secret prompt", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], CLAUDE_READONLY_TOOLS)
        self.assertEqual(cmd[cmd.index("--allowedTools") + 1], CLAUDE_READONLY_TOOLS)
        self.assertEqual(cmd[cmd.index("--disallowedTools") + 1], CLAUDE_DENIED_TOOLS)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertNotIn("--bare", cmd)
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-sonnet-4-6")
        self.assertIn("--effort", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "max")

    def test_build_command_uses_bare_mode_for_api_auth_only(self):
        api_runner = ClaudeRunner(claude_config(auth_mode="api"), CredentialStore("/tmp/unused"))
        self.assertIn("--bare", api_runner.build_command("prompt", '{"type":"object"}'))

        logged_in_runner = ClaudeRunner(claude_config(auth_mode="logged_in"), CredentialStore("/tmp/unused"))
        self.assertNotIn("--bare", logged_in_runner.build_command("prompt", '{"type":"object"}'))

    def test_build_command_omits_model_when_empty(self):
        runner = ClaudeRunner(claude_config(model=""), CredentialStore("/tmp/unused"))
        cmd = runner.build_command("prompt", '{"type":"object"}')
        self.assertNotIn("--model", cmd)

    def test_build_command_omits_effort_when_empty(self):
        runner = ClaudeRunner(claude_config(effort=""), CredentialStore("/tmp/unused"))
        cmd = runner.build_command("prompt", '{"type":"object"}')
        self.assertNotIn("--effort", cmd)

    def test_build_risk_command_uses_classifier_model_and_effort(self):
        runner = ClaudeRunner(claude_config(model="claude-sonnet-4-6", effort="max"), CredentialStore("/tmp/unused"))
        cmd = runner.build_risk_command('{"type":"object"}', model="claude-sonnet-4-6", effort="low")
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-sonnet-4-6")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")

    def test_assess_risk_passes_description_on_stdin_and_parses_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "fake-claude"
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
cat <<'JSON'
{"type":"result","is_error":false,"result":"{\\"risk\\":\\"low\\"}"}
JSON
""",
                encoding="utf-8",
            )
            command.chmod(0o755)
            runner = ClaudeRunner(
                claude_config(command=str(command), timeout_seconds=5),
                CredentialStore("/tmp/unused"),
            )

            risk = runner.assess_risk(
                description="Docs only",
                model="claude-sonnet-4-6",
                effort="low",
                timeout_seconds=5,
                run_dir=str(Path(tmp) / "risk"),
                is_superseded=lambda: False,
            )

            self.assertEqual(risk, "low")
            self.assertIn("Docs only", stdin_capture.read_text(encoding="utf-8"))
            self.assertNotIn("Docs only", argv_capture.read_text(encoding="utf-8"))

    def test_api_auth_env_uses_anthropic_api_key_and_configured_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "claude").write_text("secret\n", encoding="utf-8")
            runner = ClaudeRunner(
                claude_config(auth_mode="api", home_dir=str(Path(tmp) / "home")),
                CredentialStore(tmp),
            )
            env = runner._env()
            self.assertEqual(env["HOME"], str(Path(tmp) / "home"))
            self.assertEqual(env["ANTHROPIC_API_KEY"], "secret")

    def test_run_passes_prompt_on_stdin_without_argv_exposure(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "fake-claude"
            stdin_capture = Path(tmp) / "stdin.txt"
            argv_capture = Path(tmp) / "argv.txt"
            envelope = json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": valid_review_json(),
                    "modelUsage": {
                        "claude-sonnet-4-6": {
                            "inputTokens": 10,
                            "outputTokens": 20,
                            "cacheCreationInputTokens": 30,
                            "cacheReadInputTokens": 40,
                            "costUSD": 0.12,
                        }
                    },
                }
            )
            command.write_text(
                """#!/bin/sh
cat > '"""
                + str(stdin_capture)
                + """'
printf '%s\n' "$@" > '"""
                + str(argv_capture)
                + """'
cat <<'JSON'
"""
                + envelope
                + """
JSON
""",
                encoding="utf-8",
            )
            command.chmod(0o755)
            schema = Path(tmp) / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            runner = ClaudeRunner(
                claude_config(command=str(command), timeout_seconds=5),
                CredentialStore("/tmp/unused"),
            )

            result = runner.run(
                worktree=tmp,
                prompt="secret prompt",
                schema_path=str(schema),
                run_dir=str(Path(tmp) / "run"),
                is_superseded=lambda: False,
            )

            self.assertEqual(result.final_message.strip(), valid_review_json())
            self.assertEqual(stdin_capture.read_text(encoding="utf-8"), "secret prompt")
            self.assertNotIn("secret prompt", argv_capture.read_text(encoding="utf-8"))

    def test_run_extracts_result_from_claude_json_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "fake-claude"
            envelope = json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": valid_review_json(),
                    "modelUsage": {
                        "claude-sonnet-4-6": {
                            "inputTokens": 10,
                            "outputTokens": 20,
                            "cacheCreationInputTokens": 30,
                            "cacheReadInputTokens": 40,
                            "costUSD": 0.12,
                        }
                    },
                }
            )
            command.write_text(
                """#!/bin/sh
cat <<'JSON'
"""
                + envelope
                + """
JSON
echo diagnostics >&2
""",
                encoding="utf-8",
            )
            command.chmod(0o755)
            schema = Path(tmp) / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            runner = ClaudeRunner(
                claude_config(command=str(command), timeout_seconds=5),
                CredentialStore("/tmp/unused"),
            )

            result = runner.run(
                worktree=tmp,
                prompt="prompt",
                schema_path=str(schema),
                run_dir=str(Path(tmp) / "run"),
                is_superseded=lambda: False,
            )

            self.assertEqual(result.final_message.strip(), valid_review_json())
            self.assertEqual(result.usage["total_tokens"], 100)
            self.assertAlmostEqual(result.usage["cost_usd"], 0.12)
            self.assertEqual((Path(tmp) / "run" / "claude-stdout.log").read_text(encoding="utf-8").strip(), envelope)
            self.assertIn("diagnostics", result.stderr)

    def test_run_extracts_object_result_from_claude_json_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "fake-claude"
            command.write_text(
                """#!/bin/sh
cat <<'JSON'
{"type":"result","subtype":"success","is_error":false,"result":{"recommendation":"approve","report":{"title":"Claude PR Review","details":"No issues.","report_type":"BUG","reporter":"scout","data":[{"title":"Findings","type":"NUMBER","value":0}]},"annotations":[]}}
JSON
""",
                encoding="utf-8",
            )
            command.chmod(0o755)
            schema = Path(tmp) / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            runner = ClaudeRunner(
                claude_config(command=str(command), timeout_seconds=5),
                CredentialStore("/tmp/unused"),
            )

            result = runner.run(
                worktree=tmp,
                prompt="prompt",
                schema_path=str(schema),
                run_dir=str(Path(tmp) / "run"),
                is_superseded=lambda: False,
            )

            self.assertIn('"recommendation":"approve"', result.final_message)

    def test_run_extracts_embedded_json_from_successful_result_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "fake-claude"
            envelope = json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "Preparing final JSON.\n" + valid_review_json(),
                }
            )
            command.write_text(
                """#!/bin/sh
cat <<'JSON'
"""
                + envelope
                + """
JSON
""",
                encoding="utf-8",
            )
            command.chmod(0o755)
            schema = Path(tmp) / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            runner = ClaudeRunner(
                claude_config(command=str(command), timeout_seconds=5),
                CredentialStore("/tmp/unused"),
            )

            result = runner.run(
                worktree=tmp,
                prompt="prompt",
                schema_path=str(schema),
                run_dir=str(Path(tmp) / "run"),
                is_superseded=lambda: False,
            )

            self.assertEqual(result.final_message.strip(), valid_review_json())

    def test_run_extracts_schema_json_from_fenced_result_after_other_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "fake-claude"
            envelope = json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": (
                        'Subagents reported {"note":"not the review schema"}.\n'
                        "```json\n"
                        + valid_review_json()
                        + "\n```\n"
                    ),
                }
            )
            command.write_text(
                """#!/bin/sh
cat <<'JSON'
"""
                + envelope
                + """
JSON
""",
                encoding="utf-8",
            )
            command.chmod(0o755)
            schema = Path(tmp) / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            runner = ClaudeRunner(
                claude_config(command=str(command), timeout_seconds=5),
                CredentialStore("/tmp/unused"),
            )

            result = runner.run(
                worktree=tmp,
                prompt="prompt",
                schema_path=str(schema),
                run_dir=str(Path(tmp) / "run"),
                is_superseded=lambda: False,
            )

            self.assertEqual(result.final_message.strip(), valid_review_json())

    def test_run_rejects_successful_result_without_schema_json_as_provider_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "fake-claude"
            envelope = json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": 'I found issues, see {"note":"not the review schema"}.',
                }
            )
            command.write_text(
                """#!/bin/sh
cat <<'JSON'
"""
                + envelope
                + """
JSON
""",
                encoding="utf-8",
            )
            command.chmod(0o755)
            schema = Path(tmp) / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            runner = ClaudeRunner(
                claude_config(command=str(command), timeout_seconds=5),
                CredentialStore("/tmp/unused"),
            )

            with self.assertRaises(ProviderError) as raised:
                runner.run(
                    worktree=tmp,
                    prompt="prompt",
                    schema_path=str(schema),
                    run_dir=str(Path(tmp) / "run"),
                    is_superseded=lambda: False,
                )

            self.assertIn("did not contain a schema JSON object", str(raised.exception))

    def test_run_rejects_nonzero_exit_without_salvage(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "fake-claude"
            command.write_text(
                """#!/bin/sh
cat <<'JSON'
"""
                + valid_review_json()
                + """
JSON
echo failed >&2
exit 1
""",
                encoding="utf-8",
            )
            command.chmod(0o755)
            schema = Path(tmp) / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            runner = ClaudeRunner(
                claude_config(command=str(command), timeout_seconds=5),
                CredentialStore("/tmp/unused"),
            )

            with self.assertRaises(ProviderError) as raised:
                runner.run(
                    worktree=tmp,
                    prompt="prompt",
                    schema_path=str(schema),
                    run_dir=str(Path(tmp) / "run"),
                    is_superseded=lambda: False,
                )

            self.assertIn("Claude exited with status 1", str(raised.exception))

    def test_run_marks_usage_limit_errors_for_provider_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "fake-claude"
            command.write_text(
                """#!/bin/sh
echo "Claude usage limit reached. Your limit will reset at 7 PM." >&2
exit 1
""",
                encoding="utf-8",
            )
            command.chmod(0o755)
            schema = Path(tmp) / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            runner = ClaudeRunner(
                claude_config(command=str(command), timeout_seconds=5),
                CredentialStore("/tmp/unused"),
            )

            with self.assertRaises(ProviderError) as raised:
                runner.run(
                    worktree=tmp,
                    prompt="prompt",
                    schema_path=str(schema),
                    run_dir=str(Path(tmp) / "run"),
                    is_superseded=lambda: False,
                )

            self.assertEqual(raised.exception.cooldown_seconds, DEFAULT_PROVIDER_COOLDOWN_SECONDS)
            self.assertEqual(raised.exception.provider_status, "quota_exhausted")

    def test_run_rejects_error_envelope(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "fake-claude"
            command.write_text(
                """#!/bin/sh
cat <<'JSON'
{"type":"result","subtype":"error","is_error":true,"result":"failed"}
JSON
""",
                encoding="utf-8",
            )
            command.chmod(0o755)
            schema = Path(tmp) / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            runner = ClaudeRunner(
                claude_config(command=str(command), timeout_seconds=5),
                CredentialStore("/tmp/unused"),
            )

            with self.assertRaises(ProviderError) as raised:
                runner.run(
                    worktree=tmp,
                    prompt="prompt",
                    schema_path=str(schema),
                    run_dir=str(Path(tmp) / "run"),
                    is_superseded=lambda: False,
                )

            self.assertIn("Claude reported an error result", str(raised.exception))

    def test_run_rejects_missing_stdout_even_when_stderr_contains_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "fake-claude"
            command.write_text(
                """#!/bin/sh
cat >&2 <<'JSON'
"""
                + valid_review_json()
                + """
JSON
""",
                encoding="utf-8",
            )
            command.chmod(0o755)
            schema = Path(tmp) / "schema.json"
            schema.write_text('{"type":"object"}', encoding="utf-8")
            runner = ClaudeRunner(
                claude_config(command=str(command), timeout_seconds=5),
                CredentialStore("/tmp/unused"),
            )

            with self.assertRaises(ProviderError) as raised:
                runner.run(
                    worktree=tmp,
                    prompt="prompt",
                    schema_path=str(schema),
                    run_dir=str(Path(tmp) / "run"),
                    is_superseded=lambda: False,
                )

            self.assertIn("Claude did not write final JSON to stdout", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
