## Problem and outcome

Describe the concrete problem and resulting behavior. Link related issues if applicable.

## Architecture and scope

Identify the decision owner, affected boundaries, and any replaced mechanisms removed.

## Verification

List commands and results, including skipped checks and unresolved findings.

- [ ] Read `AGENTS.md` and affected module owner documents.
- [ ] Ran `uv run python -m unittest discover -s tests -v`.
- [ ] Ran `uv run guard check --base origin/main`.
- [ ] Ran `uv run guard review` on committed changes and resolved blockers or supplied evidence.
- [ ] Ran `uv run guard check-push` for the reviewed patch.
- [ ] Updated both READMEs when changing public usage.

## Risks or limitations

Describe compatibility, security, or operational implications; omit private code and credentials.
