from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import shutil
import stat
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, Optional

from .models import PullRequest
from .review_plan import count_changed_lines

LOG = logging.getLogger(__name__)
DEFAULT_GIT_TIMEOUT_SECONDS = 600


class GitError(RuntimeError):
    pass


class GitManager:
    def __init__(
        self,
        state_dir: str,
        ssh_key_path: Optional[str] = None,
        git_timeout_seconds: int = DEFAULT_GIT_TIMEOUT_SECONDS,
    ):
        self.state_dir = Path(state_dir)
        self.repos_dir = self.state_dir / "repos"
        self.worktrees_dir = self.state_dir / "worktrees"
        self.ssh_key_path = ssh_key_path
        self.git_timeout_seconds = git_timeout_seconds
        self._mirror_locks: Dict[Path, threading.Lock] = {}
        self._mirror_locks_guard = threading.Lock()

    def ensure_mirror(self, workspace: str, repo_slug: str, clone_url: str) -> Path:
        mirror = self.repos_dir / workspace / "{}.git".format(repo_slug)
        mirror.parent.mkdir(parents=True, exist_ok=True)
        with self._mirror_lock(mirror):
            self._refresh_local_clone(clone_url)
            if mirror.exists():
                self._git(["-C", str(mirror), "fetch", "--prune", "origin"])
            else:
                self._git(["clone", "--mirror", clone_url, str(mirror)])
        return mirror

    def validate_clone_url(self, clone_url: str) -> None:
        self._git(["ls-remote", "--heads", clone_url])

    def create_worktree(self, mirror: Path, pr: PullRequest, suffix: Optional[str] = None) -> Path:
        short_commit = pr.source_commit_hash[:12]
        worktree_name = "pr-{}-{}".format(pr.pr_id, short_commit)
        if suffix:
            worktree_name = "{}-{}".format(worktree_name, suffix)
        worktree = self.worktrees_dir / pr.workspace / pr.repo_slug / worktree_name
        with self._mirror_lock(mirror):
            if worktree.exists():
                self._remove_worktree_unlocked(mirror, worktree)
            worktree.parent.mkdir(parents=True, exist_ok=True)
            try:
                self._git(["-C", str(mirror), "worktree", "add", "--detach", str(worktree), pr.source_commit_hash])
            except Exception:
                self._remove_worktree_unlocked(mirror, worktree)
                raise
        return worktree

    def prepare_context(self, mirror: Path, worktree: Path, pr: PullRequest) -> Dict[str, str]:
        base_ref = pr.destination_commit_hash or pr.destination_branch
        merge_base = self._git_capture(["-C", str(worktree), "merge-base", "HEAD", base_ref]).strip()
        diff = self._git_capture(["-C", str(worktree), "diff", "{}..HEAD".format(merge_base)])
        files = self._git_capture(["-C", str(worktree), "diff", "--name-only", "{}..HEAD".format(merge_base)])
        changed_lines = count_changed_lines(diff)
        context_dir = worktree / ".scout-review"
        context_dir.mkdir(parents=True, exist_ok=True)
        context = {
            "workspace": pr.workspace,
            "repo_slug": pr.repo_slug,
            "pr_id": str(pr.pr_id),
            "title": pr.title,
            "description": pr.description,
            "source_branch": pr.source_branch,
            "source_commit": pr.source_commit_hash,
            "target_branch": pr.destination_branch,
            "target_commit": pr.destination_commit_hash or "",
            "merge_base": merge_base,
            "changed_lines": str(changed_lines),
            "diff_path": str(context_dir / "diff.patch"),
            "files_path": str(context_dir / "files.txt"),
        }
        (context_dir / "context.json").write_text(json.dumps(context, indent=2, sort_keys=True), encoding="utf-8")
        (context_dir / "diff.patch").write_text(diff, encoding="utf-8")
        (context_dir / "files.txt").write_text(files, encoding="utf-8")
        self._make_readonly(worktree)
        context["context_path"] = str(context_dir / "context.json")
        return context

    def remove_worktree(self, mirror: Path, worktree: Path) -> None:
        with self._mirror_lock(mirror):
            self._remove_worktree_unlocked(mirror, worktree)

    def _remove_worktree_unlocked(self, mirror: Path, worktree: Path) -> None:
        try:
            if worktree.exists():
                self._make_writable(worktree)
            self._git(["-C", str(mirror), "worktree", "remove", "--force", str(worktree)])
        except Exception:
            shutil.rmtree(worktree, ignore_errors=True)
            try:
                self._git(["-C", str(mirror), "worktree", "prune"])
            except GitError:
                pass

    def _git(self, args: list) -> None:
        result = self._run_git(args)
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or "git command failed")

    def _git_capture(self, args: list) -> str:
        result = self._run_git(args)
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or "git command failed")
        return result.stdout

    def _refresh_local_clone(self, clone_url: str) -> None:
        source = Path(clone_url)
        if not source.exists():
            return
        result = self._run_git(["-C", str(source), "rev-parse", "--git-dir"])
        if result.returncode != 0:
            return
        origin = self._run_git(["-C", str(source), "config", "--get", "remote.origin.url"])
        if origin.returncode == 0 and origin.stdout.strip():
            self._git(["-C", str(source), "fetch", "--prune", "origin"])

    @contextmanager
    def _mirror_lock(self, mirror: Path) -> Iterator[None]:
        lock_path = self._mirror_lock_path(mirror)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        thread_lock = self._mirror_thread_lock(lock_path)
        with thread_lock:
            with lock_path.open("w", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _mirror_lock_path(self, mirror: Path) -> Path:
        digest = hashlib.sha256(str(Path(mirror).resolve()).encode("utf-8")).hexdigest()
        return self.state_dir / "locks" / "{}.lock".format(digest)

    def _mirror_thread_lock(self, lock_path: Path) -> threading.Lock:
        with self._mirror_locks_guard:
            lock = self._mirror_locks.get(lock_path)
            if lock is None:
                lock = threading.Lock()
                self._mirror_locks[lock_path] = lock
            return lock

    def _run_git(self, args: list) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", "-c", "filter.lfs.smudge=", "-c", "filter.lfs.required=false"] + args,
                env=self._env(),
                text=True,
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.git_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(
                "git command timed out after {} seconds".format(self.git_timeout_seconds)
            ) from exc

    def _env(self) -> Dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin"),
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
        if self.ssh_key_path:
            env["GIT_SSH_COMMAND"] = (
                "ssh -i {} -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new".format(self.ssh_key_path)
            )
        else:
            env["GIT_SSH_COMMAND"] = "ssh -o StrictHostKeyChecking=accept-new"
        return env

    def _make_readonly(self, path: Path) -> None:
        for root, dirs, files in os.walk(path):
            for dirname in dirs:
                self._chmod_if_regular(Path(root) / dirname, stat.S_IREAD | stat.S_IEXEC)
            for filename in files:
                self._chmod_if_regular(Path(root) / filename, stat.S_IREAD)
        self._chmod_if_regular(path, stat.S_IREAD | stat.S_IEXEC)

    def _make_writable(self, path: Path) -> None:
        for root, dirs, files in os.walk(path):
            for dirname in dirs:
                self._chmod_if_regular(Path(root) / dirname, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
            for filename in files:
                self._chmod_if_regular(Path(root) / filename, stat.S_IREAD | stat.S_IWRITE)
        self._chmod_if_regular(path, stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)

    def _chmod_if_regular(self, path: Path, mode: int) -> None:
        try:
            if path.is_symlink():
                return
            os.chmod(path, mode)
        except FileNotFoundError:
            return
