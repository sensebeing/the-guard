# the-guard

Engineering rules before code. Independent architecture review before merge.

`the-guard` is a small, configurable Python CLI with **zero external runtime dependencies**.
It puts policy and module boundaries in front of coding agents, checks changed code for concrete
topology regressions, and runs an independent read-only agent review of committed changes.

[中文](README.zh-CN.md) · [Setup skill](skills/the-guard-setup/SKILL.md)

## Quick start

Requires Python 3.11+ and Git. Install from a checkout:

```sh
python3 -m pip install /path/to/the-guard
# Or run /path/to/the-guard/bin/guard directly.
cd /path/to/your-project
guard init --preset core
guard topology sync
guard check --policy-only
guard check --base origin/main
```

Choose `--preset full` for all fourteen universal rules. `guard init --with-hooks` optionally
installs a Git `pre-push` hook and refuses to replace an existing hook. Initialization preserves
surrounding agent instructions and creates `CLAUDE.md -> AGENTS.md` only when safe.
Customized rule files are never silently overwritten.

The [setup skill](skills/the-guard-setup/SKILL.md) offers core, full, codebase distillation and hybrid
onboarding. Register its directory with your coding agent's skill loader.

## Workflow

1. Read the root policy and touched modules' boundary documents before implementing.
2. Run deterministic checks on changed files: `guard check --base origin/main`.
3. Commit the topic branch. Run `guard review` before merging/pushing the protected branch.
4. Fix concrete blockers across the affected pattern, or submit evidence with
   `guard review --note "<code locations and owner-contract evidence>"`.
5. Run `guard check-push`, then integrate through your team's normal lane.

Review providers are local `codex`, `claude`, and `agy` CLIs, in that order. They must already be
installed and authenticated. `guard probe` reports availability; it does not perform a review.

```sh
guard probe --priority-order codex,claude,agy
guard probe --provider-model codex=YOUR_MODEL:high
guard review --provider codex
guard review run --force
guard review check-push
```

Full reviews read every scope batch before reporting all high-signal blockers. Follow-up sessions
resume verified conversations and inspect prior unresolved findings plus new deltas. If continuation
cannot be verified, a fresh full review runs. Rebuttals are decided from code and contracts, not
by requiring arbitrary edits. Prompts bind all reads to Git snapshots, including module documents.
No tests, builds, writes or delegated agents are requested inside review sessions.

Review receipts use Git stable patch IDs. Identical patches survive rebase; carry-forward additionally
checks each changed file's patch when upstream context changes. Receipts, sessions and bounded logs live under the
Git common directory in `the-guard-receipts/`, `the-guard-sessions/` and `the-guard-logs/`,
using the `the-guard-receipt/v1`, `the-guard-session/v1` and `the-guard-log-meta/v1` schemas.

Receipts are trusted local workflow evidence, not cryptographic attestations or server-side protection.
Review audits committed snapshots; uncommitted changes are not included. The pre-push hook checks the
actual protected ref's old/new commit range and rejects a pushed commit different from local HEAD.
Creating a protected remote branch requires an explicit baseline before enabling this hook.

## Configuration

`.guard/config.json` is optional. With no configuration, the CLI reads the canonical policy block in `AGENTS.md`. Policy validation deliberately supports a small YAML subset: scalar fields and inline
scope lists, two-space list indentation, no YAML dependencies or implicit type coercion.

```json
{
  "manifest_file": "AGENTS.md",
  "manifest_tag": "the-guard-policy",
  "manifest_schema": "the-guard-policy/v1",
  "detail_dir": "docs/standards/rules",
  "detail_manifest_ref": "AGENTS.md#engineering-quality-policy",
  "rule_id_prefix": "guard.",
  "exempt_tag": "guard-exempt",
  "main_branch": "main",
  "module_doc_roots": ["services", "packages", "apps", "modules", "src"],
  "review": {
    "project_title": "My project",
    "reviewable_extensions": [".py", ".swift", ".ts", ".tsx", ".js", ".go", ".rs"],
    "reviewable_prefixes": ["infra/"],
    "extra_reviewable_paths": ["docs/architecture/**"],
    "service_config_roots": ["services", "packages", "apps", "src"]
  },
  "topology": {
    "python_prod_prefixes": ["src/", "services/", "packages/", "app/", "apps/"],
    "swift_prod_prefixes": [""],
    "modules": [],
    "forbidden_imports": [{"from": "src/ui/", "to": "internal.storage"}],
    "exclusive_sql_owners": [{"table": "jobs", "owners": ["src/storage/jobs.py"], "rule_id": "guard.single-source-of-truth", "message": "Use the jobs owner API."}],
    "retired_tables": [{"scope_prefixes": ["src/"], "tables": ["old_jobs"], "rule_id": "guard.data-modeling"}],
    "forbidden_patterns": [{"scope_prefixes": ["src/ui/"], "literal_or_regex": "/private-owner-state", "literal": true, "rule_id": "guard.service-boundaries", "message": "Consume the public owner projection."}]
  }
}
```

`module_boundaries` accepts the same entries as `forbidden_imports`. Either may use `scope_prefixes`
and `imports` arrays instead of `from`/`to`. Production scope entries accept prefixes or globs.
A retired-table rule may be a single object or an array. `literal_or_regex` is a Python regex unless
`literal: true` is set. A line exemption has the form
`# guard-exempt guard.architecture-simplicity: concrete reason here` (at least eight characters).
Manifest tag, schema, rule prefix and exemption tag are configured in `.guard/config.json`.
Without an explicit detail reference, the configured manifest file uses `#engineering-quality-policy`.

A manifest entry looks like this (inside `<!-- the-guard-policy:start -->` / `<!-- the-guard-policy:end -->`):

```yaml
schema: the-guard-policy/v1
rules:
  - id: guard.service-boundaries
    severity: block
    scope: [backend, frontend]
    summary: Each module owns only its declared responsibilities.
    details: docs/standards/rules/service-boundaries.md
    checker: static-plus-review
```

Each detail file has `id`, `title`, `manifest` frontmatter and a nonempty Markdown body. Paths must
remain inside the repository. `.guard/config.json`, root agent instructions, policy directories,
code and skills/prompts are reviewable by default; ordinary prose and lock-only changes are exempt.

Provider settings use the `THE_GUARD_REVIEW_` prefix with these suffixes: `CONFIG`, `PROVIDER`,
`MODEL`, `EFFORT`, `STALL_SECONDS`, `HEARTBEAT_SECONDS`, `MAX_SECONDS`, `LOG_DIR`, and `ACTIVE`.
For example, set `THE_GUARD_REVIEW_MODEL` to choose a model. `ACTIVE=1` marks an independent
reviewer session and suppresses recursive CLI check-push invocation; do not set it in developer shells. Provider discovery is persisted in
`~/.config/the-guard/review-agent.json`. Override that location with `THE_GUARD_REVIEW_CONFIG`.

## Topology and distillation

```sh
guard distill --repo-root . --json
guard topology sync --repo-root .
guard topology check --base origin/main
guard check --file src/api.py --file app/Screen.swift --json
```

Discovery inventories modules, languages, owner docs and internal dependencies. Sync updates
only the discovered module inventory, preserving declared constraints. Distill adds tooling and bounded
code samples for an agent to interpret; it does not invent or install domain rules automatically.

Python AST checks detect write locks in read functions/endpoints, duplicate awaited reads on an
execution path without an intervening write, and single-entity awaited reads in loops/comprehensions.
Changed-line plus base-function-signature comparison avoids flagging unchanged legacy violations.
Swift checks are lexical, not a Swift compiler: they detect duplicate awaited reads.
Configured checks enforce import boundaries, exclusive SQL owners, retired-table net-deletion ratchets,
and forbidden dependencies. SQL detection is static, not a runtime query tracer. Syntax-based naming,
control-flow and import checks are deliberately bounded; independent review covers semantics.

`guard hook edit-guard` accepts post-edit JSON with `tool_input.file_path`.
`guard hook main-push-gate` accepts Git pre-push stdin, one ref update per line.
Exit codes: `0` pass/exempt, `2` blocking findings or invalid policy, `3` unavailable checker/provider.
Omitted `--base` resolves `origin/<main_branch>`, `<main_branch>`, then `HEAD`.
An explicitly supplied invalid base fails with exit code `2`, even with explicit files. A failed diff is never an empty changed scope.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
bin/guard check --policy-only
```

The test suite uses temporary repositories and simulated provider CLIs; it does not call model services.
This repository dogfoods the core preset. `config` and `policy` own configuration and manifests;
`topology` owns static facts/checks; `review` owns snapshot scopes, provider execution and evidence;
`init`, `distill`, `hooks` and `cli` compose those contracts.

MIT licensed.
