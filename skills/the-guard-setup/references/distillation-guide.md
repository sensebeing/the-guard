# Distilling a repository standard

Run `guard distill --repo-root . --json`. The report inventories languages, module directories,
owner documents, configured tooling, import relationships and bounded code samples. It is evidence,
not an architectural verdict, and does not write policy automatically.

Read representative entry points, owner repositories, consumers and tests from each important module.
Trace a read and a write end to end. Record who decides business facts, who owns persistence,
which layers may import which, and how asynchronous work settles. Distinguish observed dependencies
from allowed dependencies; a forbidden import rule is a design choice, not a discovery fact.

For hybrid, run `guard init --preset core` first. For pure distill, create the policy manifest and
rule Markdown files using the format in the project's README. Use one rule for each coherent
invariant, with explicit scope and a concrete review criterion. Each detail file needs `id`, `title`
and `manifest` frontmatter and a nonempty body. Avoid copied framework manuals, hardcoded incidental
file sizes or implementation-symbol inventories.

Run `guard topology sync` to install the factual module inventory. Confirm resource ownership before
adding `exclusive_sql_owners`; identify the replacing owner and current migration boundary before
adding `retired_tables`. Declare `forbidden_imports` or `module_boundaries` only for demonstrated
layer boundaries. Use narrow `forbidden_patterns` for concrete disallowed dependencies; prefer a
single owner contract over growing generic blacklists. Preserve existing declared constraints when
refreshing discovered modules.

Validate `guard check --policy-only`, then run topology against a real base revision. Existing findings
on untouched lines are grandfathered; new or changed violations are not. For a proposed new rule,
inspect both a violating and a legitimate example before marking it blocking. Report unresolved
owner ambiguity and ask for that decision rather than fabricating a topology.
