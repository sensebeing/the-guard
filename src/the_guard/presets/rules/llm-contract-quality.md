---
id: guard.llm-contract-quality
title: LLM Input and Contract Quality
manifest: AGENTS.md#engineering-quality-policy
---

# LLM Input and Contract Quality

For model errors, inspect actual context injection, prompt consistency and output-schema clarity before adding consumer-side heuristics. Inputs distinguish confirmed facts, unconfirmed candidates and absent results using unambiguous structure.

The owner accepts valid representation variants, corrects provable representation errors and isolates parts that cannot safely be interpreted. Never guess identity, source or business facts. Preserve structured diagnostics for parse failures and refusals, explicitly handle them, and validate the real affected contract rather than manufacturing empty success.
