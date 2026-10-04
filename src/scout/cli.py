from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import CredentialStore, load_config
from .daemon import ScoutDaemon
from .diagnostic_logging import configure_diagnostic_logging
from .runtime_lock import RuntimeLock, RuntimeLockError
from .state import StateStore
from .usage import summarize_usage_log


def main(argv=None) -> int:
    handler = configure_diagnostic_logging()
    try:
        return _main(argv)
    except Exception:
        logging.getLogger(__name__).exception("Scout command failed")
        raise
    finally:
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()


def _main(argv=None) -> int:
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
    publication = parser.add_mutually_exclusive_group()
    publication.add_argument("--list-unresolved-publications", action="store_true", help="List blocked publication intents and failed rounds, with versions")
    publication.add_argument("--resolve-publication", metavar="ID", help="Record an operator decision about an unknown POST outcome")
    publication.add_argument("--retry-publication", metavar="ROUND_ID", help="Retry delivery of a saved selection plan")
    parser.add_argument("--expected-version", type=int)
    parser.add_argument("--outcome", choices=("published", "absent"))
    parser.add_argument("--comment-id", type=int)
    args = parser.parse_args(argv)
    publication_command = args.list_unresolved_publications or args.resolve_publication or args.retry_publication
    if publication_command and (args.once or args.check_config or args.check_startup or args.recover_abandoned_jobs or args.reset_state_db or args.usage_summary):
        parser.error("publication commands cannot be combined with daemon, check, or recovery commands")
    if args.resolve_publication:
        if args.expected_version is None or args.outcome is None:
            parser.error("--resolve-publication requires --expected-version and --outcome")
        if args.outcome == "published" and args.comment_id is None:
            parser.error("--outcome published requires --comment-id")
        if args.outcome == "absent" and args.comment_id is not None:
            parser.error("--comment-id is only valid with --outcome published")
    elif args.expected_version is not None or args.outcome is not None or args.comment_id is not None:
        parser.error("--expected-version, --outcome, and --comment-id require --resolve-publication")
    if args.reset_state_db and not args.once:
        parser.error("--reset-state-db requires --once")
    if args.reset_state_db and (
        args.check_config or args.check_startup or args.recover_abandoned_jobs
    ):
        parser.error("--reset-state-db cannot be combined with check or recovery commands")
    if (args.repo or args.pr) and not args.usage_summary:
        parser.error("--repo and --pr are only valid with --usage-summary")

    config = load_config(args.config)
    logging.getLogger().setLevel(getattr(logging, config.service.log_level.upper(), logging.INFO))
    if publication_command:
        state = StateStore(config.service.state_db)
        state.initialize()
        if args.list_unresolved_publications:
            print(json.dumps(state.inline.list_unresolved_publications(), indent=2, sort_keys=True))
            return 0
        if args.resolve_publication:
            changed = state.inline.resolve_publication(args.resolve_publication, args.expected_version, args.outcome, comment_id=args.comment_id)
        else:
            changed = state.inline.retry_publication(args.retry_publication)
        if not changed:
            print("publication unchanged: version changed or publication is not eligible for this operation", file=sys.stderr)
            return 1
        print("publication updated")
        return 0
    if args.check_config:
        print("configuration OK")
        return 0
    if args.usage_summary:
        _print_usage_summary(config.service.state_dir, repo=args.repo, pr=args.pr)
        return 0
    if args.check_startup:
        with RuntimeLock(config.service.state_dir):
            daemon = ScoutDaemon(config, CredentialStore())
            try:
                daemon.initialize()
            finally:
                daemon.close()
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
    if args.reset_state_db:
        try:
            with RuntimeLock(config.service.state_dir):
                _reset_state_db(config.service.state_db)
                daemon = ScoutDaemon(config, CredentialStore())
                try:
                    daemon.initialize()
                    daemon.poll_once()
                    daemon.run_pending_jobs()
                    daemon.cleanup_old_artifacts()
                finally:
                    daemon.close()
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
