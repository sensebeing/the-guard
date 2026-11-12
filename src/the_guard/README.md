# Package boundaries

- `config` validates repository-owned settings; `policy` validates manifest and rule documents.
- `finding` defines the static diagnostic contract; `git_helpers` reads historical file content.
- `topology` owns language analysis, declared dependency constraints and module discovery.
- `review/contract` owns reviewer types and wire schema; `review/github` owns GitHub PR target parsing, attributable formatting, and publication; `repository` owns Git scope and patch identity;
  `session` owns local evidence persistence; `provider` owns CLI discovery/configuration;
  `runner` owns process execution and response decoding; `prompt` owns snapshot-bound instructions;
  `engine` composes review and continuation. These modules do not implement product policy semantics.
- `init`, `distill`, `hooks`, `auto_update` and CLI modules compose those APIs for user workflows.
- `auto_update` is the sole owner of `guard self-update`, global skill symlink repair, and background update cooldown metadata in `~/.guard`. `cli` schedules post-command background self-update only after `check`, `check-push`, or `review` (`run` / `check-push`) completes and skips background updates when the inspected `--repo-root` is the `the-guard` checkout itself, so snapshot-bound inspection never races with tool updates.

Policy meanings live in the installed rule Markdown, not duplicate Python rule dictionaries.
Discovery reports observed structure; it never grants ownership or changes declared constraints.
Review reads committed snapshots. Only the local workflow writes receipts, logs and configuration.
