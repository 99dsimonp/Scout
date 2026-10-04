import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from scout.claude import ClaudeRunner
from scout.codex import CodexRunner
from scout.config import CredentialStore
from scout.provider import DEFAULT_PROVIDER_COOLDOWN_SECONDS, ProviderError, ProviderSuperseded
from test_claude import claude_config
from test_codex import codex_config


class SelectionRunnerTests(unittest.TestCase):
    def make_runner(self, provider, directory, body):
        command = Path(directory) / "fake-provider"
        command.write_text(
            "#!{}\nimport json, os, pathlib, sys, time\n".format(sys.executable)
            + "root = pathlib.Path({!r})\n".format(str(directory))
            + "(root / 'capture.json').write_text(json.dumps({'argv': sys.argv, 'stdin': sys.stdin.read(), 'env': dict(os.environ)}))\n"
            + "(root / 'pid').write_text(str(os.getpid()))\n"
            + body,
            encoding="utf-8",
        )
        command.chmod(0o755)
        config_factory, runner_type = (
            (codex_config, CodexRunner) if provider == "codex" else (claude_config, ClaudeRunner)
        )
        return runner_type(config_factory(command=str(command)), CredentialStore("/tmp/unused"))

    def classify(self, runner, directory, **overrides):
        args = dict(prompt="Private findings to compare", schema_json='{"type":"object"}',
                    model="cheap-model", effort="low", timeout_seconds=5,
                    run_dir=str(Path(directory) / "selection"), is_superseded=lambda: False)
        args.update(overrides)
        return runner.classify_findings(**args)

    def output_script(self, provider, output):
        if provider == "codex":
            return "pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text({!r})\n".format(output)
        return "print({!r})\n".format(output)

    def test_selection_uses_stdin_cheap_settings_and_scrubbed_environment(self):
        expected = {"decisions": [{"candidate_id": "one", "decision": "keep"}]}
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as tmp:
                output = json.dumps(expected if provider == "codex" else {"result": json.dumps(expected), "is_error": False})
                runner = self.make_runner(provider, tmp, self.output_script(provider, output))
                with patch.dict(os.environ, {"BITBUCKET_PASSWORD": "private", "SSH_AUTH_SOCK": "/secret", "OPENAI_API_KEY": "secret", "ANTHROPIC_API_KEY": "secret"}):
                    result = self.classify(runner, tmp)
                self.assertEqual(json.loads(result), expected)
                capture = json.loads((Path(tmp) / "capture.json").read_text())
                args = capture["argv"]
                self.assertEqual(capture["stdin"], "Private findings to compare")
                self.assertNotIn(capture["stdin"], args)
                self.assertEqual(args[args.index("--model") + 1], "cheap-model")
                for secret in ("BITBUCKET_PASSWORD", "SSH_AUTH_SOCK", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
                    self.assertNotIn(secret, capture["env"])
                if provider == "claude":
                    self.assertEqual(args[args.index("--tools") + 1], "")
                    self.assertEqual(args[args.index("--effort") + 1], "low")
                else:
                    self.assertIn('model_reasoning_effort="low"', args)
                    self.assertIn("features.shell_tool=false", args)
                    self.assertIn("features.multi_agent=false", args)

    def test_claude_accepts_structured_output_or_result_without_review_shape(self):
        expected = {"decisions": []}
        for output in (expected, {"result": expected}, {"structured_output": expected, "result": ""}):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as tmp:
                runner = self.make_runner("claude", tmp, self.output_script("claude", json.dumps(output)))
                self.assertEqual(json.loads(self.classify(runner, tmp)), expected)

    def test_api_auth_exposes_only_selected_provider_credential(self):
        for provider, key, other_key in (
            ("codex", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"),
            ("claude", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"),
        ):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as tmp:
                runner = self.make_runner(provider, tmp, self.output_script(provider, '{"decisions":[]}'))
                runner.config = replace(runner.config, auth_mode="api", home_dir=str(Path(tmp) / "provider-home"))
                runner.credentials = CredentialStore(tmp)
                (Path(tmp) / provider).write_text("provider-key")
                self.classify(runner, tmp)
                env = json.loads((Path(tmp) / "capture.json").read_text())["env"]
                self.assertEqual(env[key], "provider-key")
                self.assertEqual(env["HOME"], runner.config.home_dir)
                self.assertNotIn(other_key, env)

    def test_claude_rejects_malformed_or_empty_result(self):
        for output in ("not JSON", '{"result":""}', '{"result":[]}'):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as tmp:
                runner = self.make_runner("claude", tmp, self.output_script("claude", output))
                with self.assertRaises(ProviderError):
                    self.classify(runner, tmp)

    def test_codex_does_not_reuse_old_final_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = self.make_runner("codex", tmp, "pass\n")
            run_dir = Path(tmp) / "selection"
            run_dir.mkdir()
            output = run_dir / "codex-selection-final-message.json"
            output.write_text('{"decisions":[]}')
            with self.assertRaisesRegex(ProviderError, "did not write"):
                self.classify(runner, tmp)
            self.assertFalse(output.exists())

    def test_quota_failure_preserves_provider_cooldown(self):
        for provider in ("codex", "claude"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as tmp:
                runner = self.make_runner(provider, tmp, "print('Usage limit reached', file=sys.stderr)\nsys.exit(1)\n")
                with self.assertRaises(ProviderError) as raised:
                    self.classify(runner, tmp)
                self.assertEqual(raised.exception.cooldown_seconds, DEFAULT_PROVIDER_COOLDOWN_SECONDS)
                self.assertEqual(raised.exception.provider_status, "quota_exhausted")

    def test_claude_error_envelope_is_not_accepted_as_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = json.dumps({"is_error": True, "result": "Usage limit reached"})
            runner = self.make_runner("claude", tmp, self.output_script("claude", output))
            with self.assertRaises(ProviderError) as raised:
                self.classify(runner, tmp)
            self.assertEqual(raised.exception.cooldown_seconds, DEFAULT_PROVIDER_COOLDOWN_SECONDS)

    def test_timeout_and_cancellation_reap_process(self):
        for provider in ("codex", "claude"):
            for cancelled in (False, True):
                with self.subTest(provider=provider, cancelled=cancelled), tempfile.TemporaryDirectory() as tmp:
                    runner = self.make_runner(provider, tmp, "time.sleep(60)\n")
                    error = ProviderSuperseded if cancelled else ProviderError
                    kwargs = {"is_superseded": lambda: (Path(tmp) / "pid").exists()} if cancelled else {"timeout_seconds": 0.5}
                    with patch("scout.provider.POLL_INTERVAL_SECONDS", 0.05), self.assertRaises(error):
                        self.classify(runner, tmp, **kwargs)
                    pid = int((Path(tmp) / "pid").read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)

    def test_spawn_failure_is_provider_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = CodexRunner(codex_config(command="/missing/scout-provider"), CredentialStore("/tmp/unused"))
            with self.assertRaises(ProviderError):
                self.classify(runner, tmp)


if __name__ == "__main__":
    unittest.main()
