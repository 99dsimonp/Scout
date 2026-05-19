# Security Policy

Scout is a daemon that holds Bitbucket Cloud credentials, shells out to
agentic AI CLIs, and reads source code from the repositories it reviews. Bugs
in any of those paths can leak credentials, leak private source code, or let
an attacker influence what an AI reviewer says about a pull request. We take
reports in those areas seriously.

## Supported versions

Scout is pre-1.0. Only the latest released version on the `main` branch
receives security fixes. If you are running an older tag, the first step in
any response will be to ask you to reproduce on the current `main`.

## Reporting a vulnerability

**Please do not open a public GitHub issue for a suspected vulnerability.**

Use GitHub's private vulnerability reporting:

1. Go to <https://github.com/99dsimonp/Scout/security/advisories/new>.
2. Describe the issue, the affected version or commit, and the impact.
3. Include a minimal reproduction if you have one. A config snippet (with
   credentials redacted) plus the exact command and observed behavior is
   ideal.

If GitHub's private reporting is unavailable for you, open a minimal public
issue that says only "requesting a private security contact" and a maintainer
will reach out. Do not include vulnerability details in that issue.

We aim to acknowledge reports within a few business days. Fix timelines depend
on severity and complexity; for high-severity issues we will prioritize a
patched release and a coordinated disclosure window with the reporter.

## Scope

In scope &mdash; please report:

- Credential leakage: Bitbucket username, API key, SSH key, or provider API
  keys ending up in logs, error messages, the SQLite state DB, the
  `review-log.jsonl`, the `provider-usage.jsonl`, or any file under
  `state_dir/runs/`.
- Privilege escalation from the `scout` system user.
- Anything that lets an unprivileged user on the host read Scout's secrets
  directory, state directory, or systemd credentials.
- Command injection or argument injection into the provider CLI invocations,
  `git` calls, or any other subprocess Scout launches.
- Path traversal in worktree handling, config loading, or schema loading.
- A malicious PR (branch name, commit message, file contents, PR description)
  causing Scout to:
  - execute attacker-controlled code outside the provider CLI's documented
    sandbox,
  - post Bitbucket reports or comments outside the PR being reviewed,
  - exfiltrate other repositories' content,
  - or bypass the readonly-worktree guarantees.
- SQL injection or unsafe deserialization anywhere in the state layer.
- Bypasses of the configured tool allowlist that Scout passes to provider CLIs
  (Scout restricts Claude to read-only tools; a way to make Claude run shell
  or write files via Scout's invocation is in scope).
- TLS verification bypasses or downgrade issues in the Bitbucket client.

Out of scope:

- Vulnerabilities in dependencies that have an upstream advisory but no Scout-
  specific exposure. Report those to the upstream project.
- Vulnerabilities in the provider CLIs themselves (`claude`, `codex`) or in
  Bitbucket Cloud. Report those to their vendors.
- Issues that require an attacker who already has root on the host running
  Scout, or who already has write access to `/etc/scout/` or the state
  directory.
- Issues that require an attacker with write access to Scout's configured
  Bitbucket workspace already having permissions equivalent to a maintainer
  on the reviewed repositories. Scout assumes the reviewed Bitbucket workspace
  and repositories are trusted by the operator (see `DESIGN.md` &sect;
  "Non-Goals for v1").
- Denial of service through legitimate API rate limits, provider usage caps,
  or expected Bitbucket throttling.
- Missing security hardening that is documented as a non-goal (for example,
  full hostile-repository sandboxing).
- Theoretical issues without a working proof of concept against the current
  `main` branch.

## What to include in a good report

- The Scout commit you reproduced on.
- The OS and Python version.
- A redacted config (`config.toml`) showing only the keys relevant to the
  issue.
- The exact steps to reproduce, including any malicious-input payload.
- The observed effect and the expected effect.
- If you have a suggested fix, include it &mdash; but a clean reproduction is
  more valuable than a speculative patch.

## Handling of reports

- We will keep your report private until a fix is available.
- We will credit reporters in the release notes for the fixed version unless
  you ask us not to.
- We do not currently run a paid bug bounty. We are happy to coordinate CVE
  assignment for qualifying issues.

## Operator-side hardening (not a vulnerability, but worth knowing)

If you operate Scout, the following reduce blast radius and should be in place
before exposing the daemon to live PR traffic:

- Run as the dedicated `scout` user (the default in the packaged unit), not as
  your login user, unless a provider CLI specifically requires it.
- Store Bitbucket and provider credentials via systemd `LoadCredential=`, not
  as plain environment variables and not in `config.toml`.
- Treat the Bitbucket SSH deploy key as read-only on the Bitbucket side.
- Restrict access to `/etc/scout/`, `/var/lib/scout/`, and `/var/log/scout/`
  to the `scout` user.
- Keep `service.retention_days` at its default (7) unless you have a specific
  reason to retain raw provider output longer; raw output may contain code
  excerpts from the reviewed repository.

These are operational recommendations, not the basis of a vulnerability
report.
