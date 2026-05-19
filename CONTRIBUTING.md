# Contributing to Scout

Thanks for your interest in Scout. This guide is written for humans **and** for
coding agents (Claude Code, Codex, etc.) that are likely to do much of the
actual editing. If you are an agent reading this: follow it literally; the
conventions matter and there are no hidden ones encoded only in tribal memory.

## Project shape

- Single Python daemon, no runtime dependencies outside the standard library
  except `tomli` on Python &lt; 3.11.
- Source lives under `src/scout/`. Tests live under `tests/` and use the stdlib
  `unittest` runner.
- `DESIGN.md` is the source of truth for behavior, invariants, and the
  rationale behind non-obvious choices. Read it before making changes that
  cross module boundaries.
- `README.md` is the user-facing entry point and must stay accurate after any
  user-visible change.

## Setup

```bash
git clone https://github.com/99dsimonp/Scout.git
cd Scout
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Scout targets Python 3.9+. No package install is strictly required for
development; `PYTHONPATH=src` is enough to run the module and the test suite.

## Running

One polling/review pass against the example config (will not contact Bitbucket
until credentials and a real repository are configured):

```bash
PYTHONPATH=src python3 -m scout --config config/config.toml.example --once
```

Validate config without contacting Bitbucket or any provider CLI:

```bash
PYTHONPATH=src python3 -m scout --config config/config.toml.example --check-config
```

Reset the SQLite state DB between manual test runs:

```bash
PYTHONPATH=src python3 -m scout --config config/config.toml.example --once --reset-state-db
```

## Tests

The full suite is the bar for every change:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

It should finish in well under a minute and exit cleanly. A change that
regresses a test is not ready to ship. If your change requires modifying an
existing test, say so explicitly in the PR description and explain why the old
assertion was wrong.

New behavior needs new tests. We prefer:

- Small, focused tests that exercise one branch of one function.
- Real temp directories and real SQLite (`state.db` is small and fast); avoid
  mocking the database.
- Subprocess and HTTP boundaries stubbed at the seams already used by existing
  tests (`tests/test_claude.py`, `tests/test_codex.py`, `tests/test_bitbucket.py`
  are good models).

If you are adding a feature gated by config, add a `tests/test_config.py` case
that proves the new key is parsed and validated.

## Style

- Standard-library Python. Do not add a runtime dependency without discussing
  it in an issue first; the dependency-free posture is deliberate.
- No formatter is enforced yet, but follow the existing layout: four-space
  indents, double-quoted strings, type hints on public functions, snake_case
  module and function names.
- Keep comments rare. Write a comment only when the *why* is non-obvious
  (hidden constraint, invariant, workaround for a specific external bug).
  Don't restate what the code does or reference the PR that introduced it.
- Prefer narrow, testable functions over clever ones. Scout is a daemon that
  needs to be debuggable from logs at 3 a.m.; readability beats elegance.

## Scope discipline

This is the part that matters most for agent contributors.

- **Do exactly what was asked.** Do not refactor surrounding code, do not
  rename variables for consistency, do not "clean up" unrelated files in the
  same PR. A bug fix is a bug fix.
- **No speculative abstractions.** Three similar lines is fine. Add an
  abstraction the third or fourth time the pattern *actually* repeats, not the
  first.
- **No defensive code for impossible states.** Validate at system boundaries
  (Bitbucket responses, config files, CLI output). Trust internal callers.
- **No backwards-compat shims for code that has not shipped.** If you are
  changing a function only used inside this repo, just change it.
- **Don't add features that were not requested.** Even helpful ones. Open an
  issue and discuss first.

If you find yourself wanting to do any of the above, stop, finish the
requested change in isolation, and propose the additional work as a separate
PR or issue.

## Commits and PRs

- One logical change per PR. If you cannot describe the PR in one sentence,
  split it.
- Commit messages: a short imperative subject (under ~70 chars), then a body
  explaining the *why* if it isn't obvious. Existing history (`git log`) is the
  reference for tone.
- PRs should include:
  - What changed and why (one paragraph is usually enough).
  - How it was tested. "Ran the full suite" is the minimum; for user-visible
    changes, describe the manual verification too.
  - Any DESIGN.md or README.md updates the change implies.
- Squash-merge friendly: assume your PR will land as a single commit.

## Security-sensitive areas

Some parts of the codebase deserve extra care. If your change touches any of
these, expect a slower review and write tests that pin the exact behavior:

- Credential loading (`scout.config`, systemd `LoadCredential=` plumbing).
- Subprocess invocation of provider CLIs (`scout.claude`, `scout.codex`,
  `scout.provider`). Argument construction, environment scrubbing, and
  timeouts are all load-bearing.
- Git operations on cloned repositories (`scout.gitops`). Worktrees must stay
  readonly and isolated.
- Anything that writes to Bitbucket (`scout.bitbucket`). A bug here is visible
  to every PR author.

See [SECURITY.md](SECURITY.md) for vulnerability disclosure.

## Reporting bugs and proposing features

- Bugs: open an issue with the Scout version, Python version, OS, the
  redacted config that triggered it, and the relevant log excerpt.
- Features: open an issue describing the use case before writing code. Scout
  has explicit non-goals (see `DESIGN.md` &sect; "Non-Goals for v1"); a feature
  that lands in one of those is unlikely to be accepted without discussion.

## License

By contributing, you agree your contributions are licensed under the Apache
License 2.0, the same license as the rest of the project. See
[LICENSE](LICENSE).
