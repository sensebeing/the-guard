---
id: guard.data-modeling
title: Data Modeling
manifest: AGENTS.md#engineering-quality-policy
---

# Data Modeling

Data structures have an explicit owner, stable semantics and only necessary fields. Do not make derived data a competing source of truth. Keep different lifecycle entities distinct through schema constraints and write ownership rather than pervasive read filters.

Retired tables are delete-only: do not add or preserve new dependencies on them. A replacement completes reader cutover, old-write freezing, reference migration and obsolete branch deletion together. Preserve identity, atomicity and concurrency safeguards; do not emulate retired structures with fabricated field mappings.

Cross-module fields have explicit contracts and contract tests. Distinguish missing, unknown, withdrawn and confirmed states; never fabricate status with defaults. Once replaced, legacy tables and intermediate ledgers receive no new functional patches, fields, reads or writes.
