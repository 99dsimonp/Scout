from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import CredentialStore, load_config
from .daemon import ScoutDaemon
from .runtime_lock import RuntimeLock, RuntimeLockError
from .state import StateStore
from .usage import summarize_usage_log


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Scout Bitbucket PR review daemon")
    parser.add_argument("--config", default="/etc/scout/config.toml", help="Path to config.toml")
    parser.add_argument("--once", action="store_true", help="Run one poll/review pass and exit")
    parser.add_argument("--check-config", action="store_true", help="Validate static config and exit")
    parser.add_argument(
        "--check-startup",
        action="store_true",
        help="Validate config, credentials, repository access, and provider startup checks",
    )
    parser.add_argument(
        "--recover-abandoned-jobs",
        action="store_true",
        help="Return active jobs left by a stopped Scout process to the queue and exit",
    )
    parser.add_argument(
        "--reset-state-db",
        action="store_true",
        help="Delete the configured SQLite state database before an explicit --once test run.",
    )
    parser.add_argument(
        "--usage-summary",
        action="store_true",
        help="Print provider token usage grouped by PR and provider, sorted by total tokens.",
    )
    parser.add_argument("--repo", help="Limit --usage-summary to one repository slug")
    parser.add_argument("--pr", type=int, help="Limit --usage-summary to one pull request id")
    args = parser.parse_args(argv)
    if args.reset_state_db and not args.once:
        parser.error("--reset-state-db requires --once")
    if args.reset_state_db and (
        args.check_config or args.check_startup or args.recover_abandoned_jobs
    ):
        parser.error("--reset-state-db cannot be combined with check or recovery commands")
    if (args.repo or args.pr) and not args.usage_summary:
        parser.error("--repo and --pr are only valid with --usage-summary")

    config = load_config(args.config)
    if args.check_config:
        print("configuration OK")
        return 0
    if args.usage_summary:
        _print_usage_summary(config.service.state_dir, repo=args.repo, pr=args.pr)
        return 0
    if args.check_startup:
        with RuntimeLock(config.service.state_dir):
            daemon = ScoutDaemon(config, CredentialStore())
            daemon.initialize()
        print("startup checks OK")
        return 0
    if args.recover_abandoned_jobs:
        try:
            with RuntimeLock(config.service.state_dir):
                state = StateStore(config.service.state_db)
                state.initialize()
                recovered = state.recover_abandoned_jobs(
                    "Scout service stopped while this job was active"
                )
        except RuntimeLockError as exc:
            print("recovery skipped: {}".format(exc))
            return 0
        print("recovered abandoned jobs: {}".format(recovered))
        return 0
    logging.basicConfig(
        level=getattr(logging, config.service.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.reset_state_db:
        try:
            with RuntimeLock(config.service.state_dir):
                _reset_state_db(config.service.state_db)
                daemon = ScoutDaemon(config, CredentialStore())
                daemon.initialize()
                daemon.poll_once()
                daemon.run_pending_jobs()
                daemon.cleanup_old_artifacts()
        except RuntimeLockError as exc:
            print("reset-state-db refused: {}".format(exc), file=sys.stderr)
            return 1
    elif args.once:
        daemon = ScoutDaemon(config, CredentialStore())
        daemon.run_once()
    else:
        daemon = ScoutDaemon(config, CredentialStore())
        daemon.run_forever()
    return 0


def _reset_state_db(path: str) -> None:
    db_path = Path(path)
    for candidate in (
        db_path,
        Path(str(db_path) + "-wal"),
        Path(str(db_path) + "-shm"),
        Path(str(db_path) + "-journal"),
    ):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _print_usage_summary(state_dir: str, repo=None, pr=None) -> None:
    rows = summarize_usage_log(Path(state_dir) / "provider-usage.jsonl", repo=repo, pr=pr)
    if not rows:
        print("No provider usage records found.")
        return
    header = (
        "repo",
        "pr",
        "provider",
        "runs",
        "total_tokens",
        "input",
        "output",
        "cache_create",
        "cache_read",
        "cost_usd",
    )
    print(
        "{:<18} {:>7} {:<8} {:>4} {:>14} {:>10} {:>10} {:>13} {:>12} {:>10}".format(
            *header
        )
    )
    for row in rows:
        print(
            "{:<18} {:>7} {:<8} {:>4} {:>14} {:>10} {:>10} {:>13} {:>12} {:>10.4f}".format(
                str(row["repo"]),
                str(row["pr"]),
                str(row["provider"]),
                row["runs"],
                row["total_tokens"],
                row["input_tokens"],
                row["output_tokens"],
                row["cache_creation_input_tokens"],
                row["cache_read_input_tokens"],
                row["cost_usd"],
            )
        )


if __name__ == "__main__":
    sys.exit(main())
