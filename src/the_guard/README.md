# Package boundaries

- `config` validates repository-owned settings; `policy` validates manifest and rule documents.
- `finding` defines the static diagnostic contract; `git_helpers` reads historical file content.
- `topology` owns language analysis, declared dependency constraints and module discovery.
- `review/contract` owns reviewer types and wire schema; `repository` owns Git scope and patch identity;
  `session` owns local evidence persistence; `provider` owns CLI discovery/configuration;
  `runner` owns process execution and response decoding; `prompt` owns snapshot-bound instructions;
  `engine` composes review and continuation. These modules do not implement product policy semantics.
- `init`, `distill`, `hooks` and CLI modules compose those APIs for user workflows.

Policy meanings live in the installed rule Markdown, not duplicate Python rule dictionaries.
Discovery reports observed structure; it never grants ownership or changes declared constraints.
Review reads committed snapshots. Only the local workflow writes receipts, logs and configuration.
