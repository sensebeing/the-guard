# Preset catalog

**Core** is the default for a small project: Architecture Simplicity, Single Source of Truth,
Service Boundaries, and Code Organization. It establishes ownership, cohesion and safe simplification
without prescribing a domain model.

**Full** includes core plus Data Modeling, Database Query Efficiency, Event-driven Execution,
Process Adjunct Not Truth, No Silent Fallback, Frontend Product Surface, Agent Skill Prompt,
LLM Contract Quality, Tool Authoring, and Test Case Quality. Use it when those surfaces exist or
will be actively developed. Each rule has scope metadata; absence of a particular stack does not
create an obligation to introduce it.

Rule details live in the installed Markdown files and are the sole semantic definitions.
The static topology checker covers a deterministic subset; the independent reviewer evaluates
architectural responsibilities and invariants that cannot be inferred reliably from syntax.

**Distill** produces repository-specific rules from evidence, without forcing core/full.
**Hybrid** starts with core and adds only verified module topology and domain obligations.
Neither mode should promote copied implementation details, accidental coupling or obsolete
compatibility paths into permanent requirements.
