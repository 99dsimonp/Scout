from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .comment_request import (
    CommentRequestClassification,
    build_comment_request_prompt,
    comment_request_schema_json,
    extract_comment_request,
)
from .config import CodexConfig, CredentialStore
from .provider import (
    PROVIDER_COOLDOWN_STATUS,
    ProviderError,
    ProviderResult,
    ProviderSuperseded,
    provider_quota_cooldown_seconds,
    read_text as _read_text,
    redacted_cmd as _redacted_cmd,
    terminate_process_group as _terminate_process_group,
)
from .risk import build_risk_prompt, extract_risk, risk_schema_json
from .usage import parse_codex_usage

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodexDiagnostics:
    spawned_subagents: int
    thread_limit_errors: int
    reconnects: int
    stream_disconnected: bool
    final_message_present: bool

    def summary(self) -> str:
        return (
            "codex diagnostics: spawned_subagents={spawned_subagents} "
            "thread_limit_errors={thread_limit_errors} reconnects={reconnects} "
            "stream_disconnected={stream_disconnected} "
            "final_message_present={final_message_present}"
        ).format(**self.__dict__)


class CodexRunner:
    def __init__(self, config: CodexConfig, credentials: CredentialStore):
        self.config = config
        self.credentials = credentials

    def validate_startup(self) -> None:
        if not self.config.enabled:
            raise ProviderError("Codex provider is disabled", retryable=False)
        if shutil.which(self.config.command) is None:
            raise ProviderError("Codex command not found: {}".format(self.config.command), retryable=False)
        if self.config.auth_mode == "api":
            Path(self.config.home_dir).mkdir(parents=True, exist_ok=True)
        if self.config.auth_mode == "api":
            self.credentials.read(self.config.credential)

    def run(
        self,
        worktree: str,
        prompt: str,
        schema_path: str,
        run_dir: str,
        is_superseded: Callable[[], bool],
    ) -> ProviderResult:
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        output_file = Path(run_dir) / "codex-final-message.json"
        stdout_file = Path(run_dir) / "codex-stdout.log"
        stderr_file = Path(run_dir) / "codex-stderr.log"
        prompt_file = Path(run_dir) / "codex-prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")

        cmd = self.build_command(worktree, schema_path, str(output_file), prompt)
        LOG.info("starting Codex review command=%s prompt_file=%s", _redacted_cmd(cmd), prompt_file)
        with prompt_file.open("r", encoding="utf-8") as prompt_input, \
            stdout_file.open("w", encoding="utf-8") as stdout, \
            stderr_file.open("w", encoding="utf-8") as stderr:
            proc = subprocess.Popen(
                cmd,
                stdin=prompt_input,
                stdout=stdout,
                stderr=stderr,
                text=True,
                env=self._env(),
                start_new_session=True,
            )
            started = time.monotonic()
            while True:
                if proc.poll() is not None:
                    break
                if is_superseded():
                    _terminate_process_group(proc)
                    raise ProviderSuperseded("review superseded by a newer PR commit")
                if time.monotonic() - started > self.config.timeout_seconds:
                    _terminate_process_group(proc)
                    stdout.flush()
                    stderr.flush()
                    cooldown_seconds = provider_quota_cooldown_seconds(
                        "codex",
                        _read_text(stdout_file),
                        _read_text(stderr_file),
                        _read_text(output_file),
                    )
                    raise ProviderError(
                        "Codex review timed out after {} seconds".format(self.config.timeout_seconds),
                        cooldown_seconds=cooldown_seconds,
                        provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
                    )
                time.sleep(1)

        stdout_text = _read_text(stdout_file)
        stderr_text = _read_text(stderr_file)
        final_message = _read_text(output_file)
        diagnostics = _build_diagnostics(stdout_text, stderr_text, final_message)
        cooldown_seconds = provider_quota_cooldown_seconds("codex", stdout_text, stderr_text, final_message)
        LOG.info(
            "Codex review completed returncode=%s stdout_file=%s stderr_file=%s output_file=%s %s",
            proc.returncode,
            stdout_file,
            stderr_file,
            output_file,
            diagnostics.summary(),
        )
        if proc.returncode != 0:
            raise ProviderError(
                "Codex exited with status {}: {}".format(proc.returncode, stderr_text[:1000]),
                diagnostics=diagnostics,
                cooldown_seconds=cooldown_seconds,
                provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
            )
        if not final_message.strip():
            raise ProviderError(
                "Codex did not write a final message",
                diagnostics=diagnostics,
                cooldown_seconds=cooldown_seconds,
                provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
            )
        return ProviderResult(
            stdout=stdout_text,
            stderr=stderr_text,
            final_message=final_message,
            diagnostics=diagnostics,
            usage=parse_codex_usage(stdout_text, stderr_text, final_message),
        )

    def assess_risk(
        self,
        description: str,
        model: str,
        reasoning_effort: str,
        timeout_seconds: int,
        run_dir: str,
        is_superseded: Callable[[], bool],
    ) -> str:
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        output_file = Path(run_dir) / "codex-risk-final-message.json"
        stdout_file = Path(run_dir) / "codex-risk-stdout.log"
        stderr_file = Path(run_dir) / "codex-risk-stderr.log"
        prompt_file = Path(run_dir) / "codex-risk-prompt.txt"
        schema_file = Path(run_dir) / "risk.schema.json"
        prompt_file.write_text(build_risk_prompt(description), encoding="utf-8")
        schema_file.write_text(risk_schema_json(), encoding="utf-8")

        cmd = self.build_risk_command(
            worktree=run_dir,
            schema_path=str(schema_file),
            output_file=str(output_file),
            model=model,
            reasoning_effort=reasoning_effort,
        )
        LOG.info("starting Codex risk command=%s prompt_file=%s", _redacted_cmd(cmd), prompt_file)
        with prompt_file.open("r", encoding="utf-8") as prompt_input, \
            stdout_file.open("w", encoding="utf-8") as stdout, \
            stderr_file.open("w", encoding="utf-8") as stderr:
            proc = subprocess.Popen(
                cmd,
                stdin=prompt_input,
                stdout=stdout,
                stderr=stderr,
                text=True,
                env=self._env(),
                start_new_session=True,
            )
            started = time.monotonic()
            while True:
                if proc.poll() is not None:
                    break
                if is_superseded():
                    _terminate_process_group(proc)
                    raise ProviderSuperseded("review superseded by a newer PR commit")
                if time.monotonic() - started > timeout_seconds:
                    _terminate_process_group(proc)
                    stdout.flush()
                    stderr.flush()
                    cooldown_seconds = provider_quota_cooldown_seconds(
                        "codex",
                        _read_text(stdout_file),
                        _read_text(stderr_file),
                        _read_text(output_file),
                    )
                    raise ProviderError(
                        "Codex risk classification timed out after {} seconds".format(timeout_seconds),
                        cooldown_seconds=cooldown_seconds,
                        provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
                    )
                time.sleep(1)

        stdout_text = _read_text(stdout_file)
        stderr_text = _read_text(stderr_file)
        final_message = _read_text(output_file)
        diagnostics = _build_diagnostics(stdout_text, stderr_text, final_message)
        cooldown_seconds = provider_quota_cooldown_seconds("codex", stdout_text, stderr_text, final_message)
        LOG.info(
            "Codex risk completed returncode=%s stdout_file=%s stderr_file=%s output_file=%s %s",
            proc.returncode,
            stdout_file,
            stderr_file,
            output_file,
            diagnostics.summary(),
        )
        if proc.returncode != 0:
            raise ProviderError(
                "Codex risk classification exited with status {}: {}".format(proc.returncode, stderr_text[:1000]),
                diagnostics=diagnostics,
                cooldown_seconds=cooldown_seconds,
                provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
            )
        if not final_message.strip():
            raise ProviderError(
                "Codex risk classification did not write a final message",
                diagnostics=diagnostics,
                cooldown_seconds=cooldown_seconds,
                provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
            )
        return extract_risk(final_message)

    def classify_review_request(
        self,
        comment: str,
        model: str,
        reasoning_effort: str,
        timeout_seconds: int,
        run_dir: str,
        is_superseded: Callable[[], bool],
    ) -> CommentRequestClassification:
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        output_file = Path(run_dir) / "codex-comment-request-final-message.json"
        stdout_file = Path(run_dir) / "codex-comment-request-stdout.log"
        stderr_file = Path(run_dir) / "codex-comment-request-stderr.log"
        prompt_file = Path(run_dir) / "codex-comment-request-prompt.txt"
        schema_file = Path(run_dir) / "comment-request.schema.json"
        prompt_file.write_text(build_comment_request_prompt(comment), encoding="utf-8")
        schema_file.write_text(comment_request_schema_json(), encoding="utf-8")

        cmd = self.build_comment_request_command(
            worktree=run_dir,
            schema_path=str(schema_file),
            output_file=str(output_file),
            model=model,
            reasoning_effort=reasoning_effort,
        )
        LOG.info("starting Codex comment request command=%s prompt_file=%s", _redacted_cmd(cmd), prompt_file)
        with prompt_file.open("r", encoding="utf-8") as prompt_input, \
            stdout_file.open("w", encoding="utf-8") as stdout, \
            stderr_file.open("w", encoding="utf-8") as stderr:
            proc = subprocess.Popen(
                cmd,
                stdin=prompt_input,
                stdout=stdout,
                stderr=stderr,
                text=True,
                env=self._env(),
                start_new_session=True,
            )
            started = time.monotonic()
            while True:
                if proc.poll() is not None:
                    break
                if is_superseded():
                    _terminate_process_group(proc)
                    raise ProviderSuperseded("review request classification superseded by a newer PR comment")
                if time.monotonic() - started > timeout_seconds:
                    _terminate_process_group(proc)
                    stdout.flush()
                    stderr.flush()
                    cooldown_seconds = provider_quota_cooldown_seconds(
                        "codex",
                        _read_text(stdout_file),
                        _read_text(stderr_file),
                        _read_text(output_file),
                    )
                    raise ProviderError(
                        "Codex review request classification timed out after {} seconds".format(timeout_seconds),
                        cooldown_seconds=cooldown_seconds,
                        provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
                    )
                time.sleep(1)

        stdout_text = _read_text(stdout_file)
        stderr_text = _read_text(stderr_file)
        final_message = _read_text(output_file)
        diagnostics = _build_diagnostics(stdout_text, stderr_text, final_message)
        cooldown_seconds = provider_quota_cooldown_seconds("codex", stdout_text, stderr_text, final_message)
        LOG.info(
            "Codex comment request completed returncode=%s stdout_file=%s stderr_file=%s output_file=%s %s",
            proc.returncode,
            stdout_file,
            stderr_file,
            output_file,
            diagnostics.summary(),
        )
        if proc.returncode != 0:
            raise ProviderError(
                "Codex review request classification exited with status {}: {}".format(
                    proc.returncode,
                    stderr_text[:1000],
                ),
                diagnostics=diagnostics,
                cooldown_seconds=cooldown_seconds,
                provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
            )
        if not final_message.strip():
            raise ProviderError(
                "Codex review request classification did not write a final message",
                diagnostics=diagnostics,
                cooldown_seconds=cooldown_seconds,
                provider_status=PROVIDER_COOLDOWN_STATUS if cooldown_seconds else None,
            )
        return extract_comment_request(final_message)

    def build_command(self, worktree: str, schema_path: str, output_file: str, prompt: str) -> list:
        cmd = [
            self.config.command,
            "exec",
        ]
        if self.config.fast_mode:
            cmd.extend(["--enable", "fast_mode"])
        else:
            cmd.extend(["--disable", "fast_mode"])
        cmd.extend(
            [
                "--model",
                self.config.model,
                "--config",
                'model_reasoning_effort="{}"'.format(self.config.reasoning_effort),
                "--cd",
                worktree,
                "--sandbox",
                "read-only",
                "--output-schema",
                schema_path,
                "--output-last-message",
                output_file,
            ]
        )
        return cmd

    def build_risk_command(
        self,
        worktree: str,
        schema_path: str,
        output_file: str,
        model: str,
        reasoning_effort: str,
    ) -> list:
        cmd = [
            self.config.command,
            "exec",
        ]
        if self.config.fast_mode:
            cmd.extend(["--enable", "fast_mode"])
        else:
            cmd.extend(["--disable", "fast_mode"])
        cmd.extend(
            [
                "--model",
                model,
                "--skip-git-repo-check",
                "--config",
                'model_reasoning_effort="{}"'.format(reasoning_effort),
                "--cd",
                worktree,
                "--sandbox",
                "read-only",
                "--output-schema",
                schema_path,
                "--output-last-message",
                output_file,
            ]
        )
        return cmd

    def build_comment_request_command(
        self,
        worktree: str,
        schema_path: str,
        output_file: str,
        model: str,
        reasoning_effort: str,
    ) -> list:
        return self.build_risk_command(
            worktree=worktree,
            schema_path=schema_path,
            output_file=output_file,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    def _env(self) -> dict:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
        }
        if self.config.auth_mode == "api":
            env["HOME"] = self.config.home_dir
            env["OPENAI_API_KEY"] = self.credentials.read(self.config.credential)
        elif os.environ.get("HOME"):
            env["HOME"] = os.environ["HOME"]
        return env

def _build_diagnostics(stdout: str, stderr: str, final_message: str) -> CodexDiagnostics:
    combined = "{}\n{}".format(stdout, stderr)
    lower = combined.lower()
    return CodexDiagnostics(
        spawned_subagents=combined.count("collab: SpawnAgent"),
        thread_limit_errors=lower.count("agent thread limit reached"),
        reconnects=combined.count("ERROR: Reconnecting"),
        stream_disconnected="stream disconnected" in lower,
        final_message_present=bool(final_message.strip()),
    )
