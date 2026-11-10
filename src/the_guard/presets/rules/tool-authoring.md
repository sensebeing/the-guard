---
id: guard.tool-authoring
title: Tool Authoring
manifest: AGENTS.md#engineering-quality-policy
---

# Tool Authoring

Each model-facing tool owns one domain resource and homogeneous authorization semantics. Do not fragment summary/detail/media reads or combine proposals requiring confirmation with direct writes and ownership correction. Retiring a tool or action removes exposure, routing, schemas, runtime policy, prompts and skills together; historical decoding may remain only for settlement, never live discovery.

Use a flat action-discriminated object schema without nested command envelopes, parallel arrays, unconstrained dictionaries, conditional schema logic or duplicated hand-maintained schemas. Items are cohesive bounded objects; reject extra fields. Keep same-named fields and enums consistent across actions. Expose intent parameters only, not worker tokens or server-known context. Server defaults may supply current-time snapshots, but pagination/idempotency bindings must be explicitly preserved across requests.

Keep wire schemas bounded and test their configured byte budgets. Field descriptions state only field contracts (at most 240 characters); put troubleshooting in structured errors. Reads support progressive disclosure with core facts, navigation and bounded summaries, then sections/cursors. Writes natively support bounded batches.

Return minimal authoritative projections without UI wrappers or obsolete objects. Preserve empty lists as known-empty; omit only absent optionals. Return native multimodal content. Validation errors reference current schema fields without echoing sensitive inputs or mutating signed arguments. Confirmation-required receipts explicitly yield the turn and identify the resume action and parameters.

Concurrent writes to the same entity serialize from baseline read through persistence using an entity-scoped lock or equivalent atomic owner mechanism; unrelated entities remain concurrent. Preserve snapshot/CAS protection without creating avoidable same-turn conflicts.
