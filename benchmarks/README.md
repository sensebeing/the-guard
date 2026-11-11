# GuardBench and ReviewBench

This repository ships two reproducible evaluation tracks. **GuardBench** is a seven-case, multi-service architectural benchmark that exercises deterministic topology checks and contract-based review. **ReviewBench** records three real open-source pull request comparisons against their sampled golden findings.

Run the deterministic suite with Python 3.11+:

```sh
python3 benchmarks/guardbench.py
```

It completes in under 0.5 seconds and consumes 0 model tokens. Add `--with-review --provider codex` to run the full agent review against the selected provider. Use `--json` for machine-readable output.

## GuardBench

The cases contain 12 architectural violations across six corrupt commits and one clean architectural change.

| Case | Scenario | Ruff | `guard check` | Raw Codex review | `the-guard` full |
| --- | --- | --- | --- | --- | --- |
| 1 | UI duplicates refund state machine | Miss | Pass | Finds 2/2 | Finds 2/2, SSOT root-cause fix |
| 2 | Cross-service SQL and internal import | Miss | Finds 2/2 | Finds 2/2 after high token cost | Finds 3 findings, owner boundary fix |
| 3 | Retired table dual-read fallback | Miss | Finds data-modeling | Finds 1/2 | Finds 2/2, removes dual read |
| 4 | Retry loop, silent fallback, N+1 | Miss | Finds query-efficiency | Misses N+1; suggests keeping retry loop | Finds all; requires batch owner API |
| 5 | Write lock on read path, duplicate read | Miss | Finds query-efficiency | Suggests keeping write lock | Removes write lock and duplicate read |
| 6 | Nested tool envelope and process metadata truth | Miss | Review-only | Suggests exact `build_sha` matching (anti-architectural) | Removes process gate; restores flat schema |
| 7 | Clean owner API and retired-table cutover | Pass | Pass, 0 false positives | 352,664 input tokens | Pass, 0 false positives; 205,866 input tokens |

Across the six corrupt cases, Ruff catches 0/6 and 0/12 violations. `guard check` catches 4/6 cases in about 40ms with zero tokens. Raw Codex found 7/12 violations (58.3%) and used 352,664 input tokens on case 7; it also proposed anti-architectural fixes on cases 4, 5, and 6. The full `the-guard` path catches 12/12 violations, aligns every fix with the root cause, produces no case-7 false positive, and uses 205,866 input tokens on case 7 (41.6% fewer).

## ReviewBench

| Pull request | Sampled golden findings | Raw Codex | `the-guard` |
| --- | --- | --- | --- |
| `PierreJanineh/TechDebtMCP#135` | ReDoS warning is a known false positive; flag-stripping bug is true positive | Hits ReDoS false positive; misses silent flag bug; 445,432 input tokens | Matches 5/5 sampled findings, 0 known false positives; catches `guard.no-silent-fallback`; 258,950 input tokens (41.9% fewer) |
| `AA-Factory/aafactory-prototype#17` | Three test-contract findings | Finds 2/3 and points to wrong function | Finds 3/3 with `guard.test-case-quality` |
| `allan-mobley-jr/gimmes#468` | One stale-volume filter finding | Finds 1/1 | Finds 1/1 with owner-contract explanation |

Reproduce the deterministic track with `python3 benchmarks/guardbench.py`; see the source in [`guardbench.py`](guardbench.py).
