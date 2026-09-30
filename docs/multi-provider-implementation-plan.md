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
- Final host suite: 451 tests passed on Python 3.14.
- Final network-disabled Docker suite: 451 tests ran on Python 3.9.25,
  450 passed and one existing non-root setup-script test skipped because the
  container runs as root. The example configuration check passed on both runtimes.
- Container workaround: the existing local image lacks `tomli`; streamed the
  installed vendored parser with symlinks dereferenced. No packages downloaded
  or installed.
- Independent read-only review completed. Regression tests cover the concrete
  findings: closure fencing, ambiguous POST freshness, notice eligibility,
  provider-slot reuse during HTTP publication, and cross-batch identity halting.
  Final bounded rechecks also approved legacy snapshot retry/backoff, replay
  without provider reservations, and rejection of malformed PR inventories.
- `git diff --check` passed. Legacy tests expecting immediate per-provider inline
  publication were replaced with real-SQLite round integration tests because
  publication now waits for all providers to reach a final outcome.
- Live Bitbucket marker/anchor/identity contracts and installed provider CLI
  behavior were not exercised; HTTP and process-boundary tests use stubs.

## Upstream integration

Merged `origin/main` at `19dee02` after the user reported new commits. Preserve
its report publication snapshots, comment progress, and PR discussion context.
Pre-existing saved inline snapshots finish their original replay path; new
inline work uses rounds.
