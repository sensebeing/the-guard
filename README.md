# the-guard

[![MIT license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Zero runtime dependencies](https://img.shields.io/badge/runtime_dependencies-0-green.svg)](pyproject.toml)

**Let your whole team build with AI.**

Architecture guardrails for teams building software with AI.

AI has made writing code effortless. The real bottleneck today is **trust**: who dares to let AI-generated changes land directly in production? A feature can work and pass tests while placing a business rule in the wrong service, duplicating a state machine, or leaving an obsolete code path alive.

`the-guard` embeds your team's architectural principles directly into the repository—guiding coding agents before they write a line of code, catching structural regressions during development, and running an independent adversarial agent review before integration.

[中文文档](README.zh-CN.md) · [Quick start](#quick-start) · [Presets & rules](#presets-and-the-14-engineering-quality-rules) · [Workflow](#workflow) · [Configuration](#configuration) · [Setup skill](skills/the-guard-setup/SKILL.md) · [Daily skill](skills/the-guard/SKILL.md)

## Get started in 30 seconds — paste to your agent

Paste this into Claude Code, Codex, Cursor, Antigravity, or Orca:

```text
Set up the-guard for this repository while preserving my existing instructions and hooks.

1. Check `python3 --version` (require Python >= 3.11) and `command -v guard`.
   If `guard` is not installed, clone https://github.com/sensebeing/the-guard.git into ~/the-guard
   (or update an existing checkout with `git -C ~/the-guard pull --ff-only`), then install it with
   `python3 -m pip install -e ~/the-guard` (or use `~/the-guard/bin/guard`).
2. Register the skills by symlinking `~/the-guard/skills/the-guard` and
   `~/the-guard/skills/the-guard-setup` into `~/.agents/skills/`, `~/.claude/skills/`,
   and `~/.gemini/config/skills/` if present, preserving existing files.
   Read `~/the-guard/skills/the-guard-setup/SKILL.md`.
3. Inspect this repository's `AGENTS.md`, `CLAUDE.md`, module docs, and `.guard/config.json`,
   and run `guard probe` to check available local review agents (`codex`, `claude`, `agy`).
4. Ask me which onboarding mode I want (`core`, `full`, `distill`, or `hybrid`) and whether
   to install the local Git `pre-push` gate (`--with-hooks`).
5. Initialize the chosen preset or distilled policy, run `guard topology sync` and
   `guard check --policy-only`, and give me a quick daily workflow cheatsheet.
```

If `guard` is already installed elsewhere, locate its source checkout (or clone it to `~/the-guard`) for the skills in step 2. With the direct launcher, substitute `~/the-guard/bin/guard` for `guard` below.

## Why we built it

On our team, our product designer uses her own ChatGPT and coding agent to implement her own product requirements directly in production code. This isn't a hackathon demo—it is how we ship software every day.

Working this way changed our core engineering question: **How do we let designers, product managers, domain experts, and engineers ship features with their preferred AI—without needing a senior architect to babysit every pull request?**

Tests verify that a feature works. Linters, formatters, and static analyzers catch style issues and bug patterns. Yet a green CI pipeline still cannot tell whether a UI component just recreated a backend state machine, or whether a bug fix merely stacked a retry loop over a broken data model.

`the-guard` makes architectural intent executable and self-enforcing inside the repository:
- **Who owns each business decision** and where module boundaries lie.
- **What must be deleted** when a new path replaces an old one.
- **Whether a change solves the root cause** instead of patching symptoms.

When the repository itself carries and enforces these standards, anyone on the team can turn an idea into production-ready code with confidence.

## Three defenses, one set of rules

| Stage | Defense | What it does |
| --- | --- | --- |
| **1. Before coding** | **Upfront rules in `AGENTS.md` / `CLAUDE.md`** | Puts architectural policies and module ownership contracts directly in front of the coding agent before implementation begins. |
| **2. During editing** | **Deterministic topology checks & ratchets** | Enforces module import boundaries, exclusive SQL table ownership, retired-table ratchets, and AST/lexical anti-pattern checks via `guard check`. |
| **3. Before merge** | **Independent adversarial agent review** | Launches a separate reviewer agent (`codex`, `claude`, or `agy`) against committed Git snapshots to challenge ownership, complexity, and root-cause design before issuing a push receipt. |

Static checks enforce deterministic structural facts; independent agent review evaluates architectural meaning.

## Quick start

### 1. Install

With Python 3.11+ and Git installed:

```sh
python3 -m pip install /path/to/the-guard
```

Or run `/path/to/the-guard/bin/guard` directly from a checkout. The CLI has **zero external runtime dependencies**; independent review uses your locally installed `codex`, `claude`, or `agy` CLI.

### 2. Initialize in 30 seconds

Inside your Git repository:

```sh
guard init --preset core
guard topology sync
guard check --policy-only
```

This installs four foundational rules—**Architecture Simplicity**, **Single Source of Truth**, **Service Boundaries**, and **Code Organization**—discovers your module inventory, and validates the policy manifest.

Want to install the full 14-rule engineering standard and enable the local `pre-push` gate?

```sh
guard init --preset full --with-hooks
```

### 3. Or let your agent set it up interactively

Register [the-guard-setup](skills/the-guard-setup/SKILL.md) in your agent's skill directory and invoke `$the-guard-setup`. It guides you through four onboarding modes:
- **core**: 4 foundational architectural rules.
- **full**: All 14 production engineering quality rules.
- **distill**: Distill architecture rules tailored to your existing codebase (`guard distill`).
- **hybrid**: Combine core presets with codebase-specific module boundaries.

## Presets and the 14 engineering quality rules

| Mode | Best fit | What you get |
| --- | --- | --- |
| `core` | **Recommended default — 4 rules** | Universal architectural invariants: Single Source of Truth, Architecture Simplicity, Service Boundaries, and Code Organization, with zero stack assumptions. |
| `full` | **Production full-stack — 14 rules** | All 4 Core rules plus 10 specialized rules covering data modeling, SQL and call-topology efficiency, event-driven execution, process-adjunct separation, no silent fallback, frontend design-system consumption, agent/skill/prompt contracts, LLM input/contract quality, tool schema authoring, and resident test quality. |
| `distill` | **Codebase-derived** | `guard distill` inspects existing modules, tooling, and bounded code samples so your agent can distill rules tailored to your codebase. |
| `hybrid` | **Core + codebase topology** | The 4 Core rules plus codebase-distilled module boundaries, SQL table owners, and retired-table ratchets. |

`core` and `full` are built-in `guard init --preset` values; `distill` and `hybrid` are agent-guided onboarding modes. Discovery supplies evidence, not approved ownership: confirm codebase-specific boundaries before enforcing them.

### Core preset (4 rules)

| Rule | Focus area | Core invariant |
| --- | --- | --- |
| `guard.architecture-simplicity`<br>[Architecture Simplicity](src/the_guard/presets/rules/architecture-simplicity.md) | Ownership & write convergence | One explicit owner per responsibility and write decision; converge on write, read directly without write locks or reverse-fetching; delete replaced mechanisms instead of stacking defensive rechecks. |
| `guard.single-source-of-truth`<br>[Single Source of Truth](src/the_guard/presets/rules/single-source-of-truth.md) | Canonical state & cutover | One executable definition per business rule, state machine, vocabulary, and filter; complete reader cutover and legacy deletion in the same change without dual-track fallback reads. |
| `guard.service-boundaries`<br>[Service Boundaries](src/the_guard/presets/rules/service-boundaries.md) | Module boundary contracts | Each module performs only the responsibilities declared in its owner contract; shared libraries and UI never duplicate vertical domain logic. |
| `guard.code-organization`<br>[Code Organization](src/the_guard/presets/rules/code-organization.md) | Cohesion & acyclic structure | Cohesive module/class/method responsibilities, acyclic dependencies, top-level imports, and dead-code deletion; no mechanical file splitting or MRO method shadowing. |

### Full preset additions (10 rules)

| Rule | Focus area | Core invariant |
| --- | --- | --- |
| `guard.data-modeling`<br>[Data Modeling](src/the_guard/presets/rules/data-modeling.md) | Schema & retired tables | Explicit schema owner and stable semantics; derived data never becomes a competing truth; retired tables are delete-only. |
| `guard.database-query-efficiency`<br>[Database Query Efficiency](src/the_guard/presets/rules/database-query-efficiency.md) | Call fan-out, N+1 & locks | Control end-to-end call fan-out and SQL cost; no duplicate read-only fetches in one action, no N+1 awaited reads in loops, no RPC/LLM calls inside DB locks, and CAS/version checks on lock-free settlement. |
| `guard.event-driven-execution`<br>[Event-driven Execution](src/the_guard/presets/rules/event-driven-execution.md) | Events, watermarks & queues | Real events or messages drive execution; async work queues guarantee single-watermark discovery, terminal settlement per row, and starvation prevention; no unapproved polling/timers. |
| `guard.process-adjunct-not-truth`<br>[Process Adjunct, Not Truth](src/the_guard/presets/rules/process-adjunct-not-truth.md) | Business truth vs. metadata | Build fingerprints, prompt versions, and runtime metadata are process adjuncts, not business truth; recovery and continuation rely on business obligations and safe retryability. |
| `guard.no-silent-fallback`<br>[No Silent Fallback](src/the_guard/presets/rules/no-silent-fallback.md) | Explicit failure handling | Never fabricate facts or mask failures behind silent defaults; isolate unknown parts and block only affected unsafe operations. |
| `guard.frontend-product-surface`<br>[Frontend Product Surface](src/the_guard/presets/rules/frontend-product-surface.md) | Design system consumption | Strictly follow Design System → Components → Pages consumption; keep business state, navigation, and presentation in their respective owners. |
| `guard.agent-skill-prompt`<br>[Agent, Skill and Prompt Contracts](src/the_guard/presets/rules/agent-skill-prompt.md) | Prompt & runtime symmetry | Keep skills, prompts, tool schemas, and runtime gates symmetric; runtime injects deterministic context, code enforces hard safety gates, and prompts never copy state machines or schemas. |
| `guard.llm-contract-quality`<br>[LLM Input and Contract Quality](src/the_guard/presets/rules/llm-contract-quality.md) | Model input & output schemas | Distinguish confirmed facts from candidates in model inputs; normalize valid variants, preserve parse/refusal diagnostics, and never guess identity or business facts. |
| `guard.tool-authoring`<br>[Tool Authoring](src/the_guard/presets/rules/tool-authoring.md) | Flat tool schemas & lifecycle | One domain resource and homogeneous authorization per tool; flat action-discriminated schemas (no nested envelopes, parallel arrays, or open dicts); expose intent parameters only and remove retired tools end-to-end. |
| `guard.test-case-quality`<br>[Test Case Quality](src/the_guard/presets/rules/test-case-quality.md) | High-signal invariant tests | Resident tests protect core business invariants and state what failure means; assert observable behavior rather than mock-only calls or source-text mirrors. |

## Workflow

```mermaid
flowchart TD
    A["Contributor (Designer / PM / Engineer) + Coding AI"] --> B["Read AGENTS.md & module contracts"]
    B --> C["Implement change & run guard check"]
    C --> D["Commit on topic branch"]
    D --> E["Run independent guard review"]
    E -->|Blocking architectural findings| F["Refactor root cause or supply contract rebuttal"]
    F --> D
    E -->|GO receipt issued| G["Verify receipt via guard check-push / pre-push hook"]
    G --> H["Merge & ship with confidence"]
```

1. **Align upfront**: The coding agent reads root policies in `AGENTS.md` and touched modules' owner docs.
2. **Check deterministically**: Run `guard check --base origin/main` during development to catch boundary or AST violations immediately.
3. **Commit & review**: Commit changes on a topic branch and run `guard review`. An independent reviewer agent audits the committed diff against your rules.
4. **Converge or rebut**: If the reviewer flags a structural issue, fix the root cause across the pattern—or rebut with code/contract evidence via `guard review --note "<evidence>"`.
5. **Gate integration**: `guard check-push` (or the `pre-push` hook installed by `--with-hooks`) verifies that a valid `GO` receipt matches the committed patch before pushing to `main`.

### A concrete example

Suppose a designer asks her coding agent to add an order-status badge to a screen, and the agent computes the status rule directly inside the UI component—duplicating logic already owned by the orders service.

- A traditional linter sees valid syntax and types.
- `guard check` blocks forbidden cross-layer imports deterministically.
- `guard review` reads the orders service contract, flags the duplicated business rule under `guard.single-source-of-truth`, and asks the contributor's agent to consume the backend's canonical status field instead.

The contributor's agent fixes the implementation in one turn, re-runs `guard review`, receives a `GO` receipt, and ships clean architecture without waiting on a human code review bottleneck.

## Where it fits

`the-guard` complements your existing engineering toolchain:

| Tool category | What it does best | What `the-guard` adds |
| --- | --- | --- |
| **Linters & formatters** ([Ruff](https://github.com/astral-sh/ruff), ESLint) | Fast checks for syntax, style, formatting, and local bug patterns. | Semantic ownership rules and cross-module architectural review. |
| **Static & security analyzers** ([Semgrep](https://github.com/semgrep/semgrep)) | Pattern-based security and correctness rules. | A unified contract linking agent instructions, module docs, and adversarial design review. |
| **Dependency validators** ([dependency-cruiser](https://github.com/sverweij/dependency-cruiser)) | Language-specific dependency graphs and import rules. | Zero-config Python/Swift topology ratchets paired with LLM architectural judgment. |
| **Coding agents** (ChatGPT, Claude Code, Codex) | Write features, refactor code, and fix bugs rapidly. | An independent second opinion that challenges shortcuts before code lands on `main`. |

## Core philosophy

1. **One decision, one owner (`guard.single-source-of-truth`)**: Every business rule, state machine, vocabulary, and classification has a single canonical owner. Consumers render or forward results instead of re-deriving them.
2. **Simplify at the source (`guard.architecture-simplicity`)**: Fix root causes at the owner boundary. Delete replaced code paths, legacy tables, and compatibility shims in the same change rather than stacking retries or fallback branches.
3. **Preserve real safety boundaries**: Simplification never removes authentication, tenant isolation, concurrency locks, idempotency, or transactional integrity at the write boundary.
4. **Evidence over ceremony**: Reviewers must cite concrete file paths and contract violations. When the code is already right, a factual rebuttal (`guard review --note`) resolves the finding without cosmetic churn.

See the [core rule definitions](docs/standards/rules/architecture-simplicity.md) and the full 14-rule [preset catalog](skills/the-guard-setup/references/preset-catalog.md).

## Independent review

`the-guard` discovers locally available agent CLIs (`codex > claude > agy` by default) and runs reviews against committed Git snapshots:

```sh
guard probe --priority-order codex,claude,agy
guard probe --provider-model codex=gpt-5.4:high
guard review --provider codex
guard review run --force
guard review check-push
```

- **Full vs. incremental follow-up**: Initial reviews inspect the complete changed file scope before reporting all high-confidence blockers. Subsequent runs on the same branch resume the verified reviewer session to audit fixes and new deltas.
- **Snapshot discipline**: Review prompts bind reads to committed Git refs (`base..head`) and forbid writes, test execution, or subagent spawning during review.
- **Patch-equivalent receipts**: Receipts (`the-guard-receipt/v1`) bind to Git stable patch IDs and patch digests under `.git/the-guard-receipts/`, allowing clean rebases onto an updated `main` to carry forward verified review evidence automatically.

## Configuration

`.guard/config.json` is optional—out of the box, `guard` reads the policy block in `AGENTS.md`. Add `.guard/config.json` when you want custom paths, review scopes, or declarative topology constraints (see the full multi-service [examples/config.example.json](examples/config.example.json)):

```json
{
  "manifest_file": "AGENTS.md",
  "detail_dir": "docs/standards/rules",
  "main_branch": "main",
  "topology": {
    "forbidden_imports": [{"from": "src/ui/", "to": "internal.storage"}],
    "exclusive_sql_owners": [
      {
        "table": "jobs",
        "owners": ["src/storage/jobs.py"],
        "rule_id": "guard.single-source-of-truth",
        "message": "Use the jobs owner API."
      }
    ],
    "retired_tables": [
      {"scope_prefixes": ["src/"], "tables": ["old_jobs"], "rule_id": "guard.data-modeling"}
    ],
    "forbidden_patterns": [
      {
        "scope_prefixes": ["src/ui/"],
        "literal_or_regex": "/private-owner-state",
        "literal": true,
        "rule_id": "guard.service-boundaries",
        "message": "Consume the public owner projection."
      }
    ]
  }
}
```

- **Policy manifest**: Stored between `<!-- the-guard-policy:start -->` and `<!-- the-guard-policy:end -->` in `AGENTS.md`:
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
- **Inline exemptions**: Suppress a specific static finding with a concrete justification (`>= 8` chars):
  `# guard-exempt guard.architecture-simplicity: concrete reason here`
- **Environment variables**: Configure reviewer behavior via `THE_GUARD_REVIEW_CONFIG`, `THE_GUARD_REVIEW_PROVIDER`, `THE_GUARD_REVIEW_MODEL`, `THE_GUARD_REVIEW_EFFORT`, `THE_GUARD_REVIEW_STALL_SECONDS`, `THE_GUARD_REVIEW_HEARTBEAT_SECONDS`, `THE_GUARD_REVIEW_MAX_SECONDS`, and `THE_GUARD_REVIEW_LOG_DIR`. Provider discovery persists to `~/.config/the-guard/review-agent.json`.

## Topology checks and CLI reference

```sh
guard distill --repo-root . --json
guard topology sync --repo-root .
guard topology check --base origin/main
guard check --file src/api.py --file app/Screen.swift --json
```

| Check layer | What it enforces |
| --- | --- |
| **Python AST checks** | Write locks inside read-only functions/endpoints; duplicate awaited reads on the same execution path without an intervening write; single-entity awaited reads inside loops/comprehensions. |
| **Swift lexical checks** | Duplicate awaited reads within the same function scope. |
| **Declared topology rules** | `forbidden_imports` (`module_boundaries`), `exclusive_sql_owners`, `retired_tables`, and `forbidden_patterns`. |
| **Legacy debt ratchets** | Baseline-aware line & function-signature matching so legacy files only fail when a change introduces new violations or fails to reduce touched `retired_tables` references. |

### Commands and exit codes

| Command | Purpose |
| --- | --- |
| `guard init` | Initialize policy manifest and rule files (`--preset core\|full`, `--with-hooks`, `--rules-dir`, `--manifest-file`). |
| `guard check` | Validate policy manifest and run deterministic topology checks on changed files (`--policy-only`, `--base`, `--file`, `--json`). |
| `guard topology sync` / `check` | Sync discovered module inventory into `.guard/config.json` or run topology checks standalone. |
| `guard distill` | Emit repository module inventory, tooling signals, and code samples for agent-assisted rule distillation. |
| `guard review` / `guard review run` | Run independent agent architecture review on committed changes (`--provider`, `--force`, `--note`). |
| `guard check-push` | Verify that current `HEAD` has a matching `GO` review receipt or is review-exempt ( ordinary prose / lockfile-only changes). |
| `guard probe` | Discover and configure local reviewer agent CLIs (`--priority-order`, `--provider-model`, `--reprobe`). |
| `guard hook edit-guard` | Post-edit hook for coding agents (reads `tool_input.file_path` from JSON stdin). |
| `guard hook main-push-gate` | Git `pre-push` hook enforcing valid review receipts before pushing to protected branches. |

Exit codes: `0` pass (`GO` or exempt), `2` blocking policy/topology/review findings, `3` checker or reviewer provider unavailable.

## Development

`the-guard` dogfoods its own core preset and deterministic checks:

```sh
uv run python -m unittest discover -s tests -v
uv run guard check --base origin/main
```

See [src/the_guard/README.md](src/the_guard/README.md) for internal package architecture and module boundaries.

See [Contributing](CONTRIBUTING.md) for setup and review requirements, [Security](SECURITY.md) for private vulnerability reporting, and [Changelog](CHANGELOG.md) for release history.

## License

[MIT](LICENSE)
