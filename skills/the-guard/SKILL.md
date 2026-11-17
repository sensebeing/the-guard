---
name: the-guard
description: Run the-guard static topology checks, independent pre-merge architecture review, evidence-backed rebuttals, and push receipt verification during daily development.
---

# Daily development with the-guard

Use this skill in an onboarded repository. For first-time installation or a change of onboarding
mode, use [the-guard-setup](../the-guard-setup/SKILL.md). Run commands in the target repository;
its policy and module contracts define the rules, not this skill.

Checks, reviews, and `guard hook` workflows may schedule a non-blocking background update. A
clean newer `~/the-guard` checkout is delegated immediately and the installed uv tool is repaired
when its commit or package-tree digest is stale; inspection of the guard checkout remains
snapshot-bound. Review sessions start a fresh full review when reviewer contract code,
repository configuration, or policy rules change.

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

## Continuous boundary governance

Read the full module/layer map and each touched module's `role`, `dependencies` and
`boundary_docs`. Do not deepen host/plugin/adapter coupling in core modules merely because
legacy coupling exists. Owners may be directories or single files. Keep their declarations and
boundary docs synchronized with responsibility changes. Review reads root agent instructions,
`topology.architecture_docs` and detected root blueprints from the committed snapshot, alongside
touched boundaries; changes to these documents invalidate prior evidence.
Branch-wide `guard check` / `guard topology check` block newly broken document declarations and
stale dependencies while grandfathering untouched legacy declarations. Edit checks reject changed unregistered
production source under governance; protected pushes run static checks before exemptions/receipts.
Run `guard topology audit --json` to inspect whole-repository debt independently of changed-line
ratchets: it reports violations, missing documents, unregistered modules and stale dependencies.
A clean changed-scope check is not a clean whole-repository audit. `topology sync` preserves
curated dependencies when layers or enforcement are enabled; never use discovery to bless a
forbidden import. With both enabled, assign layers and nonempty roles to reported new modules before registering them.

Missing module paths fail audit; sync prunes deleted governed owners and incoming edges.
Branch checks ratchet newly nonexistent module declarations and missing roles in established
layer/dependency governance. Tightened constraints recheck unchanged sources in affected modules.
Review CLI and check-push run deterministic checks against the committed snapshot before providers
or receipts; protected pushes read that snapshot directly without temporary worktrees. Edit guard
ignores paths outside the repository and validates edited policy/configuration contracts.

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
