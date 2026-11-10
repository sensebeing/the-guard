"""Configuration and hooks preserve trust boundaries and identify the actual audited snapshot."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from the_guard.cli import main, changed_files
from the_guard.config import GuardConfig, load_config
from the_guard.hooks import install_hooks, main_push_gate
from the_guard.init import initialize
from the_guard.policy import load_policy, PolicyError
from the_guard.review.engine import run_architecture_review
from the_guard.review.prompt import build_review_prompt, _parse_numstat_entries
from the_guard.review.repository import (
    _classify_file_tier,
    collect_review_scope,
    is_reviewable_path,
    load_valid_receipt,
)
from the_guard.review.session import RECEIPT_SCHEMA
from the_guard.review.runner import _parse_findings
from the_guard.topology import run_topology_guard


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        initialize(self.root)
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("switch", "-c", "topic")

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=True
        ).stdout.strip()

    def write(self, path, text):
        dest = self.root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-m", "change")

    def test_production_example_loads_as_repository_config(self):
        example = Path(__file__).resolve().parents[1] / "examples/config.example.json"
        raw = example.read_text(encoding="utf-8")
        data = json.loads(raw)
        self.write(".guard/config.json", raw)
        config = load_config(self.root)
        self.assertEqual(config.main_branch, "main")
        self.assertEqual(config.module_doc_roots, tuple(data["module_doc_roots"]))
        self.assertEqual(config.review, data["review"])
        self.assertEqual(config.topology, data["topology"])
        self.assertGreater(len(config.topology["modules"]), 1)
        owner_path = "services/orders/storage/orders.py"
        consumer_path = "services/billing/queries.py"
        query = 'QUERY = "SELECT id FROM orders"\n'
        self.write(owner_path, query)
        self.write(consumer_path, query)
        self.assertEqual(run_topology_guard(self.root, [owner_path]), [])
        findings = run_topology_guard(self.root, [consumer_path])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rule_id, "guard.single-source-of-truth")

    def test_snapshot_policy_follows_manifest_symlink(self):
        config = json.loads((self.root / ".guard/config.json").read_text())
        config["manifest_file"] = "CLAUDE.md"
        self.write(".guard/config.json", json.dumps(config))
        self.commit()
        self.assertEqual(len(load_policy(self.root, revision="HEAD").rules), 4)

    def test_config_custom_detail_path_is_reviewable(self):
        config = GuardConfig(
            review={
                "extra_reviewable_paths": ["standards/**"],
                "reviewable_extensions": [".custom"],
                "service_config_roots": ["workers"],
            }
        )
        self.assertTrue(is_reviewable_path("standards/rule.md", config))
        self.assertTrue(is_reviewable_path("workers/api/schema.json", config))
        self.assertTrue(is_reviewable_path("module.custom", config))
        self.assertTrue(is_reviewable_path(".guard/config.json", config))
        self.assertFalse(is_reviewable_path("docs/guide.md", config))

    def test_malformed_findings_cannot_become_a_clean_verdict(self):
        for findings in (None, {}, [None], [{}], "not a list"):
            with self.subTest(findings=findings):
                with self.assertRaises(ValueError):
                    _parse_findings(
                        {
                            "verdict": "GO",
                            "summary": "clean",
                            "blocking_findings": findings,
                        }
                    )

    def test_unknown_receipt_is_rejected(self):
        self.write("src/main.py", "x = 1\n")
        self.commit()
        result = run_architecture_review(
            self.root,
            reviewer_fn=lambda prompt: {
                "verdict": "GO",
                "summary": "clean",
                "blocking_findings": [],
            },
        )
        receipt = self.root / ".git/the-guard-receipts" / f"{result.patch_digest}.json"
        data = json.loads(receipt.read_text())
        self.assertEqual(data["schema"], RECEIPT_SCHEMA)
        data["schema"] = "unknown-receipt/v1"
        receipt.write_text(json.dumps(data))
        self.assertIsNone(load_valid_receipt(self.root, result.patch_digest))

    def test_boundary_docs_and_config_are_snapshot_bound(self):
        self.write("src/api/README.md", "COMMITTED OWNER")
        self.write("src/api/main.py", "x = 1\n")
        self.commit()
        scope = collect_review_scope(self.root)
        self.write("src/api/README.md", "UNCOMMITTED OWNER")
        self.write(".guard/config.json", "invalid json")
        prompt = build_review_prompt(self.root, scope)
        self.assertIn("COMMITTED OWNER", prompt)
        self.assertNotIn("UNCOMMITTED OWNER", prompt)

    def test_main_push_checks_actual_range_and_preserves_hooks(self):
        self.write("src/api.py", "x = 1\n")
        self.commit()
        head = self.git("rev-parse", "HEAD")
        update = f"refs/heads/topic {head} refs/heads/main {self.base}\n"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main_push_gate(self.root, update), 2)
        run_architecture_review(
            self.root,
            reviewer_fn=lambda prompt: {
                "verdict": "GO",
                "summary": "clean",
                "blocking_findings": [],
            },
        )
        self.assertEqual(main_push_gate(self.root, update), 0)
        with self.assertRaises(ValueError):
            main_push_gate(self.root, update.replace(head, self.base, 1))
        hook = install_hooks(self.root)
        hook.write_text("# existing custom hook\n")
        with self.assertRaises(ValueError):
            install_hooks(self.root)
        self.assertEqual(hook.read_text(), "# existing custom hook\n")

    def test_config_invalid_constraints_fail_closed(self):
        self.write(
            ".guard/config.json",
            json.dumps({"topology": {"forbidden_imports": [{"from": "src/"}]}}),
        )
        with self.assertRaises(ValueError):
            load_config(self.root)

    def test_relative_and_from_import_boundary(self):
        self.write(
            ".guard/config.json",
            json.dumps(
                {
                    "topology": {
                        "forbidden_imports": [
                            {"from": "src/ui/", "to": "internal.storage"}
                        ]
                    }
                }
            ),
        )
        self.write("src/ui/main.py", "from internal import storage\n")
        self.assertEqual(len(run_topology_guard(self.root, ["src/ui/main.py"])), 1)

    def test_cli_init_distill_sync_and_checks(self):
        self.write(
            "src/api.py",
            "async def handler():\n    await api.get_item(1)\n    await api.get_item(1)\n",
        )
        for argv, code in [
            (["check", "--policy-only"], 0),
            (["distill", "--json"], 0),
            (["topology", "sync", "--json"], 0),
            (["check", "--base", "main", "--json"], 2),
            (["topology", "check", "--file", "src/api.py", "--base", "main"], 2),
        ]:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main([*argv, "--repo-root", str(self.root)]), code)
            self.assertNotEqual(out.getvalue(), "")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                main(["check", "--base", "absent", "--repo-root", str(self.root)]), 2
            )

    def test_missing_remote_base_uses_local_main_or_head(self):
        self.write("new.py", "value = 1\n")
        self.assertEqual(changed_files(self.root, None), ["new.py"])
        self.git("branch", "-D", "main")
        self.assertEqual(changed_files(self.root, None), ["new.py"])

    def test_explicit_invalid_base_fails_on_clean_repo(self):
        with self.assertRaisesRegex(ValueError, "Invalid Git base reference: absent"):
            changed_files(self.root, "absent")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(
                main(["check", "--base", "absent", "--repo-root", str(self.root)]), 2
            )
        self.assertIn("Invalid Git base reference: absent", err.getvalue())

    def test_configured_policy_directories_have_production_review_priority(self):
        config = GuardConfig(
            detail_dir="quality/rules", review={"reviewable_prefixes": ["contracts/"]}
        )
        for path in ("quality/rules/ownership.md", "contracts/policy.json"):
            self.assertTrue(is_reviewable_path(path, config))
            self.assertEqual(_classify_file_tier(path, config), 0)
        self.write(".guard/config.json", json.dumps({"detail_dir": "quality/rules"}))
        self.write("quality/rules/limits.json", '{"limit": 1}')
        self.commit()
        entries = _parse_numstat_entries(self.root, self.base, "HEAD")
        self.assertEqual(
            next(item[3] for item in entries if item[2] == "quality/rules/limits.json"),
            0,
        )

    def test_custom_exemption_tag_does_not_rewrite_source_literals(self):
        self.write(".guard/config.json", json.dumps({"exempt_tag": "custom-note"}))
        self.write(
            "src/api.py",
            'async def load():\n    await api.get_item("custom-note value")\n    await api.get_item("guard-exempt value")\n',
        )
        self.assertEqual(run_topology_guard(self.root, ["src/api.py"]), [])
        self.write(
            "src/api.py",
            "async def load():\n    await api.get_item(1)\n    await api.get_item(1)  # custom-note guard.database-query-efficiency: intentional consistency probe\n",
        )
        self.assertEqual(run_topology_guard(self.root, ["src/api.py"]), [])
        self.write(
            "src/api.swift",
            "func load() async {\n    await api.fetchItem(id: 1)\n    await api.fetchItem(id: 1) // custom-note guard.database-query-efficiency: intentional consistency probe\n}\n",
        )
        self.assertEqual(run_topology_guard(self.root, ["src/api.swift"]), [])

    def test_changed_files_excludes_deleted_paths_but_retains_new_paths(self):
        self.git("rm", "AGENTS.md")
        self.write("new.py", "value = 1\n")
        self.assertEqual(changed_files(self.root, "HEAD"), ["new.py"])
