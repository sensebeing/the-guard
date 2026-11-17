# Package boundaries

This is the root architecture blueprint declared by `topology.architecture_docs`.
The 12 owners in `.guard/config.json` include both single-file modules and subpackages.

| Layer | Owners | Permitted layers |
| --- | --- | --- |
| foundation | `finding.py`, `git_helpers.py`, `config.py`, `policy.py` | foundation |
| domain | `topology/`, `review/` | foundation, domain |
| workflow | `distill.py`, `hooks.py`, `init.py`, `auto_update.py` | foundation, domain, workflow |
| surface | `__init__.py`, `cli.py` | foundation, domain, workflow |

Layer ceilings do not grant dependencies: each module has a minimal explicit allowlist.
`review` may consume `topology`; `topology` cannot import `review`. Lower layers never
import package exports or CLI. `config.VERSION` owns the version re-exported by the surface.


- `config` validates repository-owned settings; `policy` validates manifest and rule documents.
- `finding` defines the static diagnostic contract; `git_helpers` owns safe repository/snapshot file and symlink reads and changed-line diffs.
  `policy` adapts reader failures into policy diagnostics; `review/documents` consumes the same reader.
- `topology/imports` owns Python/JS/TS import extraction and longest-prefix module resolution;
  `paths` owns production exclusions; `discovery` reports inventory; `configured` enforces declared
  dependencies and layer ceilings at touched import lines, with an import-only API for constraint rechecks. `runner` composes language checks;
  `audit` owns shared dependency evidence for full scans and baseline-aware contract drift,
  scanning only affected modules and rechecking complete production imports (including changed files
  and callers affected by target-layer or owner-resolution changes) when constraints tighten; `sync` owns the pure inventory merge shared by sync and init without
  granting observed imports permission; both validate merged configuration before writing. `config` rejects invalid layer edges before these run.
- `review/contract` owns reviewer types and wire schema; `review/github` owns GitHub PR target parsing, attributable formatting, and publication; `repository` owns Git scope and patch identity;
  `documents` owns review document inventory and snapshot reads; `session` owns local evidence persistence; `provider` owns CLI discovery/configuration;
  `runner` owns process execution and response decoding; `prompt` owns snapshot-bound instructions;
  `engine` composes review and continuation. These modules do not implement product policy semantics.
- `init`, `distill`, `hooks`, `auto_update` and CLI modules compose those APIs for user workflows.
- `auto_update` is the sole owner of `guard self-update`, package-tree digest verification, installed uv-tool repair, checkout delegation, global skill symlink repair, and background update cooldown metadata in `~/.guard`. Invocations detect a clean newer `~/the-guard` checkout and delegate immediately while a non-blocking update refreshes the installed CLI; hooks also schedule updates, and snapshot-bound inspection of the guard checkout remains local.
- Review sessions persist a contract digest covering reviewer code, repository configuration, policy rules, root instructions and architecture/boundary document snapshots; a digest change discards incremental continuation and starts a fresh full review.

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
