---
name: the-guard-setup
description: Onboard a repository to the-guard engineering policies, architecture topology checks and independent pre-merge agent review, using a preset or codebase-distilled rules.
---

# Set up the-guard

Align the coding agent before implementation, then enforce independent review at integration.
Work in the user's chosen repository; do not push, publish, or replace existing hooks implicitly.
For day-to-day checks, reviews, and rebuttals after onboarding, use [the-guard](../the-guard/SKILL.md).

## Choose the standard

Inspect existing agent instructions, policy manifests, module owner documents and hook configuration.
If the user has not already chosen, ask which mode they want:

1. **Core (recommended)** — four universal architecture and organization rules.
2. **Full** — fourteen rules spanning data, execution, tools, agents, UI and tests.
3. **Distill** — derive tailored rules and topology from current code and owner contracts.
4. **Hybrid** — core rules plus codebase-distilled topology and domain constraints.

Use [preset-catalog.md](references/preset-catalog.md) to explain tradeoffs. For distill or hybrid,
read [distillation-guide.md](references/distillation-guide.md). Do not treat existing technical debt
as an approved standard or invent module ownership from directory names.

## Install and align

Check Python >= 3.11 and `guard --help`. If the CLI is not installed, use the project's supported
local installation (`python3 -m pip install /path/to/the-guard`) or its `bin/guard` launcher.
Installation has no external runtime dependencies; independent review also needs Git and a locally
installed/authenticated `codex`, `claude` or `agy` CLI.

For core/full, run the selected preset:

```sh
guard init --repo-root . --preset core --manifest-file AGENTS.md
guard topology sync --repo-root .
guard check --repo-root . --policy-only
```

`init` preserves surrounding instructions and refuses to overwrite customized rules. It creates
`CLAUDE.md -> AGENTS.md` only if that name is free. For an existing policy, configure its manifest
and detail paths instead of generating a competing manifest. `.guard/config.json` owns topology;
the policy manifest and rule documents own semantics. Read the [configuration reference](../../README.md#configuration)
when customizing them.

Have the user review genuinely new domain constraints before making them blocking. Existing authorization
covers routine setup choices. Record the chosen rules and module boundaries in the target repository's
agent instructions so future agents read them before coding.

## Wire the integration boundary

If hook installation is authorized, `guard init --with-hooks` installs Git's `pre-push` hook.
It refuses to replace an existing hook. If one exists, integrate `guard hook main-push-gate`
into that hook while preserving its other checks and passing Git's complete stdin update list.
Do not consume stdin twice. For agent post-edit hooks, invoke `guard hook edit-guard --repo-root .`
with JSON containing `tool_input.file_path`; this checks the file after an edit, not a proposed edit.

Run `guard probe` to inspect available review providers. Before merging or pushing a protected
change, commit it on its topic branch, run `guard review`, and address all concrete blocking findings.
Use `guard review --note "<code locations, owner contracts and invariant evidence>"` for a supported
rebuttal. Do not blindly alter correct code to satisfy a reviewer. Re-run until GO, then integrate.
Receipts are local evidence, not remote branch protection; CI/server enforcement requires the team's
own trusted execution boundary. Do not claim a real provider review from unit tests or a probe.

## Verify and report

Validate policy and topology on the actual changed scope (`guard check --base <base>`).
Report the chosen mode, files installed, discovered/confirmed owners, hook integration and verification.
Distinguish successful static checks, provider availability and an actual independent review receipt.
