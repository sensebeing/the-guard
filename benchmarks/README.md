# GuardBench and ReviewBench

This repository ships two reproducible evaluation tracks. GuardBench is a seven-case, multi-service architectural benchmark with 12 violations across six corrupt commits and one clean change. ReviewBench records real open-source pull request comparisons. The Codex 6.1 runs use model gpt-6.1-sol with effort=medium; the public benchmark source is [sensebeing/code-review-benchmark](https://github.com/sensebeing/code-review-benchmark).

Run the deterministic suite:

    python3 benchmarks/guardbench.py

It completes in under 0.5 seconds and consumes no model tokens. Add --with-review --provider codex for an agent review and --json for machine-readable output.

## Track 1 - GuardBench

The seven-tool comparison is Ruff, Semgrep 1.185.0, code-review-graph, guard check, Claude Code raw, Codex 6.1 raw (gpt-6.1-sol), and the-guard full (guard check plus review).

| Case | Violations | Ruff | Semgrep | code-review-graph | guard check | Claude raw | Codex 6.1 raw | the-guard full | Noise / anti-architectural fixes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| case-1 duplicate UI state machine | 1 | 0 | 0 | 0 | 0 | 1 | 1 | 1 | 0 |
| case-2 cross-service SQL/import and duplicate read | 3 | 0 | 0 | 0 | 2 | 3 | 2 | 3 | 0 |
| case-3 retired-table dual read | 1 | 0 | 0 | 0 | 1 | 1 | 1 | 1 | 0 |
| case-4 retry, silent fallback, N+1 | 2 | 0 | 0 | 0 | 1 | 2 | 1 | 2 | 0 |
| case-5 read lock, duplicate read, silent old-row fallback | 3 | 0 | 0 | 0 | 1 | 3 | 1 | 3 | 0 |
| case-6 nested tool envelope and process adjunct as truth | 2 | 0 | 0 | 0 | review-only | 1 | 0.5 | 2 | 1 anti-architectural build_sha fix (case-6) |
| case-7 clean owner API (false-positive control) | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| **Corrupt-case total** | **12** | **0/6** | **0/6** | **0/6** | **4/6** | **6/6** | **6/6** | **6/6** | N/A |
| **Violation recall** | **12** | **0/12** | **0/12** | **0/12** | **5/12** | **11/12** | **7/12** | **12/12** | 0 (12/12 root-cause aligned) |

The static guard layer runs in about 40 ms with zero tokens. Noise and fix-direction results are: Ruff, Semgrep, code-review-graph, and guard check each 0; Claude Code raw 6 speculative findings (24%); Codex 6.1 raw 1 anti-architectural build_sha fix (case-6); and the-guard full 0, with 12/12 fixes aligned to root causes. The complete path attributes findings to guard rule IDs and had no clean-case false positives.

## Track 2 - Martian real open-source PRs

Aggregate comparison (`the-guard` and `Codex 6.1 raw` evaluated locally with `gpt-6.1-sol` on the 6-PR real-PR suite; third-party products from Martian's 50-PR aggregate):

| Product | Evaluated scope | Precision | Recall | F1 | TP / FP / FN |
| --- | --- | ---: | ---: | ---: | ---: |
| **the-guard (full + agy gemini-4-carbon-high, Core 158)** | **Martian 50-PR Code Review Bench** | **59.1%** | **75.9% (#1)** | **66.5% (#1)** | 120 / 83 / 38 |
| **the-guard (full + agy gemini-4-carbon-high, All-173)** | **Martian 50-PR Code Review Bench** | **59.7% (#1)** | **71.1% (#1)** | **64.9% (#1)** | 123 / 83 / 50 |
| **the-guard (full + agy gemini-4-carbon-high, Strict 139)** | **Martian 50-PR Code Review Bench** | **56.9%** | **77.0%** | **65.4%** | 107 / 81 / 32 |
| Cubic v2 | Martian 50-PR aggregate | 57.8% | 57.8% | 57.8% | 100 / 73 / 73 |
| Qodo Extended v2 / PR-Agent | Martian 50-PR aggregate | 58.9% | 55.5% | 57.1% | 96 / 67 / 77 |
| Augment | Martian 50-PR aggregate | 50.8% | 58.4% | 54.3% | 101 / 98 / 72 |
| Qodo v2 | Martian 50-PR aggregate | 47.5% | 59.5% | 52.8% | 103 / 114 / 70 |
| Cursor BugBot | Martian 50-PR aggregate | 52.8% | 43.4% | 47.6% | 75 / 67 / 98 |
| GitLab Duo | Martian 50-PR aggregate | 46.9% | 47.4% | 47.1% | 82 / 93 / 91 |
| Devin Review | Martian 50-PR aggregate | 59.3% | 38.7% | 46.9% | 67 / 46 / 106 |
| Greptile v4.1 | Martian 50-PR aggregate | 44.7% | 48.6% | 46.5% | 84 / 104 / 89 |
| Gemini Code Assist v2 | Martian 50-PR aggregate | 40.3% | 51.4% | 45.2% | 89 / 132 / 84 |
| GitHub Copilot Review v2 | Martian 50-PR aggregate | 35.2% | 63.0% | 45.1% | 109 / 201 / 64 |
| Sourcery | Martian 50-PR aggregate | 38.1% | 49.7% | 43.1% | 86 / 140 / 87 |
| Claude Code | Martian 50-PR aggregate | 42.3% | 41.0% | 41.6% | 71 / 97 / 102 |
| CodeRabbit | Martian 50-PR aggregate | 30.7% | 60.1% | 40.6% | 104 / 235 / 69 |
| **the-guard (gpt-6.1-sol)** | **6-PR real-PR suite (3 Martian + 3 ReviewBench)** | **90.0%** | **100.0%** | **94.7%** | N/A |
| **Codex 6.1 raw (gpt-6.1-sol)** | **6-PR real-PR suite (3 Martian + 3 ReviewBench)** | **80.0%** | **44.4%** | **57.1%** | N/A |

The local comparisons use the same PR snapshots:

| Pull request | Codex 6.1 raw | the-guard (gpt-6.1-sol) | the-guard findings |
| --- | --- | --- | --- |
| getsentry/sentry#80168 | NO-GO (1 finding) | NO-GO | 3: abstract handler, non-atomic event publication, test assertion mismatch |
| calcom/cal.com#22345 | GO (0 findings) | NO-GO | 2: weakened discriminated options contract, raw SQL not executed in org/team tests |
| getsentry/sentry#80528 | GO (0 findings) | GO | 0; refactor preserved behavior |

### Real-PR product comparison

The three PRs above are the local rows in a broader 11-product comparison. The commercial and hosted products below are the Martian comparison products; their per-PR rows summarize the public benchmark results.

| Product | sentry#80168 | cal.com#22345 | sentry#80528 |
| --- | --- | --- | --- |
| CodeRabbit | 1/2 true defects, 2 FP | 1/2 true defects, 3 FP | legacy issue plus style noise |
| Cursor BugBot | 1/2, 2 FP | 1/2, 1 FP | GO |
| GitHub Copilot Review v2 | 1/2, 1 FP | 1/2, 3 FP | legacy issue plus style noise |
| Greptile v4.1 | 1/2, 1 FP | 0/2, 3 FP | GO |
| Qodo Extended v2 / PR-Agent | 1/2 | 0/2, 0 FP | GO |
| Claude Code | 2/2, 1 FP | 1/2, 2 FP | GO |
| Devin | 1/2, 2 FP | 0/2, 0 FP | GO |
| Sourcery | 1/2, 3 FP | 1/2, 0 FP | legacy issue plus style noise |
| Kodus v2 | aggregate only | aggregate only | aggregate only |
| Codex 6.1 raw | 1/2 | 0/2, 0 FP | 0 |
| the-guard (gpt-6.1-sol) | 2/2 plus Kafka ordering issue | 2 blocking contract/test findings | 0, GO |

### ReviewBench

ReviewBench contains three additional real PR comparisons used by the repository's earlier benchmark report:

| Pull request | Golden findings | Codex raw | the-guard |
| --- | --- | --- | --- |
| PierreJanineh/TechDebtMCP#135 | 1 true positive and 1 annotated ReDoS false positive | Finds the ReDoS false positive; misses the true positive | Finds the true positive with guard.no-silent-fallback; 0 annotated false positives |
| AA-Factory/aafactory-prototype#17 | 3 test-contract findings | Finds 2/3 | Finds 3/3 with guard.test-case-quality |
| allan-mobley-jr/gimmes#468 | 1 stale-volume filter finding | Finds 1/1 | Finds 1/1 with owner-contract evidence |

The deterministic track is fully reproducible with [benchmarks/guardbench.py](guardbench.py). The real-PR comparison source is the public [sensebeing/code-review-benchmark](https://github.com/sensebeing/code-review-benchmark).

## Full 50-PR Martian Code Review Bench

The complete 50-PR Martian evaluation artifacts are published at [sensebeing/code-review-benchmark](https://github.com/sensebeing/code-review-benchmark), including [benchmark_data.json](https://github.com/sensebeing/code-review-benchmark/blob/main/offline/results/benchmark_data.json) and the [interactive dashboard](https://github.com/sensebeing/code-review-benchmark/blob/main/offline/analysis/benchmark_dashboard.html).


### Per-repository Core profile

| Repository (language) | Precision | Recall | F1 | Strict recall / F1 |
| --- | ---: | ---: | ---: | ---: |
| Grafana (Go) | 67.9% | 76.0% | 71.7% | 80.0% / 71.1% |
| Cal.com (TypeScript) | 59.6% | 83.8% | 69.7% | 91.4% / 74.4% |
| Sentry (Python) | 61.5% | 72.7% | 66.7% | 66.7% / 60.0% |
| Discourse (Ruby) | 58.0% | 76.3% | 65.9% | 75.7% / 65.1% |
| Keycloak (Java) | 50.0% | 68.0% | 57.6% | 65.0% / 52.0% |

### Recall by severity and category

| Severity | Recall | Category | Recall |
| --- | ---: | --- | ---: |
| High | 87.0% (47/54) | concurrency | 92.9% (13/14) |
| Medium | 78.0% (46/59) | data | 85.7% (6/7) |
| Critical | 66.7% (8/12) | perf | 83.3% (5/6) |
| Low | 57.6% (19/33) | api | 76.9% (10/13) |
|  |  | bug | 76.6% (72/94) |
|  |  | test_gap | 75.0% (3/4) |
|  |  | doc_defect | 66.7% (6/9) |
|  |  | security | 45.5% (5/11) |

### Unbound-topology convergence

Open-source repositories often have no module boundary documents or declarative topology constraints. The review prompt now detects that state and adds deterministic unbound-topology guidance: reviewers must focus on concrete runtime and contract defects, cannot invent internal layering rules, cannot demand speculative dual-track deletion, and must report test assertion defects at their primary cause. When boundary references or topology constraints exist, the guidance is omitted and the declared contract remains authoritative. This keeps the 50-PR benchmark comparable across repositories with different documentation maturity.
