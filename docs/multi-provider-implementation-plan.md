# Multi-provider inline review implementation

Implement the approved design in `multi-provider-inline-review-design.md`.
Healthy queueing may wait; an unavailable reviewer or selector must not prevent
successful reviewers from producing feedback.

1. Add real SQLite coverage for rounds, the provider barrier, immutable results,
   selection plans, recoverable publication intents, and operator recovery.
2. Implement exact and model-assisted coverage selection, bounded structured
   inputs, conservative fallback, and validated configuration.
3. Implement publication, trusted identities, eligible history, snapshot checks,
   and partial-coverage/clean/stale notices without rerunning reviews on retries.
4. Integrate rounds with shared-worker scheduling, nonblocking provider limits,
   fixed recovery deadlines, request/push handling, and correct PR pruning.
5. Run targeted tests and the full suite; independently review the integrated
   change, address findings, and update README/DESIGN/configuration examples.

Ownership: state and state tests; selector/config and their tests;
publisher/Bitbucket/CLI and their tests; daemon/git snapshot integration and its
tests. The root task coordinates APIs, documentation, integration verification,
and the final read-only review. All code work uses the isolated worktree.

No live Bitbucket writes, provider calls, network package installation, deployment,
or push is part of this task. Live Bitbucket contract verification remains an
explicit release limitation unless existing response fixtures establish it.

## Verification

- Baseline full unittest suite: 319 tests passed (Python 3.14).
- Container support: local Rocky Linux Python 3.9 image, networking disabled;
  stream the installed vendored TOML parser with symlinks dereferenced (no install).
- Targeted component tests: pending.
- Integrated full suite and static config validation: pending.
- Independent review: pending.
