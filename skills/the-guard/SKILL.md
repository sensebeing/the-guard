---
name: the-guard
description: Run the-guard static topology checks, independent pre-merge architecture review, evidence-backed rebuttals, and push receipt verification during daily development.
---

# Daily development with the-guard

Use this skill in an onboarded repository. For first-time installation or a change of onboarding
mode, use [the-guard-setup](../the-guard-setup/SKILL.md). Run commands in the target repository;
its policy and module contracts define the rules, not this skill.

## Daily loop

1. **Read before editing.** Read the root policy in `AGENTS.md` (or the manifest selected by
   `.guard/config.json`) and touched modules' owner documents. Trace affected producers and
   consumers so the change preserves the sole owner of each business decision.
2. **Check while editing.** Run `guard check --base origin/main` for the changed scope, or
   `guard check --file <path>` for focused feedback. Use the repository's actual base when it
   differs from `origin/main`. Fix policy and topology regressions before requesting review.
3. **Commit and review.** Commit the intended changes on the topic branch, then run
   `guard review` for an independent pre-merge architecture review of committed snapshots.
   A clean static check or successful `guard probe` is not an independent review receipt.
4. **Resolve or rebut.** Fix blocking findings at the architectural root cause across the affected
   pattern, preserving real safety boundaries. Commit fixes and rerun `guard review`. When the
   implementation already satisfies the contract, submit factual evidence with
   `guard review --note "<file:line and owner contract evidence>"` instead of changing correct
   code merely to appease a reviewer. A rebuttal requests reassessment; it does not bypass review.
5. **Verify integration readiness.** After a `GO` result, run `guard check-push` before integrating
   into `main`. If the committed patch changes, obtain matching review evidence again. A local
   receipt establishes review readiness, not permission to merge, push, or publish; follow the
   user's authorization and repository integration process.

## GitHub PR mode

When `.guard/config.json` sets `review.integration_mode` to `github-pr`, the main branch is
integrated through GitHub pull requests rather than direct pushes:

1. Commit on the topic branch, push it, and open a PR with `gh pr create`.
2. Run `guard review --github-pr <owner/repo#number>` to audit the committed diff and publish the
   attributable review as `the-guard-reviewer[bot]`. Resolve blocking findings or submit
   `--note` evidence and rerun until `GO`.
3. Merge with `gh pr merge`. In `github-pr` mode the local `pre-push` hook rejects direct pushes
   to the main branch; do not bypass it.

With `both`, the PR workflow is available for review and merge, while direct pushes to the main
branch remain gated by the local `GO` receipt check.

## Report the evidence

Report the checked scope, static result, committed revision, independent review verdict, and
`guard check-push` result separately. If a provider is unavailable or a command fails, report the
blocker without claiming a `GO` receipt. Preserve existing instructions and hooks; a local receipt
or `pre-push` hook is not server-side branch protection.

See the [workflow](../../README.md#workflow) and [CLI reference](../../README.md#topology-checks-and-cli-reference)
for command options, exit codes, and configuration.
