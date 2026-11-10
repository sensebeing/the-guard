# Contributing to the-guard

Contributions that make architecture guardrails clearer, simpler, and more reliable are welcome.
For a substantial behavior change, open an issue describing the invariant, owner, and observable
outcome before implementation. Report vulnerabilities privately using [SECURITY.md](SECURITY.md).

## Set up

Use Python 3.11+, Git, and [uv](https://docs.astral.sh/uv/):

```sh
git clone https://github.com/sensebeing/the-guard.git
cd the-guard
uv sync
git switch -c your-topic
```

The CLI has no external runtime dependencies. Independent review additionally needs a locally
installed and authenticated `codex`, `claude`, or `agy`; inspect availability with `uv run guard probe`.

## Develop and verify

Read [AGENTS.md](AGENTS.md) and the [package boundaries](src/the_guard/README.md) before editing.
We dogfood the core policy: one owner for each decision, cohesive modules, and root-cause fixes.
Keep changes focused, preserve trust-boundary checks, and add or extend tests for observable behavior.
Keep [README.md](README.md) and [README.zh-CN.md](README.zh-CN.md) aligned when changing public usage.

```sh
uv run python -m unittest discover -s tests -v
uv run guard check --base origin/main
```

Commit on your topic branch before independent review:

```sh
uv run guard review
uv run guard check-push
```

Resolve blocking findings at their architectural root, or supply code and owner-contract evidence
with `uv run guard review --note "<file:line and owner contract evidence>"`. Commit fixes and review
again. Static checks, provider discovery, and an actual `GO` receipt are distinct evidence.
See the [daily skill](skills/the-guard/SKILL.md) for the complete loop.

## Submit a pull request

Describe the concrete problem, resulting behavior, and verification results. Disclose checks that
could not run and unresolved findings. Never include credentials or private reviewer inputs/logs.
Maintainers review contributions and decide when to integrate them; a local receipt does not
replace that decision or grant permission to push to `main`.
