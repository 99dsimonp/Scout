# Multi-provider inline review implementation

Implement the approved design in `multi-provider-inline-review-design.md`.
Healthy queueing may wait; an unavailable reviewer or selector must not prevent
successful reviewers from producing feedback.

1. Add real SQLite coverage for rounds, the provider barrier, immutable results,
   selection plans, recoverable publication intents, and operator recovery.
2. Implement exact and model-assisted coverage selection, bounded structured
   inputs, conservative fallback, and validated configuration.
3. Implement publication, trusted identities, eligible history, lifecycle checks,
   and partial-coverage/clean notices without rerunning reviews on retries.
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

## Review corrections

1. Add failing regression tests for OLD-side anchors with a null `to`, disabled
   providers during cooldown recovery, comment lookups that cross the settling
   deadline, saved publications whose provider was removed, and exhausted POST
   retries that never leave `publishing`.
2. Fix the owning history, recovery, and publication paths. Preserve frozen
   results and selection plans; a publication retry must not rerun reviews.
3. Run the targeted tests and full suite, validate the example configuration,
   and obtain an independent read-only review before reporting completion.

The implementer owns the source and regression tests. The root session owns
documentation and final validation; the reviewer makes no edits.

Completed all five corrections. Removed-provider replay also retains the existing
lease cushion when `job_timeout_seconds = 1`; otherwise the local expiry check
immediately requeues the saved publication without sending it.

- Nine targeted regression tests failed before the fixes and passed afterward.
  Coverage includes both publication modes, legacy OLD-side history, fixed
  provider recovery deadlines, corrupt snapshots, and operator resolution/retry.
- Final host suite: 465 tests passed on Python 3.14.5.
- Final offline Docker suite: 465 tests ran on Python 3.9.25, with 464 passing
  and the existing non-root setup-script test skipped because the container
  runs as root. Example configuration validation passed on both runtimes.
- The first container image lacked Git and `tomli`. Used an existing local
  image with Git and streamed the already-installed vendored `tomli`; no
  downloads or package installation were needed. Container networking was off.
- Independent read-only review found no remaining issues, including a final
  check of short leases and replaced-token fencing. `git diff --check` passed.
- Live Bitbucket and provider CLI contracts remain untested.

## Finish the reviewed snapshot

The user changed the source-push policy: finish the existing review against its
frozen snapshot, even if a newer revision appears. Do not discard or automatically
restart the round on a push, and remove stale-notice support. Explicit review
requests still start a new round. PR closure, draft status, repository eligibility,
publication leases, and uncertain POST recovery still control whether Scout may
send comments.

1. Add regression tests for pushes during review, selection, delivery, and retry;
   require the original result and locations to finish without a notice or rerun.
2. Remove source-movement replacement and stale-publication branches from the
   inline workflow, preserving the earlier review fixes and lifecycle checks.
3. Update the documented contract, run targeted and full host/offline Docker
   checks, and have a read-only reviewer check the completed change.

Completed the snapshot policy change, including legacy saved inline publications.
Explicit requests atomically supersede unfinished inline jobs across provider,
policy, and schema identities, so a removed provider cannot publish an older
review after a fresh request. Reports and completed history remain intact.

- Ten snapshot-policy regressions failed before the change and passed afterward.
  Three additional regressions reproduced legacy supersession failures before
  the fix; coverage also verifies rollback if fresh-round creation fails.
- Final host suite: 470 tests passed on Python 3.14.5.
- Final offline Docker suite: 470 tests ran on Python 3.9.25, with 469 passing
  and the existing non-root setup-script test skipped because the container
  runs as root. Example configuration validation passed on both runtimes.
- Independent read-only review found no remaining actionable findings.
  `git diff --check` passed.
- The current Bitbucket adapter does not bind comments to the reviewed commit.
  A mid-review push can leave the original locations pointing at changed code;
  live acceptance of those anchors and provider CLI contracts remain untested.
