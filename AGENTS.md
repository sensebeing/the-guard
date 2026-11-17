# Agent instructions

Read the engineering policy and module boundaries before changing code.

## Integration workflow

This repository uses GitHub PR mode (`review.integration_mode=github-pr`): commit changes on a
topic branch, push it to `origin`, open a GitHub PR with `gh pr create`, review it with
`guard review --github-pr sensebeing/the-guard#<number>` (the review is published as
`the-guard-reviewer[bot]`), and merge with `gh pr merge` rather than pushing directly to `main`.
The local `pre-push` hook rejects direct pushes to `main` in this mode.

## Architecture

The executable module map in `.guard/config.json` is authoritative. Foundation
(`config`, `policy`, `finding`, `git_helpers`) owns validation and read-only primitives.
Domain (`topology`, `review`) owns deterministic checks and snapshot review; topology
never imports review. Workflow (`init`, `distill`, `hooks`, `auto_update`) composes those
owners. Surface (`cli`, package exports) exposes workflows and is never imported below.
Every module declares its exact dependencies, role, layer and boundary document.
Update that map and `src/the_guard/README.md` together when responsibilities change.
`git_helpers` owns safe file/symlink snapshot reads and changed-line diffs; policy adapts
reader failures to policy diagnostics. Review CLI and push hooks run deterministic checks
on the target commit before provider execution or receipt acceptance. Contract drift owns
missing-module ratchets and rechecks existing sources when module constraints tighten.

## Engineering Quality Policy

<!-- the-guard-policy:start -->
```yaml
schema: the-guard-policy/v1
rules:
  - id: guard.architecture-simplicity
    severity: block
    scope: [backend, frontend, api, schema, shared, tool, skill, prompt, test]
    summary: Every responsibility, business state, and write decision has one explicit owner.
    details: docs/standards/rules/architecture-simplicity.md
    checker: static-plus-review
  - id: guard.single-source-of-truth
    severity: block
    scope: [backend, frontend, api, schema, shared, tool, skill, prompt, test]
    summary: Each business rule, state, vocabulary, classification and filter has one executable owner.
    details: docs/standards/rules/single-source-of-truth.md
    checker: static-plus-review
  - id: guard.service-boundaries
    severity: block
    scope: [backend, frontend, api, schema, shared, tool, skill, prompt, test]
    summary: Modules perform only the responsibilities and decisions declared in their owner documentation.
    details: docs/standards/rules/service-boundaries.md
    checker: static-plus-review
  - id: guard.code-organization
    severity: block
    scope: [backend, frontend, api, schema, shared, tool, skill, prompt, test]
    summary: Modules, files, classes and methods have clear cohesive responsibilities and names.
    details: docs/standards/rules/code-organization.md
    checker: static-plus-review
```
<!-- the-guard-policy:end -->
