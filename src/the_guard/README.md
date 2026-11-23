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


- `config` validates repository-owned settings and reads committed configuration through `git_helpers`.
  `policy` validates manifest and rule documents.
- `finding` defines the static diagnostic contract; `git_helpers` owns safe repository/snapshot file and symlink reads and changed-line diffs.
  `policy` adapts reader failures into policy diagnostics; `review/documents` consumes the same reader.
- `topology/imports` owns Python/JS/TS import extraction and longest-prefix module resolution;
  `paths` owns production exclusions; `discovery` reports inventory; `configured` enforces declared
  dependencies and layer ceilings at touched import lines and enforces delete-only module retirements.
  Its import-only API compares owner-edge permissions for constraint rechecks.
  `runner` applies built-in language diagnostics for rule IDs installed in the target policy;
  `audit` owns shared dependency evidence for full scans and baseline-aware contract drift,
  scanning only affected modules and rechecking complete production imports (including changed files
  and callers affected by target-layer or owner-resolution changes) when constraints tighten; `sync` owns the pure inventory merge shared by sync and init without
  granting observed imports permission; both validate merged configuration before writing. `config` rejects invalid layer edges before these run.
- `review/contract` owns reviewer types, wire schema and output validation; `review/github` owns GitHub PR target parsing, attributable formatting, and publication; `repository` owns Git scope, patch identity and receipt carry-forward;
  `documents` owns review document inventory, binding blueprint selection and snapshot reads; `session` owns local evidence persistence; `provider` owns CLI discovery/configuration;
  `runner` owns process execution and provider stream decoding; `prompt` owns snapshot-bound instructions;
  `engine` composes review and continuation. These modules do not implement product policy semantics.
- `init`, `distill`, `hooks` and CLI modules compose those APIs for user workflows.
- `auto_update` owns checkout selection, package-tree digest verification, package activation, `guard self-update`, installed uv-tool repair, global skill symlink repair, and background update state in `~/.config/the-guard`. The default installation migrates legacy update metadata and rotates the update log at 512 KiB, keeping one backup. Both CLI entry points use the same checkout selection as package bootstrap. A Git source compares against its own HEAD, and a VCS install compares against its PEP 610 commit. Without per-install provenance, an installed package can use a clean `~/the-guard` checkout at the globally installed commit if its own package digest differs. Bootstrap redirects submodule imports and reloads the updater from the checkout while retaining the original package as the baseline for CLI delegation. CLI workflows and hooks schedule background updates. Snapshot-bound inspection of the guard checkout from its worktree remains local.
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

`review.blueprint_globs` defaults to an empty array. When configured, `review/documents` selects
matching Markdown blueprints and discovers matching one-hop references from Markdown changed in the branch, reading
its bodies at the merge base, reviewed revisions, and HEAD, and from `--note`. Persisted anchors
without branch changes are retained as evidence and their outgoing links are skipped. Links
beginning with `./` or `../` resolve against the source document directory. Other links try the
repository root first and then the source directory, using the tree where the link appears.
Selection excludes unchanged, already loaded contracts before allocating up to four inline
preview slots. Previews prioritize unchanged referenced merge-base evidence, then changed
merge-base blueprints, then branch blueprints. The merge base supplies immutable Anchor 0.
Each changed blueprint and referenced merge-base document retains ordered revisions for every
distinct reviewed body. Inline previews include each historical body and a bounded diff or
deletion marker when it differs from HEAD. Symlink-backed blueprints compare resolved bodies.
The prompt indexes revision lists for paths outside the inline preview. Anchor histories
survive provider restarts, verified rebases, and changes to discovery settings.
`review.operational_ledgers` excludes explicit repository-relative paths from discovery and
defaults to `["CHANGELOG.md"]`. Changes
limited to non-contract reports or plans remain review-exempt.

Full and incremental prompts direct reviewers to inspect the complete touched subsystem at the
target snapshot and compare it with the immutable merge-base Anchor 0, prior reviewed branch
blueprints, and referenced evidence. Rule Markdown supplies the blocking criteria, including
binding scope and authorized simplification in [Architecture Simplicity](presets/rules/architecture-simplicity.md).

`guard check`, branch-wide `guard topology check`, and protected-push hooks reject
new missing declared documents and newly stale dependency edges. An edited production
file without a declared module also fails the edit guard. Untouched baseline debt stays
grandfathered; `guard topology audit` reports the full repository debt. Changes to module
responsibilities require synchronized configuration and authoritative boundary docs.
`guard topology sync` refreshes inventory without granting observed imports permission.
Modules declared with `retired: true` keep their reserved paths after the last file is deleted.
They have no declared dependency edges or active boundary documents. Changed files under retired
paths and inbound imports trigger service-boundary checks. A new retirement also rechecks
unchanged inbound imports throughout the repository.

Review CLI, check-push and protected-push checks read the exact target Git snapshot directly,
without creating temporary worktrees. Exact snapshot reads use committed submodule metadata,
reject unavailable blobs and disable working-tree rename inference for both baseline and target reads; binary assets do not count as import coverage. Dirty local files cannot alter the verdict. Deterministic
findings block provider execution and receipt acceptance; the Python review engine remains the
pure independent-review entry point. Loaded configuration, policy and contract digests pass
directly to review consumers instead of being reloaded by each helper.
`config.py` requires a regular configuration file and normalizes legacy import declarations only
when a consumer explicitly loads a historical baseline.
The protected-push hook uses Git's local and remote commit IDs, including object-only replayed
commits. Repository review can carry forward a matching `GO` receipt from a retained session in
another linked worktree when its contract digest and applicable merge-base and patch checks pass.
Audit reports all missing module paths. Drift blocks modules deleted since the baseline or newly
registered at nonexistent paths, while grandfathering baseline debt. Newly registered modules
require roles once layer/dependency governance is established; sync prunes deleted owners and
incoming dependency edges. Edit guard ignores external paths and validates edited policy/configuration. Contract classification includes implicit module README
files and safe symlink targets; changing those documents selects their declaring modules for
review context. Dependency drift retains baseline language coverage when the final source file
is deleted, without treating unsupported-language modules as fully observable.
