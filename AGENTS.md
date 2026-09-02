# AGENTS.md -- gallery-dl-wrapper

Agent workflow guide. Read this before touching anything.

---

## What this is

A tiny Python CLI wrapper around `gallery-dl` (installed as `gdw`) that keeps
downloads and state repo-local, groups target URLs by provider in
`sites.json`, and supports a `--dry-run` preview mode. See `README.md` for
full configuration and usage examples.

## Commands

```bash
poetry install
poetry run gdw --dry-run              # preview what would run, no downloads
poetry run gdw                        # run everything configured
poetry run gdw --provider twitter     # run one provider's list
```

## Development

```bash
poetry install --with dev
poetry run pytest       # tests
```

No `tests/` directory exists yet, so `pytest` currently collects zero tests
and exits with status 5 -- that's expected until the first test module
lands, not a broken environment. There is no CI job running `pytest` for
this repo yet either.

---

## GitHub operations go through repo-scaffold

This repo is managed by [repo-scaffold](https://github.com/blairg23/repo-scaffold). Never call
`gh` CLI directly for issues, PRs, branches, or project boards -- use
`poetry run repo-scaffold <command>` from the repo-scaffold checkout.

## Branch naming

Format: `type/NNN-short-description`

- `type`: `feat`, `fix`, `docs`, `chore`, `refactor`, `test`
- `NNN`: the GitHub issue number -- create the issue first if one does not exist
- `short-description`: kebab-case, 3-4 words max

Examples: `feat/22-provider-progress-bar`, `fix/31-cookie-path-resolution`

`main` is the only long-lived branch. Never reuse a branch after its PR has merged.

---

## PR titles

Format: `type(scope): description (#NNN)`

The issue number at the end is required so the PR is immediately traceable to its ticket.

---

## Workflow rules

- Create a GitHub issue before starting work so you have the `NNN` for the branch name.
- Always use the PR template (`.github/pull_request_template.md`) -- no freeform bodies.
- Always use the issue template (`.github/issue_template.md`).
- Never merge or close PRs -- push the branch, open the PR, stop there.
- Never push new commits to a branch whose PR is already merged -- cut a fresh branch from main.

---

## Review comment SOP (all four steps, in order, non-negotiable)

`check-sop` enforces this automatically and fails the PR until every review
thread satisfies it. For every review thread on a PR you are working on,
complete ALL of the following:

**Step 1 -- Push the fix.** Make the code change, commit, push. Note the commit hash.

**Step 2 -- Reply to the thread** with the commit hash and a one-sentence explanation:
```bash
poetry run repo-scaffold pr comment --repo blairg23/gallery-dl-wrapper --pr-number N \
  --body "Fixed in <hash>. <one sentence what changed>." \
  --reply-to COMMENT_ID
```

**Step 3 -- Resolve the thread:**
```bash
poetry run repo-scaffold pr resolve-thread --repo blairg23/gallery-dl-wrapper --thread-id THREAD_ID
```

**Step 4 -- React +1 to the original reviewer comment:**
```bash
poetry run repo-scaffold pr react --repo blairg23/gallery-dl-wrapper --comment-id COMMENT_ID --reaction "+1"
```

A thread is **NOT done** until all four steps are complete (fix, reply, resolve, react).

To get THREAD_ID and COMMENT_ID (databaseId of the first comment in each thread):
```bash
poetry run repo-scaffold pr review-threads --repo blairg23/gallery-dl-wrapper --pr-number N --json
```

To verify all threads are SOP-compliant before declaring work done:
```bash
poetry run repo-scaffold pr check-sop --repo blairg23/gallery-dl-wrapper --pr-number N
```

**The `check-sop` status check doesn't always refresh itself:** replying (step 2) is a
new review comment and re-triggers it automatically, but resolving (step 3) and
reacting (step 4) have no corresponding Actions trigger event. If it's still
failed/stale after all four steps, refresh it manually:
```bash
poetry run repo-scaffold pr rerun --repo blairg23/gallery-dl-wrapper --pr-number N --failed-only
```

---

## Git identity

Before your first commit, confirm `git config user.name` and `git config user.email` are
set to real values (not `Your Name` / `you@example.com`). If they are placeholders, stop
and ask the user to configure them before continuing.

---

## Commit messages

Format: subject line (imperative mood) + blank line + body.

- Subject: 50 chars max, no trailing period
- Body: explain WHY the change is needed, not what it does (the diff shows what)
- No one-liner commits for non-trivial changes
