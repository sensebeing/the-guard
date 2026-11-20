# Package boundaries

This is the root architecture blueprint declared by `topology.architecture_docs`.
The 12 owners in `.guard/config.json` include both single-file modules and subpackages.

| Layer | Owners | Permitted layers |
| --- | --- | --- |
| foundation | `finding.py`, `git_helpers.py`, `config.py`, `policy.py`, `auto_update.py` | foundation |
| domain | `topology/`, `review/` | foundation, domain |
| workflow | `distill.py`, `hooks.py`, `init.py` | foundation, domain, workflow |
| surface | `__init__.py`, `cli.py` | foundation, domain, workflow |

Layer ceilings do not grant dependencies: each module has a minimal explicit allowlist.
`review` may consume `topology`; `topology` cannot import `review`. Lower layers never
import package exports or CLI. `config.VERSION` owns the version re-exported by the surface.


- `config` validates repository-owned settings; `policy` validates manifest and rule documents.
- `finding` defines the static diagnostic contract; `git_helpers` owns safe repository/snapshot file and symlink reads and changed-line diffs.
  `policy` adapts reader failures into policy diagnostics; `review/documents` consumes the same reader.
- `topology/imports` owns Python/JS/TS import extraction and longest-prefix module resolution;
  `paths` owns production exclusions; `discovery` reports inventory; `configured` enforces declared
  dependencies and layer ceilings at touched import lines, with an import-only API that compares owner-edge permission outcomes for constraint rechecks;
  diagnostic text never decides baseline grandfathering. `runner` composes language checks;
  `audit` owns shared dependency evidence for full scans and baseline-aware contract drift,
  scanning only affected modules and rechecking complete production imports (including changed files
  and callers affected by target-layer or owner-resolution changes) when constraints tighten; `sync` owns the pure inventory merge shared by sync and init without
  granting observed imports permission; both validate merged configuration before writing. `config` rejects invalid layer edges before these run.
- `review/contract` owns reviewer types and wire schema; `review/github` owns GitHub PR target parsing, attributable formatting, and publication; `repository` owns Git scope and patch identity;
  `documents` owns review document inventory, binding blueprint selection and snapshot reads; `session` owns local evidence persistence; `provider` owns CLI discovery/configuration;
  `runner` owns process execution and response decoding; `prompt` owns snapshot-bound instructions;
  `engine` composes review and continuation. These modules do not implement product policy semantics.
- `init`, `distill`, `hooks` and CLI modules compose those APIs for user workflows.
- `auto_update` owns checkout selection, package-tree digest verification, package activation, `guard self-update`, installed uv-tool repair, global skill symlink repair, and background update cooldown metadata in `~/.guard`. Both CLI entry points use the same checkout selection as package bootstrap. A Git source compares against its own HEAD, and a VCS install compares against its PEP 610 commit. Without per-install provenance, an installed package can use a clean `~/the-guard` checkout at the globally installed commit if its own package digest differs. Bootstrap redirects submodule imports and reloads the updater from the checkout while retaining the original package as the baseline for CLI delegation. CLI workflows and hooks schedule background updates. Snapshot-bound inspection of the guard checkout from its worktree remains local.
- Review sessions persist the first reviewed head, the binding revisions of task blueprints already reviewed, and a contract digest covering reviewer code, configuration, policy rules, root instructions and architecture/boundary snapshots. A digest change starts a fresh full review while preserving the binding blueprint revisions for a valid branch baseline.

Policy meanings live in the installed rule Markdown, not duplicate Python rule dictionaries.
Discovery reports observed structure; it never grants ownership or changes declared constraints.
Review reads committed snapshots. Only the local workflow writes receipts, logs and configuration.

Review prompts retain the complete module/layer map and load declared boundary documents for
touched modules from the reviewed snapshot (24,000 characters per document). Legacy adapter
coupling never authorizes introducing or deepening a declared core boundary violation.

`guard review` always loads root manifest prose (without its policy YAML), declared
`architecture_docs`, and existing `ARCHITECTURE.md` / `docs/architecture.md` from the
reviewed commit. Full, batched and incremental reviews map changes to these contracts
before examining hunks. Missing boundary declarations do not count as loaded evidence.

Full and incremental prompts preview up to four task blueprints. Discovery scans changed
Markdown at the merge base, reviewed revisions, and HEAD, plus `--note`. Links beginning with
`./` or `../` resolve against the source document directory. Other links try the repository root
first and then the source directory, using the tree where the link appears. Previews prioritize
unchanged referenced merge-base evidence, then changed merge-base blueprints, then branch
blueprints. The merge base supplies immutable Anchor 0. Each changed blueprint and referenced
merge-base document retains ordered
revisions for every distinct reviewed body. Inline previews include each historical body and a
bounded diff or deletion marker when it differs from HEAD. Symlink-backed blueprints compare
resolved bodies. The prompt indexes revision lists for paths outside the inline preview.
Anchor histories survive provider restarts and verified rebases. Operational ledgers are excluded.
Changes limited to non-contract reports or plans remain review-exempt.

Reviewers derive the minimal target architecture from the business goal and loaded blueprints,
then compare it with the committed schema, writers, lifecycle transitions, and readers across
the touched subsystem. New capabilities belong on the authoritative entity lifecycle or read
projection. Consolidation and root-cause repairs must remove the redundant persistence and
state transitions targeted by the change. Reviewers check binding scope against merge-base
Anchor 0 and prior reviewed branch blueprints, including referenced audit evidence, root-cause
repairs, retirement and convergence commitments, and target complexity budgets. Branch-introduced
overdesign can be simplified at the sole owner when the change preserves those obligations and
equivalent safety boundaries. Reviewers block consolidations that move a secondary lifecycle into
parent-table columns while its state machine remains live.

`guard check`, branch-wide `guard topology check`, and protected-push hooks reject
new missing declared documents and newly stale dependency edges. An edited production
file without a declared module also fails the edit guard. Untouched baseline debt stays
grandfathered; `guard topology audit` reports the full repository debt. Changes to module
responsibilities require synchronized configuration and authoritative boundary docs.
`guard topology sync` refreshes inventory without granting observed imports permission.

Review CLI, check-push and protected-push checks read the exact target Git snapshot directly,
without creating temporary worktrees. Exact snapshot reads use committed submodule metadata,
reject unavailable blobs and disable working-tree rename inference for both baseline and target reads; binary assets do not count as import coverage. Dirty local files cannot alter the verdict. Deterministic
findings block provider execution and receipt acceptance; the Python review engine remains the
pure independent-review entry point. Loaded configuration, policy and contract digests pass
directly to review consumers instead of being reloaded by each helper.
Audit reports all missing module paths. Drift blocks modules deleted since the baseline or newly
registered at nonexistent paths, while grandfathering baseline debt. Newly registered modules
require roles once layer/dependency governance is established; sync prunes deleted owners and
incoming dependency edges. Edit guard ignores external paths and validates edited policy/configuration. Contract classification includes implicit module README
files and safe symlink targets; changing those documents selects their declaring modules for
review context. Dependency drift retains baseline language coverage when the final source file
is deleted, without treating unsupported-language modules as fully observable.
