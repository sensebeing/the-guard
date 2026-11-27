"""Configuration and hooks preserve trust boundaries and identify the actual audited snapshot."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from the_guard import __version__
from benchmarks import guardbench
from benchmarks.guardbench import build_guardbench_repo, evaluate_deterministic_checks
from the_guard.cli import main, changed_files
from the_guard.config import GuardConfig, load_config, validate_config
from the_guard.git_helpers import GIT_REPO_ENV_VARS, read_repository_file
from the_guard.hooks import edit_guard, install_hooks, main_push_gate
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
from the_guard.review.contract import validate_review_output
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

    def test_shared_repository_reader_preserves_policy_errors(self):
        (self.root / "escape.md").symlink_to("/tmp/outside-guard-file")
        (self.root / "cycle.md").symlink_to("cycle.md")
        (self.root / "invalid.md").write_bytes(b"\xff")
        self.commit()
        for revision in (None, "HEAD"):
            for path in ("escape.md", "cycle.md", "invalid.md", "missing.md", "../unsafe.md"):
                with self.subTest(revision=revision, path=path):
                    with self.assertRaises(ValueError) as reader_error:
                        read_repository_file(self.root, path, revision=revision)
                    with self.assertRaises(PolicyError) as policy_error:
                        load_policy(self.root, revision=revision, config=GuardConfig(manifest_file=path))
                    self.assertEqual(str(reader_error.exception), str(policy_error.exception))

    def test_symlinked_configuration_cannot_supply_active_policy(self):
        self.write("settings/guard.json", '{"main_branch": "unreviewed"}')
        config = self.root / ".guard/config.json"
        config.unlink()
        config.symlink_to("../settings/guard.json")
        self.commit()
        for revision in (None, "HEAD"):
            with self.subTest(revision=revision), self.assertRaisesRegex(ValueError, "regular file"):
                load_config(self.root, revision=revision)
        with self.assertRaisesRegex(ValueError, "regular file"):
            collect_review_scope(self.root)

    def test_review_reads_legacy_import_rules_only_from_historical_config(self):
        historical = {
            "forbidden_imports": [{"from": "src/", "to": "storage"}],
            "module_boundaries": [{"scope_prefixes": ["tools/"], "forbidden_imports": ["private"],
                                    "message": "Keep the boundary"}],
        }
        canonical = {"forbidden_imports": [
            {"scope_prefixes": ["src/"], "imports": ["storage"]},
            {"scope_prefixes": ["tools/"], "imports": ["private"], "message": "Keep the boundary"},
        ]}
        self.write(".guard/config.json", json.dumps({"topology": historical}))
        self.commit()
        baseline = self.git("rev-parse", "HEAD")
        for revision in (None, baseline):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                load_config(self.root, revision=revision)
        self.assertEqual(load_config(self.root, revision=baseline, allow_historical=True).topology, canonical)

        self.write(".guard/config.json", json.dumps({"topology": canonical}))
        self.write("src/example.py", "value = 1\n")
        self.commit()
        scope = collect_review_scope(self.root, main_branch=baseline)
        self.assertIn(".guard/config.json", scope.reviewable_files)
        self.assertIn("src/example.py", build_review_prompt(self.root, scope))

    def test_edit_guard_external_paths_and_invalid_contract_edits(self):
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(
                edit_guard(self.root, json.dumps({"path": "/tmp/outside-guard-file"})),
                0,
            )
        self.assertEqual(errors.getvalue(), "")
        self.write(".guard/config.json", "{invalid")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                edit_guard(self.root, json.dumps({"path": ".guard/config.json"})), 2
            )
        self.git("restore", ".guard/config.json")
        self.write("AGENTS.md", "broken policy manifest\n")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                edit_guard(self.root, json.dumps({"path": "AGENTS.md"})), 2
            )
        self.git("restore", "AGENTS.md")
        config = json.loads((self.root / ".guard/config.json").read_text())
        config.setdefault("topology", {})["architecture_docs"] = ["docs/missing.md"]
        self.write(".guard/config.json", json.dumps(config))
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(
                edit_guard(self.root, json.dumps({"path": ".guard/config.json"})), 2
            )
        self.assertIn("missing.md", errors.getvalue())

    def test_architecture_docs_validate_repository_relative_paths(self):
        for docs in ("docs/a.md", ["../a.md"], ["/a.md"], ["C:\\a.md"], [12]):
            with self.subTest(docs=docs):
                self.write(".guard/config.json", json.dumps({"topology": {"architecture_docs": docs}}))
                with self.assertRaises(ValueError):
                    load_config(self.root)
        self.write(".guard/config.json", json.dumps({"topology": {"architecture_docs": ["docs/a.md"]}}))
        self.assertEqual(load_config(self.root).topology["architecture_docs"], ["docs/a.md"])

    def test_review_paths_and_retired_modules_validate_at_load(self):
        for name in ("operational_ledgers", "blueprint_globs"):
            for value in ("a.md", [""], ["/a.md"], ["C:\\a.md"], ["docs/../a.md"], [1]):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, name):
                    validate_config({"review": {name: value}})
        review = {"operational_ledgers": ["docs/log.md"], "blueprint_globs": ["docs/**/*.md"]}
        retired = {"path": "legacy", "retired": True}
        config = validate_config({"review": review, "topology": {"modules": [retired]}})
        self.assertEqual(config.review, review)
        self.assertEqual(config.topology["modules"], [retired])
        for modules in (
            [{**retired, "retired": 1}],
            [{**retired, "dependencies": ["active"]}, {"path": "active"}],
            [retired, {"path": "active", "dependencies": ["legacy"]}],
            [retired, {"path": "legacy/active"}],
        ):
            with self.subTest(modules=modules), self.assertRaisesRegex(ValueError, "[Rr]etired"):
                validate_config({"topology": {"modules": modules}})

    def test_review_rule_scope_globs_validation(self):
        valid = {"tool": ["**/tools/**", "tools/**"], "empty": []}
        config = validate_config({"review": {"rule_scope_globs": valid}})
        self.assertEqual(config.review["rule_scope_globs"], valid)
        for bad in (
            "**/tools/**",
            ["**/tools/**"],
            123,
            {"": ["a/**"]},
            {"   ": ["a/**"]},
            {"tool": "**/tools/**"},
            {"tool": [""]},
            {"tool": ["/abs/**"]},
            {"tool": ["C:\\abs"]},
            {"tool": ["../escape/**"]},
            {"tool": ["a\x00b"]},
            {"tool": [42]},
        ):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "rule_scope_globs"):
                validate_config({"review": {"rule_scope_globs": bad}})

    def test_check_and_edit_guard_reject_new_unregistered_source(self):
        self.write(".guard/config.json", json.dumps({"topology": {"enforce_module_dependencies": True}}))
        self.write("packages/new/main.py", "value = 1\n")
        self.commit()
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["check", "--repo-root", str(self.root), "--base", "HEAD"]), 0)
            self.write("packages/new/main.py", "value = 2\n")
            self.assertEqual(main(["check", "--repo-root", str(self.root), "--base", "HEAD"]), 2)
            self.assertEqual(edit_guard(self.root, json.dumps({"path": "packages/new/main.py"})), 2)

    def test_deleted_contract_docs_block_check_topology_and_push_before_exemption(self):
        self.write(".guard/config.json", json.dumps({"topology": {
            "architecture_docs": ["docs/blueprint.md"],
            "modules": [{"path": "packages/a", "boundary_docs": ["docs/boundary.md"]}]}}))
        self.write("docs/blueprint.md", "Root owner\n")
        self.write("docs/boundary.md", "Module owner\n")
        self.commit()
        base = self.git("rev-parse", "HEAD")
        for doc in ("docs/blueprint.md", "docs/boundary.md"):
            (self.root / doc).unlink()
        self.commit()
        head = self.git("rev-parse", "HEAD")
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["check", "--repo-root", str(self.root), "--base", base]), 2)
            self.assertEqual(main(["topology", "check", "--repo-root", str(self.root), "--base", base]), 2)
            with patch("the_guard.hooks.collect_review_scope") as scope:
                self.assertEqual(main_push_gate(self.root, f"refs/heads/topic {head} refs/heads/main {base}\n"), 2)
                scope.assert_not_called()
        self.assertIn("docs/blueprint.md", errors.getvalue())
        self.assertIn("docs/boundary.md", errors.getvalue())

    def test_push_checks_committed_snapshot_despite_dirty_contract_restoration(self):
        self.write(".guard/config.json", json.dumps({"topology": {"architecture_docs": ["docs/blueprint.md"]}}))
        self.write("docs/blueprint.md", "Owner boundary\n")
        self.commit()
        base = self.git("rev-parse", "HEAD")
        (self.root / "docs/blueprint.md").unlink()
        self.commit()
        head = self.git("rev-parse", "HEAD")
        self.write("docs/blueprint.md", "Uncommitted restoration\n")
        with (
            patch("the_guard.hooks.subprocess.run", wraps=subprocess.run) as calls,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(main_push_gate(self.root, f"refs/heads/topic {head} refs/heads/main {base}\n"), 2)
        self.assertFalse(
            any("worktree" in call.args[0] for call in calls.call_args_list)
        )
        self.assertIn("docs/blueprint.md", errors.getvalue())
        self.git("reset", "--hard", base)
        self.write("notes.txt", "Harmless committed documentation\n")
        self.commit()
        head = self.git("rev-parse", "HEAD")
        self.write(".guard/config.json", "invalid dirty JSON")
        (self.root / "docs/blueprint.md").unlink()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main_push_gate(self.root, f"refs/heads/topic {head} refs/heads/main {base}\n"), 0)
        worktrees = self.git("worktree", "list", "--porcelain")
        self.assertNotIn("guard-push-", worktrees)

    def test_check_blocks_new_stale_dependency_but_not_unchanged_legacy_debt(self):
        config = {"topology": {"enforce_module_dependencies": True, "modules": [
            {"path": "packages/a", "dependencies": ["packages/b"]},
            {"path": "packages/b", "dependencies": []}]}}
        self.write(".guard/config.json", json.dumps(config))
        self.write("packages/a/main.py", "from packages.b import value\n")
        self.write("packages/b/main.py", "value = 1\n")
        self.commit()
        base = self.git("rev-parse", "HEAD")
        self.write("packages/a/main.py", "value = 1\n")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["check", "--repo-root", str(self.root), "--base", base]), 2)
        self.assertIn("Stale dependency", output.getvalue())
        self.commit()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["check", "--repo-root", str(self.root), "--base", "HEAD"]), 0)
        config["topology"]["architecture_docs"] = ["docs/new-missing.md"]
        self.write(".guard/config.json", json.dumps(config))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["check", "--repo-root", str(self.root), "--base", "HEAD"]), 2)
        self.assertIn("docs/new-missing.md", output.getvalue())

    def test_reinitialization_preserves_governed_module_permissions(self):
        topology = {
            "layers": {"core": [], "plugin": ["core"]},
            "enforce_module_dependencies": True,
            "modules": [
                {"path": "packages/core", "layer": "core", "role": "shared state", "dependencies": [],
                 "boundary_docs": ["docs/architecture.md"]},
                {"path": "packages/plugin", "layer": "plugin", "role": "host adapter",
                 "dependencies": ["packages/core"], "boundary_docs": ["docs/plugin.md"]},
            ],
        }
        self.write(".guard/config.json", json.dumps({"topology": topology}))
        self.write("packages/core/index.ts", "import '../plugin/index.ts';")
        self.write("packages/plugin/index.ts", "export const plugin = 1;")
        self.write("packages/new/index.ts", "export const value = 1;")
        result = initialize(self.root, preset="full")
        self.assertEqual(result["unregistered_modules"], ["packages/new"])
        saved = load_config(self.root).topology
        for old, new in zip(topology["modules"], saved["modules"]):
            for key in ("path", "layer", "role", "dependencies", "boundary_docs"):
                self.assertEqual(old[key], new[key])
        # Both writers validate merged data before publishing any invalid configuration.
        previous = (self.root / ".guard/config.json").read_text()
        invalid = {**saved, "modules": [{"path": "../escape"}]}
        with patch("the_guard.init.merge_topology", return_value=(invalid, [])):
            with self.assertRaises(ValueError):
                initialize(self.root, preset="full")
        self.assertEqual((self.root / ".guard/config.json").read_text(), previous)
        with patch("the_guard.topology.sync.merge_topology", return_value=(invalid, [])):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["topology", "sync", "--repo-root", str(self.root)]), 2)
        self.assertEqual((self.root / ".guard/config.json").read_text(), previous)

    def test_module_schema_and_layer_ceiling_validation(self):
        base = {"layers": {"core": [], "plugin": ["core"]}, "enforce_module_dependencies": True,
                "modules": [{"path": "packages/core", "layer": "core", "dependencies": []},
                            {"path": "packages/host", "layer": "plugin", "dependencies": ["packages/core"]}]}
        cases = [
            ("enforce_module_dependencies", "yes"), ("layers", []),
            ("layers", {"": []}), ("layers", {"core": ["absent"]}),
            ("layers", {"core": "plugin"}),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.write(".guard/config.json", json.dumps({"topology": {**base, key: value}}))
                with self.assertRaises(ValueError):
                    load_config(self.root)
        for override in (
            {"path": "../core"}, {"path": "/core"}, {"path": "core/"}, {"path": "core//sub"},
            {"path": "core/./sub"}, {"path": "packages/host"},
            {"boundary_docs": ["../secrets"]}, {"boundary_docs": ["/tmp/doc"]},
            {"boundary_docs": ["C:\\outside"]}, {"languages": "Python"},
            {"dependencies": [42]}, {"dependencies": ["missing"]},
            {"dependencies": ["packages/core"]}, {"dependencies": ["packages/host"]},
            {"role": " "}, {"layer": "missing"}, {"layer": ""},
        ):
            with self.subTest(override=override):
                data = json.loads(json.dumps(base))
                data["modules"][0].update(override)
                self.write(".guard/config.json", json.dumps({"topology": data}))
                with self.assertRaises(ValueError):
                    load_config(self.root)
        del base["modules"][0]["layer"]
        self.write(".guard/config.json", json.dumps({"topology": base}))
        with self.assertRaisesRegex(ValueError, "requires a layer"):
            load_config(self.root)
        base["enforce_module_dependencies"] = False
        self.write(".guard/config.json", json.dumps({"topology": base}))
        self.assertEqual(load_config(self.root).topology, base)
        base["modules"][0].update(layer="core", dependencies=["packages/host"])
        self.write(".guard/config.json", json.dumps({"topology": base}))
        with self.assertRaisesRegex(ValueError, "must not depend"):
            load_config(self.root)

    def test_topology_inventory_actions_reject_diff_scope_flags(self):
        before = (self.root / ".guard/config.json").read_text()
        for action in ("sync", "audit"):
            for flags in (["--base", "main"], ["--file", "src/api.py"]):
                with self.subTest(action=action, flags=flags):
                    with contextlib.redirect_stderr(io.StringIO()) as err:
                        result = main(["topology", action, "--json", "--repo-root", str(self.root), *flags])
                    self.assertEqual(result, 2)
                    self.assertIn("does not accept --base or --file", err.getvalue())
                    self.assertEqual((self.root / ".guard/config.json").read_text(), before)

    def test_topology_audit_cli_full_scan_json_and_text_exit_codes(self):
        self.write("src/api.py", 'def get_item():\n    return "FOR UPDATE"\n')
        self.commit()
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        for output_args in (["--json"], []):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = main(["topology", "audit", "--repo-root", str(self.root), *output_args])
            self.assertEqual(code, 2)
            if output_args:
                report = json.loads(out.getvalue())
                self.assertFalse(report["passed"])
                self.assertEqual(len(report["findings"]), 1)
            else:
                self.assertIn("Topology audit: FAIL", out.getvalue())
                self.assertIn("src/api.py", out.getvalue())
        self.write("src/api.py", "value = 1\n")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["topology", "audit", "--repo-root", str(self.root), "--json"]), 0)
        self.assertTrue(json.loads(out.getvalue())["passed"])

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
                    validate_review_output(
                        {
                            "verdict": "GO",
                            "summary": "clean",
                            "blocking_findings": findings,
                            "reviewed_head_sha": "a" * 40,
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
        self.assertEqual(main_push_gate(self.root, update.replace(head, self.base, 1)), 0)
        hook = install_hooks(self.root)
        hook.write_text("# existing custom hook\n")
        with self.assertRaisesRegex(ValueError, "Refusing to overwrite existing hook"):
            install_hooks(self.root)
        self.assertEqual(hook.read_text(), "# existing custom hook\n")

    def test_managed_pre_push_uses_global_fallback_and_cleans_git_environment(self):
        hook = install_hooks(self.root)
        with tempfile.TemporaryDirectory() as outside:
            outside = Path(outside)
            foreign_git = outside / "foreign.git"
            subprocess.run(
                ["git", "init", "--bare", str(foreign_git)], check=True, capture_output=True,
            )
            home = outside / "test home"
            guard = home / ".local/bin/guard"
            guard.parent.mkdir(parents=True)
            guard.write_text(
                '#!/bin/sh\nenv > "$HOME/provider-env"\nprintf "%s\\n" "$@"\ncat\n',
                encoding="utf-8",
            )
            guard.chmod(0o755)
            env = {
                **dict.fromkeys(GIT_REPO_ENV_VARS, "inherited"),
                "GIT_DIR": str(foreign_git),
                "GIT_COMMON_DIR": str(foreign_git),
                "GIT_OBJECT_DIRECTORY": str(foreign_git / "objects"),
                "GIT_WORK_TREE": str(self.root.resolve()),
                "HOME": str(home),
                "PATH": "/usr/bin:/bin",
            }
            updates = "refs/heads/topic new refs/heads/main old\nrefs/tags/v1 new refs/tags/v1 zero\n"
            result = subprocess.run(
                [str(hook), "origin", "remote"], cwd=outside, env=env,
                input=updates, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                result.stdout, f"hook\nmain-push-gate\n--repo-root\n{self.root.resolve()}\n{updates}",
            )
            child_env = {
                line.partition("=")[0] for line in (home / "provider-env").read_text().splitlines()
            }
            self.assertFalse(child_env.intersection(GIT_REPO_ENV_VARS))

    def test_install_hooks_upgrades_only_the_legacy_managed_template(self):
        hook = install_hooks(self.root)
        hardened = hook.read_text(encoding="utf-8")
        hook.write_text(
            '#!/bin/sh\n# Managed by the-guard\n'
            'exec guard hook main-push-gate --repo-root "$(git rev-parse --show-toplevel)"\n',
            encoding="utf-8",
        )
        self.assertEqual(install_hooks(self.root), hook)
        self.assertEqual(hook.read_text(encoding="utf-8"), hardened)
        self.assertIn("unset " + " ".join(GIT_REPO_ENV_VARS), hardened)

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
                            {"scope_prefixes": ["src/ui/"], "imports": ["internal.storage"]}
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
            'def get_item():\n    return "SELECT * FROM items FOR UPDATE"\n',
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
        self.assertEqual(changed_files(self.root, "HEAD", head="HEAD"), [])

    def test_guardbench_deterministic_suite_matches_expected_rules(self):
        bench_root = self.root / "guardbench"
        result = evaluate_deterministic_checks(build_guardbench_repo(bench_root))
        self.assertTrue(result["passed"], result)
        self.assertEqual(len(result["cases"]), 7)
        self.assertTrue(all(case["passed"] for case in result["cases"]))

    def test_guardbench_review_enforces_verdicts_and_rule_ids(self):
        bench_root = self.root / "guardbench-review"

        def fake_review(repo, **_kwargs):
            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            case = next(case for case in guardbench.CASES if case.branch == branch)
            if branch == "case-3-retired-table-dual-read":
                raise RuntimeError("review unavailable")
            verdict = "GO" if branch in {"case-2-cross-service-sql-and-import", "case-7-clean-architecture-feature"} else "NO-GO"
            findings = [SimpleNamespace(rule_id=rule_id) for rule_id in case.expected_review_rule_ids]
            return SimpleNamespace(verdict=verdict, provider="codex", blocking_findings=findings)

        output = io.StringIO()
        with patch("benchmarks.guardbench.run_architecture_review", side_effect=fake_review), contextlib.redirect_stdout(output):
            code = guardbench.main(["--repo-dir", str(bench_root), "--with-review", "--json"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 2)
        self.assertFalse(result["passed"])
        self.assertFalse(result["review"]["passed"])
        cases = {case["branch"]: case for case in result["review"]["cases"]}
        self.assertTrue(all(case["expected_verdict"] == "NO-GO" for case in cases.values() if case["branch"] != "case-7-clean-architecture-feature"))
        self.assertEqual(cases["case-7-clean-architecture-feature"]["expected_verdict"], "GO")
        self.assertTrue(cases["case-1-duplicate-state-machine"]["passed"])
        self.assertFalse(cases["case-2-cross-service-sql-and-import"]["passed"])
        self.assertFalse(cases["case-3-retired-table-dual-read"]["passed"])

    def test_plugin_and_agent_manifests_match_package_metadata(self):
        repo_root = Path(__file__).resolve().parents[1]
        pyproject = tomllib.loads(
            (repo_root / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(pyproject["project"]["version"], __version__)
        claude_plugin = json.loads(
            (repo_root / ".claude-plugin/plugin.json").read_text(encoding="utf-8")
        )
        marketplace = json.loads(
            (repo_root / ".claude-plugin/marketplace.json").read_text(encoding="utf-8")
        )
        cursor_plugin = json.loads(
            (repo_root / ".cursor-plugin/plugin.json").read_text(encoding="utf-8")
        )
        for manifest in (claude_plugin, cursor_plugin, marketplace["plugins"][0]):
            self.assertEqual(manifest["name"], "the-guard")
            self.assertEqual(manifest["version"], __version__)
        for rel_skill in ("skills/the-guard", "skills/the-guard-setup"):
            skill_dir = repo_root / rel_skill
            self.assertTrue((skill_dir / "SKILL.md").is_file())
            openai_yaml = (skill_dir / "agents/openai.yaml").read_text(encoding="utf-8")
            self.assertIn("display_name:", openai_yaml)
            self.assertIn("default_prompt:", openai_yaml)

