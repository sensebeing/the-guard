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
snapshot-bound. Changes to the review contract version or root review contract require a fresh
full review. Routine fixes and automatic upgrades preserve existing `GO` receipts when the
version and scoped repository contract match.
Updater state and reviewer discovery default to `~/.config/the-guard`. The updater migrates
legacy metadata on first default-path use and retains one rotated 512 KiB log backup.
Review prompts reach `claude` over standard input so prompt size is not bounded by the OS argv
limit (`agy -p` requires its prompt as the flag value). Snapshot Git reads run with
`-c core.fsmonitor=false`, so a wedged or cross-version worktree fsmonitor daemon cannot stall
them. Each run's `.meta.json` and `index.jsonl` record `full_review_reason` when the run is full:
`force`, `no_prior_session`, `contract_changed`, `no_incremental_delta`, `provider_changed`,
`missing_session_id` or `resume_failed`; incremental runs record an empty value.

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
   pattern, preserving real safety boundaries. Commit fixes and rerun plain `guard review`; the prior
   session resumes incrementally, which is several times faster than a cold run. Do not pass `--force`
   during iterative fix-and-re-review; reserve it for discarding a corrupted session or forcing an
   explicit cold re-audit. When the implementation already satisfies the contract, submit factual
   evidence with `guard review --note "<file:line and owner contract evidence>"` instead of changing
   correct code merely to appease a reviewer. A rebuttal requests reassessment; it does not bypass
   review.
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
Task blueprint discovery runs when `review.blueprint_globs` is configured. It selects matching
Markdown paths and excludes `review.operational_ledgers`, which defaults to `["CHANGELOG.md"]`.
Branch-wide `guard check` / `guard topology check` block newly broken document declarations and
stale dependencies while grandfathering untouched legacy declarations. Edit checks reject changed unregistered
production source under governance; protected pushes run static checks before exemptions/receipts.
Run `guard topology audit --json` to inspect whole-repository debt independently of changed-line
ratchets: it reports violations, missing documents, unregistered modules and stale dependencies.
A clean changed-scope check is not a clean whole-repository audit. `topology sync` preserves
curated dependencies when layers or enforcement are enabled; never use discovery to bless a
forbidden import. With both enabled, assign layers and nonempty roles to reported new modules before registering them.

Missing active module paths fail audit. Sync prunes deleted active owners and preserves paths
declared with `retired: true`. Retired paths allow deletions and block additions, modifications,
and inbound imports. Built-in lexical diagnostics apply to rule IDs installed in the target policy.
Branch checks ratchet newly nonexistent module declarations and missing roles in established
layer/dependency governance. Tightened constraints recheck unchanged sources in affected modules.
Review CLI and check-push run deterministic checks against the committed snapshot before providers
or receipts; protected pushes read that snapshot directly without temporary worktrees. Edit guard
ignores paths outside the repository and validates edited policy/configuration contracts.
In local-hook mode, the gate checks the pushed Git ref even if its commit differs from `HEAD`.
A matching `GO` receipt from a retained linked-worktree session can carry forward when the
contract digest and merge-base and patch checks pass.

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
