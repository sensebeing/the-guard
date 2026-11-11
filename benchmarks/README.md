# GuardBench and ReviewBench

This repository ships two reproducible evaluation tracks. GuardBench is a seven-case, multi-service architectural benchmark with 12 violations across six corrupt commits and one clean change. ReviewBench records real open-source pull request comparisons. The Codex 6.1 runs use model gpt-6.1-sol with effort=medium; the public benchmark source is [withmartian/code-review-benchmark](https://github.com/withmartian/code-review-benchmark).

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
| **Corrupt-case total** | **12** | **0/6** | **0/6** | **0/6** | **4/6** | **6/6** | **6/6** | **6/6** | — |
| **Violation recall** | **12** | **0/12** | **0/12** | **0/12** | **5/12** | **11/12** | **7/12** | **12/12** | 0 (12/12 root-cause aligned) |

The static guard layer runs in about 40 ms with zero tokens. Noise and fix-direction results are: Ruff, Semgrep, code-review-graph, and guard check each 0; Claude Code raw 6 speculative findings (24%); Codex 6.1 raw 1 anti-architectural build_sha fix (case-6); and the-guard full 0, with 12/12 fixes aligned to root causes. The complete path attributes findings to guard rule IDs and had no clean-case false positives.

## Track 2 - Martian real open-source PRs

Martian's aggregate table covers 50 real PRs:

| Product | Precision | Recall | F1 | Type |
| --- | ---: | ---: | ---: | --- |
| Claude Code | 58.9% | 32.1% | 41.6% | Agent |
| Cursor BugBot | 51.9% | 32.9% | 40.3% | PR bot |
| CodeRabbit | 31.8% | 51.4% | 39.3% | PR bot |
| Greptile v4.1 | 34.9% | 42.9% | 38.5% | Graph PR bot |
| GitHub Copilot Review v2 | 31.3% | 42.9% | 36.2% | Platform review |
| Qodo Extended v2 / PR-Agent | 38.7% | 33.6% | 36.0% | PR agent |
| Devin Review | 34.9% | 37.1% | 36.0% | Autonomous agent |
| Sourcery | 26.5% | 35.0% | 30.2% | Refactoring bot |
| Kodus v2 | 20.2% | 46.4% | 28.1% | PR bot |

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

The deterministic track is fully reproducible with [benchmarks/guardbench.py](guardbench.py). The real-PR comparison source is the public [withmartian/code-review-benchmark](https://github.com/withmartian/code-review-benchmark).
