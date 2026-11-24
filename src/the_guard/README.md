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
- `finding` defines the static diagnostic contract. `git_helpers` owns Git subprocess repository
  isolation, batched tree and blob reads, safe snapshot reads, stable patch-ID streaming, and
  changed-line diffs. Snapshot readers reuse their tree inventory across preload calls.
  `policy` adapts reader failures into policy diagnostics, and `review/documents` uses the same reader.
- `topology/imports` owns Python/JS/TS import extraction and longest-prefix module resolution;
  `paths` owns production exclusions; `discovery` reports inventory; `configured` enforces declared
  dependencies and layer ceilings at touched import lines and enforces delete-only module retirements.
  Its import-only API compares owner-edge permissions for constraint rechecks.
  `runner` applies installed language diagnostics, batches target snapshot reads and reads baseline
  bodies for files present in the target. `audit` owns dependency evidence for full scans and
  baseline-aware contract drift. It skips inert assets before reading import evidence and checks
  changed sources and remaining dependency witnesses when ownership is stable. Import permission
  checks reuse parsed imports when the parser input and resolution roots permit it. Audit rechecks production imports
  when dependency, layer, or owner-resolution constraints tighten. `sync` owns the inventory merge
  shared by sync and init without granting observed imports permission. Both validate merged
  configuration before writing. `config` rejects invalid layer edges before these run.
- `review/contract` owns the explicit review contract version, reviewer types, wire schema and
  output validation. `review/github` owns GitHub PR target parsing, attributable formatting,
  and publication. `repository` owns Git scope, patch identity, receipt carry-forward and
  fsmonitor-isolated snapshot Git calls. `documents` owns review document inventory and binding
  blueprint selection, batching regular-file reads and following safe snapshot symlinks.
  `session` owns scoped contract hashes, rule scope projection and local evidence persistence.
  `provider` owns CLI discovery and configuration. `runner` owns process execution, stdin prompt
  delivery and provider stream decoding. `prompt` owns snapshot-bound instructions and groups
  bulk deletions into directory inspection commands while listing active files and pinned anchors.
  `engine` composes review and continuation. These modules do not implement product policy semantics.
- `init`, `distill`, `hooks` and CLI modules compose those APIs for user workflows. The managed
  `pre-push` hook resolves the push worktree before clearing repository selectors such as
  `GIT_DIR` using the list from `git_helpers`. It finds `guard` on `PATH` or falls back to
  `~/.local/bin/guard` and passes the complete ref update stream.
- `auto_update` owns checkout selection, package-tree digest verification, package activation, `guard self-update`, installed uv-tool repair, global skill symlink repair, and background update state in `~/.config/the-guard`. The default installation migrates legacy update metadata and rotates the update log at 512 KiB, keeping one backup. Both CLI entry points use the same checkout selection as package bootstrap. A Git source compares against its own HEAD, and a VCS install compares against its PEP 610 commit. Without per-install provenance, an installed package can use a clean `~/the-guard` checkout at the globally installed commit if its own package digest differs. Bootstrap redirects submodule imports to the checkout, then reloads the Git helpers and updater in dependency order. The original package remains the baseline for CLI delegation. CLI workflows and hooks schedule background updates. Snapshot-bound inspection of the guard checkout from its worktree remains local.
- Review Contract `v2` retains the first reviewed head and binding task blueprint revisions.
  Receipt reuse and carry-forward require the scoped `contract_digest`, which covers
  `REVIEW_CONTRACT_VERSION`, repository configuration, policy rules, root instructions,
  architecture documents, touched-module boundary documents and touched and one-hop module
  declarations. Routine upgrades preserve `GO` receipts when the version and scoped contract
  match. Scoped retired-table, forbidden-pattern and forbidden-import rules affect receipts for
  the applicable modules. The session digest covers global rules and root contracts. Branch
  configuration edits can resume when the changed declarations and scoped rules belong to
  currently or previously touched modules. A rebase also checks touched modules and one-hop
  neighbours. Sessions saved before the root digest was
  introduced require an exact scoped digest match to continue. A full review retains binding
  blueprint revisions for a valid branch baseline.

Without a local checkout, `guard self-update` runs `uv tool upgrade the-guard` and records the
installed package's PEP 610 commit, package-tree digest and CLI update status.

Provider processes receive an environment cleared of inherited Git repository selectors.
Codex isolation homes trust the worktree and common repository root, and provider stream decoding
combines stderr with structured stdout errors so quota or auth failures reach sticky failover.

For both `codex exec` and `codex exec resume`, static reviews disable each `[mcp_servers]`
entry discovered in `.codex/config.toml` layers up to the active worktree's Git root. A single
`-c` override sets `enabled=false` for each discovered server in a TOML inline table, preserving
literal names that contain dots. The isolated Codex home omits user MCP configuration, and a
linked worktree uses its own project configuration layers.

Iterative fix-and-re-review rounds rerun plain `guard review`. An eligible incremental review
prefers its current session provider and preserves the global engine preference. Transient
failures retry twice with 10- and 20-second backoff before failover. The retry count (up to three)
and backoff are configurable through `THE_GUARD_REVIEW_TRANSIENT_RETRIES` and
`THE_GUARD_REVIEW_RETRY_BACKOFF_SECONDS`. The default stall and runaway limits are 600 and 1800
seconds. `--force` starts a full review and prints a warning when it bypasses reusable evidence.
Claude and Codex receive prompts on stdin. Agy receives its prompt as an argument and rejects
inputs over 128,000 UTF-8 bytes before launching. Every `repository` snapshot Git call injects
`-c core.fsmonitor=false`, so a wedged worktree fsmonitor cannot stall review. Full runs record the
objective cause in bounded run metadata as `full_review_reason`: `force`, `no_prior_session`,
`contract_changed`, `no_incremental_delta`, `provider_changed`, `missing_session_id` or
`resume_failed`. Contract invalidation also records up to five changed contract paths in
`contract_change_causes`. Log retention protects the four most recent runs per active worktree
alongside the global target of 30 runs. `index.jsonl` rotates at 2 MiB with a single backup.
Isolated Codex homes remove `tmp/` and retain active session data.

Policy meanings live in the installed rule Markdown, not duplicate Python rule dictionaries.
Discovery reports observed structure; it never grants ownership or changes declared constraints.
Review reads committed snapshots. Only the local workflow writes receipts, logs and configuration.

Full prompts read the committed root manifest prose, architecture blueprints and touched
module boundaries. Root instructions, root `ARCHITECTURE.md` and touched module
`AGENTS.md` / `CLAUDE.md` have priority with a 48,000-character per-file limit. Other reference
documents have a 24,000-character limit. The document section has a 180,000-character budget.
Oversized documents use a heading index with line numbers and a snapshot read command.
Incremental prompts send prior findings, rebuttal evidence, the verified change, and new or
changed boundary and blueprint context. An unchanged-head rebuttal uses the session history.
The manifest supplies the allowed finding IDs for prompts, output schemas and verdict validation.
A `NO-GO` verdict requires at least one blocking finding.

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
distinct reviewed body. Inline previews include the bounded baseline body and a bounded diff
from that baseline to each subsequent distinct reviewed revision. Any reviewed body that differs
from HEAD also includes a bounded diff to HEAD or a deletion marker. Symlink-backed blueprints
compare resolved bodies.
The prompt indexes revisions outside the inline previews. For bulk deletions it groups omitted
merge-base-only blueprints by directory with commands to recover their paths. The stored anchor
history retains every applicable revision and survives provider restarts, verified rebases, and
changes to discovery settings.
`review.operational_ledgers` excludes explicit repository-relative paths from discovery and
defaults to `["CHANGELOG.md"]`. A `GO` receipt records the review contract digest and the
blueprint revisions it covered. Reuse requires coverage of the applicable retained revisions
and equivalent configured and retained blueprint bodies across replayed snapshots; across a
rebase, configured blueprints count only when the replayed branch changed them or their symlink
targets, while retained blueprint revisions remain fail-closed. Scope,
exemptions and prompts share applicable
retained history across linked worktrees, including when new discovery is disabled. Sessions
already landed in the target merge base contribute only retained blueprints whose resolved
paths the candidate branch changes, so merged unrelated branches cannot pollute new scopes or
invalidate rebased receipts. An unlanded linked-worktree session contributes its history only
when its branch changed files overlap the candidate scope, comparing safe snapshot symlink
targets so either alias of a blueprint counts. Changes
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
With stable ownership and import resolution, the stale-dependency ratchet checks
changed files first and scans unchanged sources only for edges that may have lost their last import.
`guard topology sync` refreshes inventory without granting observed imports permission.
Modules declared with `retired: true` keep their reserved paths after the last file is deleted.
They have no declared dependency edges or active boundary documents. Changed files under retired
paths and inbound imports trigger service-boundary checks. A new retirement also rechecks
unchanged inbound imports throughout the repository. Active declarations cannot be nested
within a retired path.

Review CLI, check-push and protected-push checks read the exact target Git snapshot directly,
without creating temporary worktrees. `guard review check-push --head <rev>` verifies a
specific commit or ref and defaults to `HEAD` when omitted. Exact snapshot reads use committed
submodule metadata, reject unavailable blobs and disable working-tree rename inference for both
baseline and target reads. Binary assets do not count as import coverage. Dirty local files
cannot alter the verdict. Deterministic
findings block provider execution and receipt acceptance; the Python review engine remains the
pure independent-review entry point. Loaded configuration, policy and contract digests pass
directly to review consumers instead of being reloaded by each helper.
`config.py` requires a regular configuration file and normalizes legacy import declarations only
when a consumer explicitly loads a historical baseline.
During default-state migration, `auto_update` holds both locks through the checkout, tool, skills
and metadata writes. It retires the legacy files only after a successful update that replaces or
verifies the installed guard. The cutover assumes older executables are no longer launched
after that replacement.
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
