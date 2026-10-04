from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .comment_request import (
    CommentRequestClassification,
    build_comment_request_prompt,
    comment_request_schema_json,
    extract_comment_request,
)
from .config import CodexConfig, CredentialStore
from .provider import (
    ProcessOutput,
    ProviderError,
    ProviderResult,
    redacted_cmd as _redacted_cmd,
    require_provider_success,
    run_provider_command,
    run_selection_command,
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
            self.credentials.read(self.config.credential)

    def run(
        self,
        worktree: str,
        prompt: str,
        schema_path: str,
        run_dir: str,
        is_superseded: Callable[[], bool],
        additional_dirs: Optional[List[str]] = None,
    ) -> ProviderResult:
        output_file = Path(run_dir) / "codex-final-message.json"
        cmd = self.build_command(
            worktree,
            schema_path,
            str(output_file),
            prompt,
            additional_dirs=additional_dirs,
        )
        output, diagnostics = self._run_command(
            "review", "codex", "Codex review", cmd, prompt, run_dir, output_file,
            self.config.timeout_seconds, is_superseded, "review superseded by a newer PR commit",
        )
        require_provider_success(output, "Codex", "Codex did not write a final message", diagnostics)
        return ProviderResult(
            stdout=output.stdout,
            stderr=output.stderr,
            final_message=output.final_message,
            diagnostics=diagnostics,
            usage=parse_codex_usage(output.stdout, output.stderr, output.final_message),
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
        label = "Codex risk classification"
        output_file = Path(run_dir) / "codex-risk-final-message.json"
        schema_file = Path(run_dir) / "risk.schema.json"
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        schema_file.write_text(risk_schema_json(), encoding="utf-8")
        cmd = self.build_risk_command(
            worktree=run_dir,
            schema_path=str(schema_file),
            output_file=str(output_file),
            model=model,
            reasoning_effort=reasoning_effort,
        )
        output, diagnostics = self._run_command(
            "risk", "codex-risk", label, cmd, build_risk_prompt(description), run_dir, output_file,
            timeout_seconds, is_superseded, "review superseded by a newer PR commit",
        )
        require_provider_success(output, label, "{} did not write a final message".format(label), diagnostics)
        return extract_risk(output.final_message)

    def classify_review_request(
        self,
        comment: str,
        model: str,
        reasoning_effort: str,
        timeout_seconds: int,
        run_dir: str,
        is_superseded: Callable[[], bool],
    ) -> CommentRequestClassification:
        label = "Codex review request classification"
        output_file = Path(run_dir) / "codex-comment-request-final-message.json"
        schema_file = Path(run_dir) / "comment-request.schema.json"
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        schema_file.write_text(comment_request_schema_json(), encoding="utf-8")
        cmd = self.build_comment_request_command(
            worktree=run_dir,
            schema_path=str(schema_file),
            output_file=str(output_file),
            model=model,
            reasoning_effort=reasoning_effort,
        )
        output, diagnostics = self._run_command(
            "comment request", "codex-comment-request", label, cmd, build_comment_request_prompt(comment),
            run_dir, output_file, timeout_seconds, is_superseded,
            "review request classification superseded by a newer PR comment",
        )
        require_provider_success(output, label, "{} did not write a final message".format(label), diagnostics)
        return extract_comment_request(output.final_message)

    def _run_command(
        self,
        kind: str,
        file_prefix: str,
        label: str,
        cmd: list,
        prompt: str,
        run_dir: str,
        output_file: Path,
        timeout_seconds: int,
        is_superseded: Callable[[], bool],
        superseded_message: str,
    ) -> Tuple[ProcessOutput, CodexDiagnostics]:
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        output_file.unlink(missing_ok=True)
        stdout_file = Path(run_dir) / "{}-stdout.log".format(file_prefix)
        stderr_file = Path(run_dir) / "{}-stderr.log".format(file_prefix)
        prompt_file = Path(run_dir) / "{}-prompt.txt".format(file_prefix)
        prompt_file.write_text(prompt, encoding="utf-8")
        LOG.info("starting Codex %s command=%s prompt_file=%s", kind, _redacted_cmd(cmd), prompt_file)
        output = run_provider_command(
            "codex", label, cmd, prompt_file, stdout_file, stderr_file, self._env(),
            timeout_seconds, is_superseded, superseded_message, output_file=output_file,
        )
        diagnostics = _build_diagnostics(output.stdout, output.stderr, output.final_message)
        LOG.info(
            "Codex %s completed returncode=%s stdout_file=%s stderr_file=%s output_file=%s %s",
            kind,
            output.returncode,
            stdout_file,
            stderr_file,
            output_file,
            diagnostics.summary(),
        )
        return output, diagnostics

    def classify_findings(
        self,
        prompt: str,
        schema_json: str,
        model: str,
        effort: str,
        timeout_seconds: int,
        run_dir: str,
        is_superseded: Callable[[], bool],
    ) -> str:
        run_dir = str(Path(run_dir).resolve())
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        output_file = Path(run_dir) / "codex-selection-final-message.json"
        output_file.unlink(missing_ok=True)
        prompt_file = Path(run_dir) / "codex-selection-prompt.txt"
        schema_file = Path(run_dir) / "selection.schema.json"
        prompt_file.write_text(prompt, encoding="utf-8")
        schema_file.write_text(schema_json, encoding="utf-8")
        cmd = self.build_comment_request_command(
            run_dir, str(schema_file), str(output_file), model, effort,
        )
        cmd.extend([
            "--config", "features.shell_tool=false",
            "--config", "features.multi_agent=false",
            "--config", 'web_search="disabled"',
        ])
        LOG.info("starting Codex selection command=%s prompt_file=%s", _redacted_cmd(cmd), prompt_file)
        return run_selection_command(
            "codex", cmd, prompt_file, run_dir, self._env(), timeout_seconds,
            is_superseded, output_file=output_file,
        )

    def build_command(
        self,
        worktree: str,
        schema_path: str,
        output_file: str,
        prompt: str,
        additional_dirs: Optional[List[str]] = None,
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
        for directory in additional_dirs or []:
            cmd.extend(["--add-dir", directory])
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
