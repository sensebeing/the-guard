---
id: guard.database-query-efficiency
title: Database Query Efficiency
manifest: AGENTS.md#engineering-quality-policy
---

# Database Query Efficiency

Evaluate the full request topology, cross-service fan-out and SQL cost, not just a local query. Within one request or action, reuse an identical read-only resource response; do not fetch it repeatedly without an intervening state change. Collections use bounded batch APIs rather than awaited single-entity calls in loops or comprehensions.

Keep normal reads free of write locks, read-time migrations and reverse-fetch recomputation. Transactions and locks cover local memory/SQL work only; never wait on RPC, queues or model calls while holding them. When computation or RPC runs outside a lock, settlement must compare the snapshot version or work identity atomically in the write transaction. Define deterministic conflict handling (merge, requeue or safe abort), never blindly overwrite concurrent writes.

Writes return the latest authoritative snapshot and version watermark directly (for example via RETURNING or a transaction-local snapshot). Consumers use that response instead of immediately issuing another GET. Do not call a heavyweight read for one field and discard its main response.
