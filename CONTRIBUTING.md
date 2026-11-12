# Contributing to the-guard

Contributions that make architecture guardrails clearer, simpler, and more reliable are welcome.
For a substantial behavior change, open an issue describing the invariant, owner, and observable
outcome before implementation. Report vulnerabilities privately using [SECURITY.md](SECURITY.md).

## Set up

Use Python 3.11+, Git, and [uv](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/sensebeing/the-guard.git
cd the-guard
uv sync
git switch -c your-topic
```

The CLI has no external runtime dependencies. Independent review additionally needs a locally
installed and authenticated `codex`, `claude`, or `agy`; inspect availability with `uv run guard probe`.

## Develop and verify

Read [AGENTS.md](AGENTS.md) and the [package boundaries](src/the_guard/README.md) before editing.
We dogfood the core policy: one owner for each decision, cohesive modules, and root-cause fixes.
Keep changes focused, preserve trust-boundary checks, and add or extend tests for observable behavior.
Keep [README.md](README.md) and [README.zh-CN.md](README.zh-CN.md) aligned when changing public usage.

```sh
uv run python -m unittest discover -s tests -v
uv run guard check --base origin/main
```

Commit on your topic branch and review before submission:

```sh
uv run guard review
uv run guard check-push
```

Resolve blocking findings at their architectural root, or supply code and owner-contract evidence
with `uv run guard review --note "<file:line and owner contract evidence>"`. Commit fixes and review
again. Static checks, provider discovery, and an actual `GO` receipt are distinct evidence.
See the [daily skill](skills/the-guard/SKILL.md) for the complete loop.

## Submit a pull request

This repository uses GitHub PR mode (`review.integration_mode=github-pr`): changes must be
committed on a topic branch, pushed to `origin`, and merged through GitHub rather than pushed
directly to `main`. Open the PR and publish the independent review to it:

```sh
gh pr create --fill
uv run guard review --github-pr sensebeing/the-guard#<number>
```

The review is published as `the-guard-reviewer[bot]` through the repository's GitHub App. Describe
the concrete problem, resulting behavior, and verification results. Disclose checks that
could not run and unresolved findings. Never include credentials or private reviewer inputs/logs.
Merge the reviewed PR with `gh pr merge`; the local `pre-push` hook rejects direct pushes to `main`
in this mode.
