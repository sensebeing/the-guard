# Contract: policy schema, severity, frontmatter and nonempty details must agree.
import tempfile
import textwrap
import unittest
from pathlib import Path

from the_guard.policy import PolicyError, load_policy


MANIFEST = """\
# Root

<!-- product-engineering-quality:start -->
```yaml
schema: product-engineering-quality/v1
rules:
  - id: peq.single-source-of-truth
    severity: block
    scope: [frontend, backend]
    summary: One owner.
    details: docs/engineering-quality/rules/single-source-of-truth.md
    checker: static-plus-review
```
<!-- product-engineering-quality:end -->
"""


class PolicyLoaderTest(unittest.TestCase):
    def make_repo(
        self,
        claude_text=MANIFEST,
        detail_id="peq.single-source-of-truth",
        detail_manifest="CLAUDE.md#product-engineering-quality-policy",
        create_detail=True,
        create_reviewer=False,
    ):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / "CLAUDE.md").write_text(claude_text, encoding="utf-8")
        detail = root / "docs/engineering-quality/rules/single-source-of-truth.md"
        detail.parent.mkdir(parents=True)
        if create_detail:
            detail.write_text(
                textwrap.dedent(
                    f"""\
                    ---
                    id: {detail_id}
                    title: Single Source
                    manifest: {detail_manifest}
                    ---

                    # Body
                    """
                ),
                encoding="utf-8",
            )
        if create_reviewer:
            reviewer = detail.parent / "reviewer-checklist.md"
            reviewer.write_text(
                textwrap.dedent(
                    """\
                    ---
                    id: reviewer-checklist
                    title: Product Quality Reviewer Checklist
                    manifest: CLAUDE.md#product-engineering-quality-policy
                    kind: reviewer-guidance
                    ---

                    # Body
                    """
                ),
                encoding="utf-8",
            )
        return tmp, root

    def test_loads_valid_policy(self):
        tmp, root = self.make_repo()
        self.addCleanup(tmp.cleanup)
        policy = load_policy(root)
        self.assertEqual(policy.schema, "product-engineering-quality/v1")
        self.assertEqual(policy.rules[0].id, "peq.single-source-of-truth")
        self.assertEqual(policy.rules[0].scope, ("frontend", "backend"))

    def test_missing_policy_block_fails(self):
        tmp, root = self.make_repo(claude_text="# no policy")
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(PolicyError, "policy manifest"):
            load_policy(root)

    def test_unsupported_schema_fails(self):
        text = MANIFEST.replace("product-engineering-quality/v1", "other/v1")
        tmp, root = self.make_repo(claude_text=text)
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(PolicyError, "Unsupported policy schema"):
            load_policy(root)

    def test_missing_required_rule_field_fails(self):
        text = MANIFEST.replace("    checker: static-plus-review\n", "")
        tmp, root = self.make_repo(claude_text=text)
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(PolicyError, "missing required fields"):
            load_policy(root)

    def test_duplicate_rule_id_fails(self):
        text = MANIFEST.replace(
            "```\n<!-- product-engineering-quality:end -->",
            textwrap.dedent(
                """\
                  - id: peq.single-source-of-truth
                    severity: block
                    scope: [frontend]
                    summary: Duplicate.
                    details: docs/engineering-quality/rules/single-source-of-truth.md
                    checker: static
                ```
                <!-- product-engineering-quality:end -->"""
            ),
        )
        tmp, root = self.make_repo(claude_text=text)
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(PolicyError, "Duplicate rule id"):
            load_policy(root)

    def test_duplicate_top_level_schema_fails(self):
        text = MANIFEST.replace(
            "schema: product-engineering-quality/v1",
            "schema: product-engineering-quality/v1\nschema: product-engineering-quality/v1",
        )
        tmp, root = self.make_repo(claude_text=text)
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(PolicyError, "Duplicate top-level field|Duplicate"):
            load_policy(root)

    def test_duplicate_rule_field_fails(self):
        text = MANIFEST.replace(
            "    severity: block",
            "    severity: block\n    severity: warn",
        )
        tmp, root = self.make_repo(claude_text=text)
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(PolicyError, "Duplicate rule field|Duplicate"):
            load_policy(root)

    def test_detail_id_mismatch_fails(self):
        tmp, root = self.make_repo(detail_id="peq.other")
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(PolicyError, "frontmatter id"):
            load_policy(root)

    def test_unsupported_top_level_field_after_rules_fails(self):
        text = MANIFEST.replace(
            "```\n<!-- product-engineering-quality:end -->",
            "unexpected: true\n```\n<!-- product-engineering-quality:end -->",
        )
        tmp, root = self.make_repo(claude_text=text)
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(PolicyError, "Unsupported|indent"):
            load_policy(root)

    def test_unknown_rule_field_fails(self):
        text = MANIFEST.replace(
            "    checker: static-plus-review",
            "    checker: static-plus-review\n    extra: nope",
        )
        tmp, root = self.make_repo(claude_text=text)
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(PolicyError, "Unknown rule field"):
            load_policy(root)

    def test_quoted_scope_items_are_normalized(self):
        text = MANIFEST.replace("scope: [frontend, backend]", 'scope: ["frontend", "backend"]')
        tmp, root = self.make_repo(claude_text=text)
        self.addCleanup(tmp.cleanup)
        policy = load_policy(root)
        self.assertEqual(policy.rules[0].scope, ("frontend", "backend"))

    def test_empty_scope_item_fails(self):
        text = MANIFEST.replace("scope: [frontend, backend]", "scope: [frontend, ]")
        tmp, root = self.make_repo(claude_text=text)
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(PolicyError, "empty"):
            load_policy(root)

    def test_details_path_traversal_fails(self):
        text = MANIFEST.replace(
            "docs/engineering-quality/rules/single-source-of-truth.md",
            "../outside.md",
        )
        tmp, root = self.make_repo(claude_text=text)
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(PolicyError, "inside the repository"):
            load_policy(root)

    def test_detail_manifest_mismatch_fails(self):
        tmp, root = self.make_repo(detail_manifest="other.md#policy")
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(PolicyError, "details manifest"):
            load_policy(root)

    def test_duplicate_frontmatter_id_fails(self):
        tmp, root = self.make_repo()
        self.addCleanup(tmp.cleanup)
        detail = root / "docs/engineering-quality/rules/single-source-of-truth.md"
        detail.write_text(
            textwrap.dedent(
                """\
                ---
                id: peq.single-source-of-truth
                id: peq.single-source-of-truth
                title: Single Source
                manifest: CLAUDE.md#product-engineering-quality-policy
                ---

                # Body
                """
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PolicyError, "Duplicate frontmatter"):
            load_policy(root)

    def test_duplicate_frontmatter_manifest_fails(self):
        tmp, root = self.make_repo()
        self.addCleanup(tmp.cleanup)
        detail = root / "docs/engineering-quality/rules/single-source-of-truth.md"
        detail.write_text(
            textwrap.dedent(
                """\
                ---
                id: peq.single-source-of-truth
                title: Single Source
                manifest: CLAUDE.md#product-engineering-quality-policy
                manifest: CLAUDE.md#product-engineering-quality-policy
                ---

                # Body
                """
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PolicyError, "Duplicate frontmatter"):
            load_policy(root)

    def test_missing_details_file_fails(self):
        tmp, root = self.make_repo(create_detail=False)
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(PolicyError, "details file does not exist"):
            load_policy(root)

    def test_unreferenced_reviewer_checklist_is_ignored(self):
        tmp, root = self.make_repo(create_reviewer=True)
        self.addCleanup(tmp.cleanup)
        policy = load_policy(root)
        self.assertEqual([rule.id for rule in policy.rules], ["peq.single-source-of-truth"])

    def test_quoted_scalar_value_is_normalized(self):
        text = MANIFEST.replace("summary: One owner.", 'summary: "One owner."')
        tmp, root = self.make_repo(claude_text=text)
        self.addCleanup(tmp.cleanup)
        policy = load_policy(root)
        self.assertEqual(policy.rules[0].summary, "One owner.")

    def test_malformed_quoted_scalar_fails(self):
        text = MANIFEST.replace("summary: One owner.", 'summary: "unterminated')
        tmp, root = self.make_repo(claude_text=text)
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(PolicyError, "Malformed quoted value|quote"):
            load_policy(root)

    def test_malformed_quoted_scope_item_fails(self):
        text = MANIFEST.replace("scope: [frontend, backend]", 'scope: ["frontend, backend]')
        tmp, root = self.make_repo(claude_text=text)
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(PolicyError, "Malformed quoted value|quote"):
            load_policy(root)

    def test_unknown_severity_fails(self):
        text = MANIFEST.replace("    severity: block", "    severity: bolck")
        tmp, root = self.make_repo(claude_text=text)
        self.addCleanup(tmp.cleanup)
        with self.assertRaisesRegex(PolicyError, "unsupported severity"):
            load_policy(root)


if __name__ == "__main__":
    unittest.main()
