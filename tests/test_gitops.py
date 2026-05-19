import subprocess
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scout.gitops import DEFAULT_GIT_TIMEOUT_SECONDS, GitError, GitManager


class GitManagerTests(unittest.TestCase):
    def run_git(self, args, cwd=None):
        return subprocess.run(
            ["git"] + args,
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def git_output(self, args, cwd=None):
        return self.run_git(args, cwd=cwd).stdout.strip()

    def test_git_commands_use_default_timeout(self):
        manager = GitManager("/tmp/state")
        completed = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")

        with patch("scout.gitops.subprocess.run", return_value=completed) as run:
            manager._git(["status"])

        self.assertEqual(run.call_args.kwargs["timeout"], DEFAULT_GIT_TIMEOUT_SECONDS)
        self.assertEqual(
            run.call_args.kwargs["env"]["GIT_SSH_COMMAND"],
            "ssh -o StrictHostKeyChecking=accept-new",
        )
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_git_capture_decodes_non_utf8_output_with_replacement(self):
        manager = GitManager("/tmp/state")

        def fake_run(*args, **kwargs):
            self.assertEqual(kwargs["errors"], "replace")
            return subprocess.CompletedProcess(
                args=args[0],
                returncode=0,
                stdout="diff --git a/file b/file\n+\ufffd\n",
                stderr="",
            )

        with patch("scout.gitops.subprocess.run", side_effect=fake_run):
            output = manager._git_capture(["diff", "base..HEAD"])

        self.assertIn("\ufffd", output)

    def test_git_commands_use_explicit_ssh_key_when_configured(self):
        manager = GitManager("/tmp/state", ssh_key_path="/run/credentials/key")
        completed = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")

        with patch("scout.gitops.subprocess.run", return_value=completed) as run:
            manager._git(["status"])

        self.assertEqual(
            run.call_args.kwargs["env"]["GIT_SSH_COMMAND"],
            "ssh -i /run/credentials/key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new",
        )

    def test_git_timeout_raises_git_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = GitManager(tmp, git_timeout_seconds=7)

            with patch(
                "scout.gitops.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["git", "status"], 7),
            ):
                with self.assertRaises(GitError) as raised:
                    manager._git_capture(["status"])

            self.assertEqual(str(raised.exception), "git command timed out after 7 seconds")

    def test_create_worktree_can_use_job_specific_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = GitManager(tmp)
            pr = type(
                "PR",
                (),
                {
                    "workspace": "workspace",
                    "repo_slug": "repo",
                    "pr_id": 12,
                    "source_commit_hash": "abcdef1234567890",
                },
            )()

            with patch.object(manager, "_git") as git:
                worktree = manager.create_worktree(Path(tmp) / "repo.git", pr, suffix="job-9")

            self.assertEqual(worktree.name, "pr-12-abcdef123456-job-9")
            git.assert_called_once()

    def test_create_worktree_removes_partial_directory_when_git_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = GitManager(tmp)
            mirror = Path(tmp) / "repo.git"
            pr = type(
                "PR",
                (),
                {
                    "workspace": "workspace",
                    "repo_slug": "repo",
                    "pr_id": 12,
                    "source_commit_hash": "abcdef1234567890",
                },
            )()

            def fake_git(args):
                worktree = Path(tmp) / "worktrees" / "workspace" / "repo" / "pr-12-abcdef123456-job-9"
                if args[1:4] == [str(mirror), "worktree", "add"]:
                    worktree.mkdir(parents=True)
                    (worktree / "partial.txt").write_text("partial", encoding="utf-8")
                    raise GitError("No space left on device")
                if args[1:4] == [str(mirror), "worktree", "remove"]:
                    shutil.rmtree(worktree)

            with patch.object(manager, "_git", side_effect=fake_git):
                with self.assertRaises(GitError):
                    manager.create_worktree(mirror, pr, suffix="job-9")

            worktree = Path(tmp) / "worktrees" / "workspace" / "repo" / "pr-12-abcdef123456-job-9"
            self.assertFalse(worktree.exists())

    def test_readonly_and_writable_ignore_broken_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = GitManager(tmp)
            root = Path(tmp) / "worktree"
            root.mkdir()
            (root / "file.txt").write_text("content", encoding="utf-8")
            (root / "broken").symlink_to(root / "missing")

            manager._make_readonly(root)
            manager._make_writable(root)

            self.assertTrue((root / "broken").is_symlink())

    def test_ensure_mirror_leaves_remote_url_command_flow_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            clone_url = "https://example.test/workspace/repo.git"
            manager = GitManager(tmp)
            mirror = Path(tmp) / "repos" / "workspace" / "repo.git"
            completed = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")

            with patch("scout.gitops.subprocess.run", return_value=completed) as run:
                manager.ensure_mirror("workspace", "repo", clone_url)

            self.assertEqual(run.call_count, 1)
            self.assertEqual(
                run.call_args.args[0],
                [
                    "git",
                    "-c",
                    "filter.lfs.smudge=",
                    "-c",
                    "filter.lfs.required=false",
                    "clone",
                    "--mirror",
                    clone_url,
                    str(mirror),
                ],
            )

            mirror.mkdir(parents=True)
            with patch("scout.gitops.subprocess.run", return_value=completed) as run:
                manager.ensure_mirror("workspace", "repo", clone_url)

            self.assertEqual(run.call_count, 1)
            self.assertEqual(
                run.call_args.args[0],
                [
                    "git",
                    "-c",
                    "filter.lfs.smudge=",
                    "-c",
                    "filter.lfs.required=false",
                    "-C",
                    str(mirror),
                    "fetch",
                    "--prune",
                    "origin",
                ],
            )

    def test_validate_clone_url_uses_ls_remote_heads(self):
        manager = GitManager("/tmp/state")

        with patch.object(manager, "_git") as git:
            manager.validate_clone_url("git@bitbucket.org:ws/repo.git")

        git.assert_called_once_with(["ls-remote", "--heads", "git@bitbucket.org:ws/repo.git"])

    def test_ensure_mirror_refreshes_local_clone_before_fetching_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            upstream = tmp_path / "upstream.git"
            seed = tmp_path / "seed"
            source = tmp_path / "source"
            state = tmp_path / "state"

            self.run_git(["init", "--bare", str(upstream)])
            self.run_git(["init", str(seed)])
            self.run_git(["config", "user.email", "scout@example.test"], cwd=seed)
            self.run_git(["config", "user.name", "Scout Tests"], cwd=seed)
            (seed / "README.md").write_text("one\n", encoding="utf-8")
            self.run_git(["add", "README.md"], cwd=seed)
            self.run_git(["commit", "-m", "initial"], cwd=seed)
            self.run_git(["branch", "-M", "main"], cwd=seed)
            self.run_git(["remote", "add", "origin", str(upstream)], cwd=seed)
            self.run_git(["push", "origin", "main"], cwd=seed)
            self.run_git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=upstream)
            self.run_git(["clone", str(upstream), str(source)])

            manager = GitManager(str(state))
            mirror = manager.ensure_mirror("workspace", "repo", str(source))

            (seed / "README.md").write_text("one\ntwo\n", encoding="utf-8")
            self.run_git(["commit", "-am", "second"], cwd=seed)
            self.run_git(["push", "origin", "main"], cwd=seed)
            new_commit = self.git_output(["rev-parse", "HEAD"], cwd=seed)

            manager.ensure_mirror("workspace", "repo", str(source))

            self.run_git(["cat-file", "-e", "{}^{{commit}}".format(new_commit)], cwd=mirror)

    def test_ensure_mirror_serializes_concurrent_first_clone(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = GitManager(tmp)
            mirror = Path(tmp) / "repos" / "workspace" / "repo.git"
            calls = []
            errors = []
            active = 0
            max_active = 0
            state_lock = threading.Lock()
            start = threading.Barrier(3, timeout=5)

            def fake_git(args):
                nonlocal active, max_active
                with state_lock:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    time.sleep(0.05)
                    if args[:2] == ["clone", "--mirror"]:
                        mirror.mkdir(parents=True, exist_ok=True)
                    with state_lock:
                        calls.append(args)
                finally:
                    with state_lock:
                        active -= 1

            def ensure():
                try:
                    start.wait()
                    manager.ensure_mirror("workspace", "repo", "ssh://example.test/repo.git")
                except Exception as exc:
                    with state_lock:
                        errors.append(exc)

            with patch.object(manager, "_git", side_effect=fake_git):
                threads = [threading.Thread(target=ensure) for _ in range(2)]
                for thread in threads:
                    thread.start()
                start.wait()
                for thread in threads:
                    thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(max_active, 1)
            self.assertEqual(sum(1 for call in calls if call[:2] == ["clone", "--mirror"]), 1)
            self.assertEqual(
                sum(1 for call in calls if call[-3:] == ["fetch", "--prune", "origin"]),
                1,
            )

    def test_worktree_adds_are_serialized_per_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = GitManager(tmp)
            mirror = Path(tmp) / "repos" / "workspace" / "repo.git"
            mirror.mkdir(parents=True)
            active = 0
            max_active = 0
            errors = []
            state_lock = threading.Lock()
            start = threading.Barrier(3, timeout=5)

            def fake_git(args):
                nonlocal active, max_active
                with state_lock:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    time.sleep(0.05)
                finally:
                    with state_lock:
                        active -= 1

            def create(suffix):
                pr = type(
                    "PR",
                    (),
                    {
                        "workspace": "workspace",
                        "repo_slug": "repo",
                        "pr_id": 12,
                        "source_commit_hash": "{}bcdef1234567890".format(suffix),
                    },
                )()
                try:
                    start.wait()
                    manager.create_worktree(mirror, pr, suffix="job-{}".format(suffix))
                except Exception as exc:
                    with state_lock:
                        errors.append(exc)

            with patch.object(manager, "_git", side_effect=fake_git):
                threads = [threading.Thread(target=create, args=(suffix,)) for suffix in ("a", "b")]
                for thread in threads:
                    thread.start()
                start.wait()
                for thread in threads:
                    thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(max_active, 1)


if __name__ == "__main__":
    unittest.main()
