"""Read paths remain pure and efficient; owner/retirement rules ratchet changed code only."""

import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import shutil
from pathlib import Path, PurePosixPath
from time import perf_counter

from the_guard.config import GuardConfig, load_config
from the_guard.cli import main
from the_guard.topology import (
    audit_topology,
    check_contract_drift,
    discover_topology,
    run_topology_guard,
    sync_topology,
)
from the_guard.topology.configured import check_configured
from the_guard.topology import audit as topology_audit
from the_guard.git_helpers import BaseFileReader, BaseFileEntry, _compute_changed_lines
from the_guard.topology.imports import (
    DETERMINISTIC_IMPORT_EXTENSIONS,
    _module_stem,
    extract_imports,
    owner_module,
    resolve_target_module,
)
from the_guard.topology.python import _check_python_file
from the_guard.topology.swift import _check_swift_file
from the_guard.topology.sync import merge_topology


class TopologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.root, check=True, capture_output=True, text=True
        ).stdout.strip()

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def config(self, topology):
        self.write(".guard/config.json", json.dumps({"topology": topology}))

    def check(self, text, base=None):
        return _check_python_file(
            "src/api.py",
            text,
            base,
            _compute_changed_lines(base, text) if base is not None else None,
        )

    def test_core_preset_only_emits_installed_lexical_rules(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HOME": home}):
            self.assertEqual(main(["init", "--repo-root", str(self.root), "--preset", "core"]), 0)
        path = "src/api.py"
        self.write(path, 'async def get_item(key):\n    await db.get_item(key)\n    await db.get_item(key)\n    return "SELECT * FROM items FOR UPDATE"\n')
        findings = run_topology_guard(self.root, [path])
        self.assertEqual({f.rule_id for f in findings}, {"guard.architecture-simplicity"})

    def test_retired_module_blocks_edits_and_inbound_imports(self):
        retired = "packages/legacy"
        self.config({"enforce_module_dependencies": True, "layers": {"domain": ["domain"]},
                     "modules": [{"path": retired, "retired": True, "boundary_docs": ["missing.md"]},
                                 {"path": "packages/api", "layer": "domain"}]})
        self.write(retired + "/notes.md", "Historical notes\n")
        self.write("packages/api/api.py", "from legacy.api import read\n")
        self.write("tools/use.py", "from legacy.api import read\n")
        paths = [retired + "/notes.md", "packages/api/api.py", "tools/use.py"]
        findings = run_topology_guard(self.root, paths)
        self.assertEqual({f.path for f in findings if "retired" in f.message.lower()}, set(paths))
        self.assertTrue(all(f.rule_id == "guard.service-boundaries" for f in findings))
        report = audit_topology(self.root)
        self.assertEqual(report["retired_modules"], [retired])
        self.assertEqual(report["unregistered_modules"], ["tools"])
        self.assertEqual(report["missing_boundary_docs"], [])
        self.git("add", ".")
        self.git("commit", "-m", "Record retired module")
        (self.root / paths[0]).unlink()
        self.assertEqual(run_topology_guard(self.root, [paths[0]]), [])
        self.assertEqual(check_contract_drift(self.root, [], base="HEAD"), [])
        self.assertTrue(next(m for m in sync_topology(self.root)["modules"] if m["path"] == retired)["retired"])

        (self.root / retired).mkdir(exist_ok=True)
        (self.root / paths[0]).symlink_to("../../packages/api/api.py")
        self.assertEqual([f.path for f in run_topology_guard(self.root, [paths[0]])], [paths[0]])

    def test_retired_imports_use_changed_spans_and_boundary_exemptions(self):
        self.config({"modules": [{"path": "packages/legacy", "retired": True}]})
        path = "packages/api/use.ts"
        existing_import = 'import { old } from "../legacy/old.js";'
        exemption = " // guard-exempt guard.service-boundaries: approved migration window"
        self.write(path, existing_import + "\nconst value = 1;\n")
        self.git("add", ".")
        self.git("commit", "-m", "Legacy inbound import")
        self.write(path, existing_import + "\nconst value = 2;\n")
        self.assertEqual(run_topology_guard(self.root, [path], base="HEAD"), [])

        import_text = 'import {\n  read,\n} from "../legacy/new.js";'
        self.write(path, import_text + exemption + "\nconst value = 2;\n")
        self.assertEqual(run_topology_guard(self.root, [path], base="HEAD"), [])
        self.write(path, import_text + "\nconst value = 2;\n")
        findings = run_topology_guard(self.root, [path], base="HEAD")
        self.assertEqual([(f.line, f.rule_id) for f in findings], [(1, "guard.service-boundaries")])

        retired_file = "packages/legacy/old.ts"
        self.write(retired_file, exemption.strip() + "\nexport const old = 1;\n")
        self.assertEqual(run_topology_guard(self.root, [retired_file], base="HEAD"), [])

    def test_contract_drift_grandfathers_untouched_retired_imports_only(self):
        path = "packages/api/use.ts"
        topology = {"enforce_module_dependencies": True, "modules": [
            {"path": "packages/api", "dependencies": ["packages/helper"]},
            {"path": "packages/helper"},
            {"path": "packages/legacy", "retired": True},
        ]}
        self.config(topology)
        old_import = 'import { old } from "../legacy/old.js";'
        self.write(path, old_import + "\nconst value = 1;\n")
        self.write("packages/helper/index.ts", "export const helper = 1;\n")
        self.git("add", ".")
        self.git("commit", "-m", "Baseline with retired import and unused permission")
        topology["modules"][0]["dependencies"] = []
        self.config(topology)
        self.write(path, "// Shift the existing import\n" + old_import + "\nconst value = 2;\n")
        changed = [".guard/config.json", path]
        self.assertEqual(check_contract_drift(self.root, changed, base="HEAD"), [])

        new_import = 'import { read } from "../legacy/new.js";'
        self.write(path, new_import + "\n" + old_import + "\nconst value = 2;\n")
        findings = check_contract_drift(self.root, changed, base="HEAD")
        self.assertEqual([(f.path, f.line) for f in findings], [(path, 1)])

    def test_contract_drift_checks_unchanged_callers_outside_active_modules_on_retirement(self):
        retired = "packages/legacy"
        self.config({"modules": [{"path": retired}]})
        callers = ["scripts/use.py", "tools/use.py"]
        for path in callers:
            self.write(path, "import legacy.api\n")
        self.write("tests/use.py", "import legacy.api  # guard-exempt guard.service-boundaries: approved fixture\n")
        self.write(retired + "/api.py", "def read():\n    return 1\n")
        # A gitlink has no root-repository source blob, even when its submodule is declared.
        gitlink = "vendor/uninitialized.py"
        self.write(".gitmodules", f'[submodule "vendor"]\npath = {gitlink}\nurl = ../vendor\n')
        self.git("add", ".")
        commit = self.git("commit-tree", self.git("write-tree"), "-m", "Submodule snapshot")
        self.git("update-index", "--add", "--cacheinfo", f"160000,{commit},{gitlink}")
        self.git("commit", "-m", "Record active module and callers")
        base = self.git("rev-parse", "HEAD")

        self.config({"modules": [{"path": retired, "retired": True}]})
        changed = [".guard/config.json"]
        findings = check_contract_drift(self.root, changed, base=base)
        self.assertEqual([(f.path, f.line) for f in findings], [(path, 1) for path in callers])
        self.git("add", ".guard/config.json")
        self.git("commit", "-m", "Retire module")

        self.write(callers[0], "pass\n")
        self.assertTrue(self.git("ls-tree", "-r", "HEAD", "--", gitlink).startswith("160000 commit "))
        self.assertFalse((self.root / gitlink / ".git").exists())
        findings = check_contract_drift(self.root, changed, base=base, revision="HEAD")
        self.assertEqual([(f.path, f.line) for f in findings], [(path, 1) for path in callers])

    def test_governed_inventory_merge_drops_dependencies_on_retired_modules(self):
        active = "packages/api"
        retired = "packages/legacy"
        topology = {"enforce_module_dependencies": True, "modules": [
            {"path": active, "dependencies": [retired]},
            {"path": retired, "retired": True},
        ]}
        discovered = [{"path": active, "boundary_docs": [], "dependencies": [retired]},
                      {"path": retired, "retired": True}]
        merged, _ = merge_topology(topology, discovered)
        self.assertEqual(merged["modules"][0]["dependencies"], [])
        self.assertTrue(merged["modules"][1]["retired"])

    def test_file_module_resolution_and_root_disambiguation(self):
        modules = ["src/the_guard/config.py", "src/the_guard/review", "packages/core/config.ts"]
        for spec in ("the_guard.config.load_config", "src.the_guard.config", "../config", "../config.py"):
            with self.subTest(spec=spec):
                self.assertEqual(resolve_target_module("src/the_guard/review/main.py", spec, modules),
                                 "src/the_guard/config.py")
        self.assertEqual(resolve_target_module("packages/core/nested/a.ts", "../config.js", modules),
                         "packages/core/config.ts")
        self.assertEqual(resolve_target_module("packages/plugin/a.ts", "@scope/core/config.js", modules),
                         "packages/core/config.ts")
        self.assertEqual(owner_module("src/the_guard/config.py", modules), "src/the_guard/config.py")
        self.assertIsNone(owner_module("src/the_guard/config.pyx", modules))
        imports = extract_imports("services/billing/nested/main.py", "from ..api import value\n")
        specs = {spec for _, _, spec in imports}
        self.assertIn("services.billing.api", specs)
        self.assertIn("billing.api", specs)
        targets = {resolve_target_module("services/billing/nested/main.py", spec,
                   ["services/billing/nested", "services/billing", "packages/billing"]) for spec in specs}
        self.assertEqual(targets, {None, "services/billing"})
        self.assertEqual(extract_imports("packages/plugin/a.cjs", "require('../core/config.js',)"),
                         [(1, 1, "../core/config.js")])

    def test_package_initializers_and_root_file_aliases_enforce_layers(self):
        modules = ["src/the_guard/__init__.py", "src/the_guard/config.py", "src/config.py"]
        for spec in ("the_guard", "the_guard.load_config", "src.the_guard", "src.the_guard.load_config"):
            self.assertEqual(resolve_target_module("src/the_guard/config.py", spec, modules),
                             "src/the_guard/__init__.py")
        self.assertEqual(resolve_target_module("src/the_guard/__init__.py", "the_guard.config.load_config", modules),
                         "src/the_guard/config.py")
        self.assertEqual(resolve_target_module("src/the_guard/config.py", "config", modules), "src/config.py")
        self.assertIsNone(resolve_target_module("src/other.py", "config", ["src/config.py", "packages/config.py"]))
        topology = {"layers": {"foundation": ["foundation"], "surface": ["foundation"]},
                    "enforce_module_dependencies": True, "modules": [
                        {"path": modules[0], "layer": "surface", "dependencies": [modules[1]]},
                        {"path": modules[1], "layer": "foundation", "dependencies": []}]}
        self.config(topology)
        self.write(modules[0], "from .config import load_config\n")
        self.write(modules[1], "from the_guard import load_config\n")
        findings = run_topology_guard(self.root, [modules[1]], config=load_config(self.root))
        self.assertTrue(any("layer 'surface'" in finding.message for finding in findings), findings)
        inventory = {m["path"]: m for m in discover_topology(self.root)["modules"]}
        self.assertEqual(inventory[modules[1]]["dependencies"], [modules[0]])
        self.assertEqual(audit_topology(self.root)["stale_dependencies"], [])

    def test_unregistered_ownership_ratchets_whole_file_but_exempts_line_one(self):
        cfg = GuardConfig(topology={"enforce_module_dependencies": True})
        path, text = "packages/new/main.py", "value = 1\nvalue = 2\n"
        self.assertEqual(check_configured(path, text, text, set(), cfg), [])
        findings = check_configured(path, text, text, {2}, cfg)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, 1)
        exempt = "# guard-exempt guard.service-boundaries: fixture ownership\nvalue = 2\n"
        self.assertEqual(check_configured(path, exempt, None, {2}, cfg), [])
        self.assertEqual(check_configured("tests/main.py", text, None, None, cfg), [])

    def test_contract_drift_preserves_legacy_and_blocks_last_import_removal(self):
        self.config({"enforce_module_dependencies": True, "architecture_docs": ["docs/old.md"],
            "modules": [{"path": "packages/a", "dependencies": ["packages/b", "packages/c"]},
                        {"path": "packages/b", "dependencies": []},
                        {"path": "packages/c", "dependencies": []}]})
        self.write("packages/a/main.ts", "import '../b/main.js';\n")
        self.write("packages/a/keep.ts", "export const value = 1;\n")
        self.write("packages/b/main.ts", "export const value = 1;\n")
        self.write("packages/c/main.ts", "export const value = 1;\n")
        self.git("add", ".")
        self.git("commit", "-m", "legacy debt with one live edge")
        self.assertEqual(check_contract_drift(self.root, ["packages/a/main.ts"], base="HEAD"), [])
        self.write("packages/a/keep.ts", "export const value = 2;\n")
        self.assertEqual(check_contract_drift(self.root, ["packages/a/keep.ts"], base="HEAD"), [])
        (self.root / "packages/a/main.ts").unlink()
        (self.root / "packages/a/keep.ts").unlink()
        findings = check_contract_drift(self.root, [], base="HEAD")
        self.assertEqual(len(findings), 1)
        self.assertIn("packages/b", findings[0].message)
        self.assertNotIn("packages/c", findings[0].message)
        self.assertEqual(findings[0].rule_id, "guard.single-source-of-truth")
        self.assertTrue(check_contract_drift(self.root, [], base="absent-base"))

    def _dependency_fixture(self, *, dependencies=("packages/b",)):
        topology = {
            "enforce_module_dependencies": True,
            "modules": [
                {"path": "packages/a", "dependencies": list(dependencies)},
                {"path": "packages/b"},
            ],
        }
        self.config(topology)
        self.write("packages/a/main.py", "from packages.b.api import value\n")
        self.write("packages/a/z_unchanged.py", "other = 1\n")
        self.write("packages/b/api.py", "value = 1\n")
        self.git("add", ".")
        self.git("commit", "-m", "dependency baseline")
        return topology, self.git("rev-parse", "HEAD")

    def test_contract_drift_import_retained_requires_no_unchanged_inventory(self):
        _, base = self._dependency_fixture()
        path = "packages/a/main.py"
        self.write(path, "from packages.b.api import value\nother = 1\n")
        self.git("commit", "-am", "retain import")
        with (
            patch.object(topology_audit, "source_files", wraps=topology_audit.source_files) as walk,
            patch.object(topology_audit, "extract_imports", wraps=extract_imports) as imports,
            patch.object(BaseFileReader, "preload", autospec=True, side_effect=BaseFileReader.preload) as preload,
        ):
            for revision in (None, "HEAD"):
                self.assertEqual(check_contract_drift(self.root, [path], base=base, revision=revision), [])
        walk.assert_not_called()
        self.assertEqual({call.args[0] for call in imports.call_args_list}, {path})
        self.assertTrue(all(set(call.args[1]) <= {path} for call in preload.call_args_list))

    def test_contract_drift_finds_omitted_changed_path_in_both_modes(self):
        _, base = self._dependency_fixture()
        self.write("packages/a/main.py", "value = 1\n")
        self.git("commit", "-am", "remove last import")
        for revision in (None, "HEAD"):
            with self.subTest(revision=revision):
                findings = check_contract_drift(self.root, [], base=base, revision=revision)
                self.assertEqual([f.message for f in findings], [
                    "Stale dependency 'packages/a' -> 'packages/b'; remove the unused declared dependency."
                ])

    def test_contract_drift_stops_at_unchanged_import(self):
        _, base = self._dependency_fixture()
        self.write("packages/a/another.py", "from packages.b.api import value\n")
        self.git("add", ".")
        self.git("commit", "-m", "second import")
        base = self.git("rev-parse", "HEAD")
        self.write("packages/a/main.py", "value = 1\n")
        self.git("commit", "-am", "remove one import")
        with patch.object(topology_audit, "extract_imports", wraps=extract_imports) as imports:
            result = check_contract_drift(self.root, [], base=base, revision="HEAD")
        self.assertEqual(result, [])
        scanned = [call.args[0] for call in imports.call_args_list]
        self.assertEqual(scanned.count("packages/a/another.py"), 1)
        self.assertNotIn("packages/a/z_unchanged.py", scanned)

    def test_contract_drift_counts_an_import_in_an_untracked_file(self):
        _, base = self._dependency_fixture()
        self.write("packages/a/main.py", "value = 1\n")
        self.assertTrue(check_contract_drift(self.root, [], base=base))
        self.write("packages/a/untracked.py", "from packages.b.api import value\n")
        self.assertEqual(check_contract_drift(self.root, [], base=base), [])

    def test_contract_drift_accounts_for_git_index_flags(self):
        _, base = self._dependency_fixture()
        path = "packages/a/main.py"
        for flag in ("assume-unchanged", "skip-worktree"):
            with self.subTest(flag=flag):
                self.git("update-index", "--" + flag, path)
                self.write(path, "value = 1\n")
                findings = check_contract_drift(self.root, [], base=base)
                self.assertEqual([f.message for f in findings], [
                    "Stale dependency 'packages/a' -> 'packages/b'; remove the unused declared dependency."
                ])
                self.write(path, "from packages.b.api import value\n")
                self.git("update-index", "--no-" + flag, path)

    def test_contract_drift_uses_baseline_coverage_for_tracked_generated_source(self):
        self._dependency_fixture()
        self.git("rm", "packages/a/main.py")
        self.write("packages/a/build/generated.py", "from packages.b.api import value\n")
        self.git("add", ".")
        self.git("commit", "-m", "baseline includes a tracked generated source")
        base = self.git("rev-parse", "HEAD")
        path = "packages/a/z_unchanged.py"
        self.write(path, "other = 2\n")
        findings = check_contract_drift(self.root, [path], base=base)
        self.assertEqual([f.message for f in findings], [
            "Stale dependency 'packages/a' -> 'packages/b'; remove the unused declared dependency."
        ])

    def test_contract_drift_reads_worktree_sources_transformed_by_git(self):
        self._dependency_fixture()
        self.write(".gitattributes", "packages/a/main.py filter=normalize-source\n")
        self.git("config", "filter.normalize-source.clean",
                 "sed 's/^value = 1$/from packages.b.api import value/'")
        self.git("add", ".gitattributes")
        self.git("commit", "-m", "baseline with source filter")
        base = self.git("rev-parse", "HEAD")
        self.write("packages/a/main.py", "value = 1\n")
        self.assertEqual(self.git("diff", "--name-only", base), "")
        self.assertEqual([f.message for f in check_contract_drift(self.root, [], base=base)], [
            "Stale dependency 'packages/a' -> 'packages/b'; remove the unused declared dependency."
        ])

    def test_contract_drift_reports_new_unused_dependency(self):
        topology, base = self._dependency_fixture(dependencies=())
        topology["modules"][0]["dependencies"] = ["packages/b"]
        self.config(topology)
        self.write("packages/a/main.py", "value = 1\n")
        findings = check_contract_drift(self.root, [], base=base)
        self.assertEqual([f.message for f in findings], [
            "Stale dependency 'packages/a' -> 'packages/b'; remove the unused declared dependency."
        ])

    def test_contract_drift_unknown_text_on_either_side_exempts_module(self):
        _, base = self._dependency_fixture()
        self.write("packages/a/first.swift", "let value = 1\n")
        self.write("packages/a/main.py", "value = 1\n")
        self.git("add", ".")
        self.git("commit", "-m", "replace import with Swift source")
        with patch.object(topology_audit, "extract_imports", wraps=extract_imports) as imports:
            findings = check_contract_drift(self.root, ["packages/a/main.py"], base=base, revision="HEAD")
        self.assertEqual(findings, [])
        self.assertNotIn("packages/a/z_unchanged.py", [call.args[0] for call in imports.call_args_list])

        self.git("checkout", "--detach", base)
        self.write("packages/a/first.swift", "let value = 1\n")
        self.git("add", ".")
        self.git("commit", "-m", "mixed-language baseline")
        base = self.git("rev-parse", "HEAD")
        self.git("rm", "packages/a/first.swift")
        self.write("packages/a/main.py", "value = 1\n")
        self.git("commit", "-am", "remove Swift source and import")
        self.assertEqual(check_contract_drift(self.root, [], base=base, revision="HEAD"), [])

    def test_contract_drift_falls_back_when_resolution_changes(self):
        topology, base = self._dependency_fixture()
        path = "packages/a/main.py"
        self.write(path, "value = 1\n")
        for setting in ("module paths", "module doc roots", "submodules"):
            with self.subTest(setting=setting):
                if setting == "module paths":
                    self.config({**topology, "modules": [*topology["modules"], {"path": "packages/c"}]})
                    self.write("packages/c/api.py", "value = 1\n")
                elif setting == "module doc roots":
                    self.write(".guard/config.json", json.dumps({
                        "topology": topology, "module_doc_roots": ["modules"],
                    }))
                else:
                    self.config(topology)
                    self.write(".gitmodules", '[submodule "vendor"]\npath = vendor\n')
                with patch.object(
                    topology_audit, "_dependency_evidence", wraps=topology_audit._dependency_evidence
                ) as evidence:
                    findings = check_contract_drift(self.root, [path], base=base)
                self.assertEqual([f.message for f in findings], [
                    "Stale dependency 'packages/a' -> 'packages/b'; remove the unused declared dependency."
                ])
                self.assertEqual(evidence.call_count, 2)

    def test_contract_drift_recognizes_nested_modules_in_snapshot(self):
        self.config({
            "modules": [
                {"path": "packages"},
                {"path": "packages/cli"},
            ],
        })
        self.write("packages/index.ts", "export const value = 1;\n")
        self.write("packages/cli/index.ts", "export const value = 2;\n")
        self.git("add", ".")
        self.git("commit", "-m", "add nested modules")

        self.assertEqual(
            check_contract_drift(
                self.root, [".guard/config.json"], base="HEAD", revision="HEAD"
            ),
            [],
        )

    def test_missing_modules_ratchet_snapshot_and_sync_prunes_incoming_edges(self):
        topology = {
            "enforce_module_dependencies": True,
            "modules": [
                {"path": "packages/a", "dependencies": ["packages/b"]},
                {"path": "packages/b", "dependencies": []},
            ],
        }
        self.config(topology)
        self.write("packages/a/main.ts", "import '../b/main.js';\n")
        self.write("packages/b/main.ts", "export const value = 1;\n")
        self.git("add", ".")
        self.git("commit", "-m", "baseline")
        base = self.git("rev-parse", "HEAD")
        shutil.rmtree(self.root / "packages/b")
        report = audit_topology(self.root)
        self.assertEqual(report["missing_modules"], ["packages/b"])
        self.assertFalse(report["passed"])
        findings = check_contract_drift(self.root, [], base=base)
        self.assertTrue(
            any("Declared module 'packages/b'" in f.message for f in findings)
        )
        self.git("add", ".")
        self.git("commit", "-m", "delete module")
        self.write("packages/b/main.ts", "restored only in dirty tree\n")
        self.assertTrue(
            any(
                "Declared module 'packages/b'" in f.message
                for f in check_contract_drift(self.root, [], base=base, revision="HEAD")
            )
        )
        shutil.rmtree(self.root / "packages/b")
        result = sync_topology(self.root)
        self.assertEqual([m["path"] for m in result["modules"]], ["packages/a"])
        self.assertEqual(result["modules"][0]["dependencies"], [])
        topology["modules"].append({"path": "packages/phantom", "dependencies": []})
        self.config(topology)
        self.assertTrue(
            any(
                "Declared module 'packages/phantom'" in f.message
                for f in check_contract_drift(
                    self.root, [".guard/config.json"], base=base
                )
            )
        )

    def test_new_governed_modules_require_roles_and_empty_candidates_do_not_scan(self):
        topology = {
            "enforce_module_dependencies": True,
            "layers": {"core": []},
            "modules": [{"path": "packages/a", "layer": "core", "dependencies": []}],
        }
        self.config(topology)
        self.write("packages/a/main.ts", "export const value = 1;\n")
        self.git("add", ".")
        self.git("commit", "-m", "governed baseline")
        with (
            patch(
                "the_guard.topology.audit.source_files",
                side_effect=AssertionError("unexpected scan"),
            ),
            patch(
                "the_guard.topology.audit._dependency_evidence",
                side_effect=AssertionError("unexpected evidence"),
            ),
        ):
            self.assertEqual(check_contract_drift(self.root, [], base="HEAD"), [])
            self.assertEqual(check_contract_drift(self.root, [], base="HEAD", revision="HEAD"), [])
        topology["modules"].append(
            {"path": "packages/new", "layer": "core", "dependencies": []}
        )
        self.write("packages/new/main.ts", "export const value = 1;\n")
        self.config(topology)
        findings = check_contract_drift(self.root, [".guard/config.json"], base="HEAD")
        self.assertTrue(
            any("requires a nonempty 'role'" in f.message for f in findings)
        )
        topology["modules"][-1]["role"] = "Own new values"
        self.config(topology)
        self.assertEqual(
            check_contract_drift(self.root, [".guard/config.json"], base="HEAD"), []
        )

    def test_tightened_contract_rechecks_unchanged_owned_production_sources(self):
        for tightening in (
            "dependencies",
            "layer",
            "layers",
            "enforcement",
            "enable_layers",
        ):
            with self.subTest(tightening=tightening):
                topology = {
                    "enforce_module_dependencies": tightening == "dependencies",
                    "layers": {"core": ["core", "host"], "host": ["core", "host"]},
                    "modules": [
                        {
                            "path": "packages/a",
                            "layer": "core",
                            "dependencies": ["packages/b"],
                        },
                        {"path": "packages/b", "layer": "host", "dependencies": []},
                    ],
                }
                if tightening != "dependencies":
                    topology["modules"][0]["dependencies"] = []
                if tightening == "enable_layers":
                    topology.pop("layers")
                self.config(topology)
                self.write("packages/a/main.ts", "import '../b/main.js';\n")
                self.write("packages/b/main.ts", "export const value = 1;\n")
                self.git("add", ".")
                self.git("commit", "--allow-empty", "-m", "baseline " + tightening)
                if tightening == "dependencies":
                    topology["modules"][0]["dependencies"] = []
                elif tightening == "layer":
                    topology["layers"]["isolated"] = []
                    topology["modules"][0]["layer"] = "isolated"
                    topology["modules"][0]["dependencies"] = []
                elif tightening in {"layers", "enable_layers"}:
                    topology["layers"] = {"core": [], "host": ["core", "host"]}
                    topology["modules"][0]["dependencies"] = []
                else:
                    topology["enforce_module_dependencies"] = True
                self.config(topology)
                findings = check_contract_drift(
                    self.root, [".guard/config.json"], base="HEAD"
                )
                self.assertTrue(
                    any(f.path == "packages/a/main.ts" for f in findings), findings
                )

    def test_topology_snapshot_ignores_dirty_source_and_dirty_symlink(self):
        self.config(
            {"forbidden_imports": [{"scope_prefixes": ["packages/a"], "imports": ["forbidden"]}]}
        )
        self.write("packages/a/main.ts", "export const value = 1;\n")
        self.git("add", ".")
        self.git("commit", "-m", "baseline")
        base = self.git("rev-parse", "HEAD")
        self.write("packages/a/main.ts", "import 'forbidden';\n")
        self.git("add", ".")
        self.git("commit", "-m", "violation")
        (self.root / "packages/a/main.ts").unlink()
        (self.root / "packages/a/main.ts").symlink_to("/tmp/outside-guard-source")
        findings = run_topology_guard(
            self.root, ["packages/a/main.ts"], base=base, revision="HEAD"
        )
        self.assertEqual(len(findings), 1)
        self.assertIn("forbidden", findings[0].message)

    def test_tightening_checks_changed_files_and_callers_without_unrelated_debt(self):
        topology = {
            "layers": {"core": ["host"], "host": [], "isolated": []},
            "enforce_module_dependencies": False,
            "forbidden_patterns": [
                {"literal_or_regex": "legacy_debt", "scope_prefixes": ["packages/"]}
            ],
            "modules": [
                {"path": "packages/a", "layer": "core", "dependencies": []},
                {"path": "packages/b", "layer": "host", "dependencies": []},
            ],
        }
        self.config(topology)
        self.write(
            "packages/a/main.ts", "import '../b/main.js';\nconst legacy_debt = 1;\n"
        )
        self.write("packages/b/main.ts", "export const value = 1;\n")
        self.git("add", ".")
        self.git("commit", "-m", "permissive baseline with unrelated debt")
        topology["modules"][1]["layer"] = "isolated"
        self.config(topology)
        self.write(
            "packages/a/main.ts",
            "import '../b/main.js';\nconst legacy_debt = 1;\nconst edited = 1;\n",
        )
        findings = check_contract_drift(
            self.root, [".guard/config.json", "packages/a/main.ts"], base="HEAD"
        )
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0].path, "packages/a/main.ts")
        self.assertEqual(findings[0].line, 1)
        self.assertIn("Layer", findings[0].message)
        self.assertNotIn("legacy_debt", findings[0].message)

    def test_snapshot_metadata_is_immutable_and_unavailable_reads_fail_closed(self):
        self.config(
            {"forbidden_imports": [{"scope_prefixes": ["packages/a"], "imports": ["forbidden"]}]}
        )
        self.write("packages/a/main.ts", "export const value = 1;\n")
        self.git("add", ".")
        self.git("commit", "-m", "baseline")
        base = self.git("rev-parse", "HEAD")
        self.write("packages/a/main.ts", "import 'forbidden';\n")
        self.git("add", ".")
        self.git("commit", "-m", "forbidden import")
        self.write(".gitmodules", '[submodule "fake"]\npath = packages/a\n')
        self.assertEqual(
            len(
                run_topology_guard(
                    self.root, ["packages/a/main.ts"], base=base, revision="HEAD"
                )
            ),
            1,
        )
        with patch.object(
            BaseFileReader,
            "_read_entry",
            return_value=BaseFileEntry("unavailable", detail="blob unreadable"),
        ):
            with self.assertRaisesRegex(ValueError, "blob unreadable"):
                run_topology_guard(
                    self.root, ["packages/a/main.ts"], base=base, revision="HEAD"
                )
            with self.assertRaisesRegex(ValueError, "blob unreadable"):
                check_contract_drift(self.root, [], base=base, revision="HEAD")
        self.write("packages/a/renamed.ts", "import 'forbidden';\n")
        self.assertIsNone(
            BaseFileReader(self.root, "HEAD", exact=True).read("packages/a/renamed.ts")
        )

    def test_binary_assets_cannot_hide_snapshot_stale_dependencies(self):
        self.config(
            {
                "enforce_module_dependencies": True,
                "modules": [
                    {"path": "packages/a", "dependencies": ["packages/b"]},
                    {"path": "packages/b", "dependencies": []},
                ],
            }
        )
        self.write("packages/a/main.ts", "import '../b/main.js';\n")
        self.write("packages/b/main.ts", "export const value = 1;\n")
        self.git("add", ".")
        self.git("commit", "-m", "live dependency")
        base = self.git("rev-parse", "HEAD")
        self.write("packages/a/main.ts", "export const value = 1;\n")
        (self.root / "packages/a/image.png").write_bytes(b"\x89PNG\x00\xff")
        self.git("add", ".")
        self.git("commit", "-m", "remove import with binary asset")
        paths = ["packages/a/main.ts", "packages/a/image.png"]
        working = check_contract_drift(self.root, paths, base=base)
        self.write(".gitmodules", '[submodule "fake"]\npath = packages/a\n')
        snapshot = check_contract_drift(self.root, paths, base=base, revision="HEAD")
        self.assertEqual([f.message for f in snapshot], [f.message for f in working])
        self.assertTrue(any("Stale dependency" in f.message for f in snapshot))

    def test_contract_drift_batches_snapshot_reads_for_large_repositories(self):
        self.config({
            "modules": [
                {"path": "packages/foundation"},
                {"path": "packages/surface"},
                {"path": "packages/legacy"},
            ]
        })
        for index in range(100):
            self.write(f"packages/foundation/mod_{index:03d}.py", f"value = {index}\n")
            self.write(f"packages/surface/mod_{index:03d}.py", f"value = {index + 1000}\n")
            self.write(f"packages/foundation/notes_{index:03d}.md", f"# Notes {index}\n")
            self.write(f"packages/surface/data_{index:03d}.json", json.dumps({"index": index}))
        self.write("packages/surface/api.py", "def read():\n    return 1\n")
        self.write("packages/legacy/api.py", "def read():\n    return 2\n")
        self.write("packages/foundation/use_surface.py", "from packages.surface.api import read\n")
        self.write("packages/surface/use_legacy.py", "from packages.legacy.api import read\n")
        self.write(
            "packages/surface/use_legacy.vue",
            '<script>\nimport { read } from "../legacy/api.ts";\n</script>\n',
        )
        # Inert documentation keeps its text but never contributes import edges.
        self.write(
            "packages/surface/guide.md",
            'Retired callers: import "packages.legacy.api"\n',
        )
        self.git("add", ".")
        self.git("commit", "-m", "permissive baseline at scale")
        base = self.git("rev-parse", "HEAD")
        self.config({
            "enforce_module_dependencies": True,
            "layers": {"foundation": [], "surface": ["foundation"]},
            "modules": [
                {"path": "packages/foundation", "layer": "foundation", "dependencies": []},
                {"path": "packages/surface", "layer": "surface", "dependencies": []},
                {"path": "packages/legacy", "retired": True},
            ],
        })
        self.git("add", ".")
        self.git("commit", "-m", "tighten layers and retire legacy")
        head = self.git("rev-parse", "HEAD")
        commands = []
        real_run = subprocess.run

        def counting_run(args, *rest, **kwargs):
            if args and args[0] == "git":
                commands.append(list(args))
            return real_run(args, *rest, **kwargs)

        with patch("subprocess.run", new=counting_run):
            findings = check_contract_drift(
                self.root, [".guard/config.json"], base=base, revision=head
            )
        self.assertEqual(
            {(f.path, f.line, f.rule_id) for f in findings},
            {
                ("packages/foundation/use_surface.py", 1, "guard.service-boundaries"),
                ("packages/surface/use_legacy.py", 1, "guard.service-boundaries"),
                ("packages/surface/use_legacy.vue", 2, "guard.service-boundaries"),
            },
        )
        by_path = {f.path: f for f in findings}
        self.assertIn("Layer 'foundation'", by_path["packages/foundation/use_surface.py"].message)
        self.assertIn("retired module 'packages/legacy'", by_path["packages/surface/use_legacy.py"].message)
        self.assertIn("retired module 'packages/legacy'", by_path["packages/surface/use_legacy.vue"].message)
        self.assertLessEqual(len(commands), 25, commands)

    def test_contract_drift_baseline_evidence_uses_baseline_ownership(self):
        self.config({
            "enforce_module_dependencies": True,
            "modules": [
                {"path": "packages/a", "dependencies": ["packages/b"]},
                {"path": "packages/b", "dependencies": []},
            ],
        })
        self.write("packages/a/keep.py", "value = 1\n")
        self.write("packages/a/nested/main.py", "import packages.b.api\n")
        self.write("packages/b/api.py", "def read():\n    return 1\n")
        self.git("add", ".")
        self.git("commit", "-m", "baseline parent-owned dependency")
        base = self.git("rev-parse", "HEAD")
        self.config({
            "enforce_module_dependencies": True,
            "modules": [
                {"path": "packages/a", "dependencies": ["packages/b"]},
                {"path": "packages/a/nested", "dependencies": []},
                {"path": "packages/b", "dependencies": []},
            ],
        })
        self.write("packages/a/keep.py", "value = 2\n")
        self.git("add", ".")
        self.git("commit", "-m", "split nested owner")
        head = self.git("rev-parse", "HEAD")
        findings = check_contract_drift(
            self.root,
            [".guard/config.json", "packages/a/keep.py"],
            base=base,
            revision=head,
        )
        self.assertEqual(
            {(f.path, f.rule_id, f.message) for f in findings},
            {
                (
                    "packages/a/nested/main.py",
                    "guard.service-boundaries",
                    "Module 'packages/a/nested' imports undeclared dependency 'packages/b' (allowed: []).",
                ),
                (
                    ".guard/config.json",
                    "guard.single-source-of-truth",
                    "Stale dependency 'packages/a' -> 'packages/b'; remove the unused declared dependency.",
                ),
            },
        )

    def test_snapshot_baseline_ignores_dirty_rename_inference(self):
        self.config(
            {"forbidden_imports": [{"scope_prefixes": ["packages/a"], "imports": ["forbidden"]}]}
        )
        self.write("packages/a/original.ts", "import 'forbidden';\n")
        self.git("add", ".")
        self.git("commit", "-m", "legacy import")
        base = self.git("rev-parse", "HEAD")
        self.write("packages/a/copied.ts", "import 'forbidden';\n")
        self.git("add", ".")
        self.git("commit", "-m", "new violating copy")
        (self.root / "packages/a/original.ts").unlink()
        findings = run_topology_guard(
            self.root, ["packages/a/copied.ts"], base=base, revision="HEAD"
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].path, "packages/a/copied.ts")

    def test_new_nested_owner_rechecks_sources_and_callers_preserving_old_debt(self):
        topology = {
            "enforce_module_dependencies": True,
            "layers": {"core": [], "plugin": []},
            "modules": [
                {
                    "path": "packages/a",
                    "layer": "core",
                    "role": "Core",
                    "dependencies": [],
                },
                {
                    "path": "packages/b",
                    "layer": "plugin",
                    "role": "Other",
                    "dependencies": [],
                },
            ],
        }
        self.config(topology)
        self.write(
            "packages/a/main.ts", "import './nested/item.js';\nimport '../b/main.js';\n"
        )
        self.write("packages/a/nested/item.ts", "import '../main.js';\n")
        self.write("packages/b/main.ts", "export const value = 1;\n")
        self.git("add", ".")
        self.git("commit", "-m", "internal imports with legacy external debt")
        topology["modules"].append(
            {
                "path": "packages/a/nested",
                "layer": "plugin",
                "role": "Nested owner",
                "dependencies": [],
            }
        )
        self.config(topology)
        findings = check_contract_drift(self.root, [".guard/config.json"], base="HEAD")
        self.assertEqual(
            {(f.path, f.line) for f in findings},
            {("packages/a/main.ts", 1), ("packages/a/nested/item.ts", 1)},
        )

    def test_dependency_cleanup_preserves_existing_denied_imports(self):
        topology = {
            "enforce_module_dependencies": True,
            "modules": [
                {"path": "packages/a", "dependencies": ["packages/c"]},
                {"path": "packages/b", "dependencies": []},
                {"path": "packages/c", "dependencies": []},
            ],
        }
        self.config(topology)
        self.write("packages/a/main.ts", "import '../b/main.js';\n")
        self.write("packages/b/main.ts", "export const value = 1;\n")
        self.write("packages/c/main.ts", "export const value = 1;\n")
        self.git("add", ".")
        self.git("commit", "-m", "legacy denied import and unused permission")
        topology["modules"][0]["dependencies"] = []
        self.config(topology)
        self.assertEqual(
            check_contract_drift(self.root, [".guard/config.json"], base="HEAD"), []
        )

    def test_module_roots_changes_recheck_working_and_committed_resolution(self):
        config = {
            "module_doc_roots": ["src"],
            "topology": {
                "enforce_module_dependencies": True,
                "modules": [
                    {"path": "packages/a", "dependencies": []},
                    {"path": "packages/b", "dependencies": []},
                ],
            },
        }
        self.write(".guard/config.json", json.dumps(config))
        self.write("packages/a/main.py", "import b\n")
        self.write("packages/b/main.py", "value = 1\n")
        self.git("add", ".")
        self.git("commit", "-m", "external alias baseline")
        base = self.git("rev-parse", "HEAD")
        config["module_doc_roots"].append("packages")
        self.write(".guard/config.json", json.dumps(config))
        working = check_contract_drift(self.root, [".guard/config.json"], base=base)
        self.assertEqual(len(working), 1)
        self.assertEqual(working[0].path, "packages/a/main.py")
        self.assertIn("packages/b", working[0].message)
        self.git("add", ".")
        self.git("commit", "-m", "alias now resolves to internal owner")
        snapshot = check_contract_drift(
            self.root, [".guard/config.json"], base=base, revision="HEAD"
        )
        self.assertEqual(snapshot, working)

    def test_missing_document_grandfathering_is_per_declaration(self):
        topology = {"architecture_docs": ["docs/legacy-root.md"], "modules": [
            {"path": "packages/a", "boundary_docs": ["docs/legacy-a.md"]},
            {"path": "packages/b", "role": "Original responsibility"}]}
        self.config(topology)
        self.git("add", ".")
        self.git("commit", "-m", "baseline missing declarations")
        topology["modules"][1]["role"] = "Updated responsibility"
        self.config(topology)
        self.assertEqual(check_contract_drift(self.root, [".guard/config.json"], base="HEAD"), [])
        topology["modules"][1]["boundary_docs"] = ["docs/new-b.md"]
        self.config(topology)
        findings = check_contract_drift(self.root, [".guard/config.json"], base="HEAD")
        self.assertEqual(len(findings), 1)
        self.assertIn("docs/new-b.md", findings[0].message)
        self.assertIn("packages/b", findings[0].message)
        self.assertNotIn("legacy", findings[0].message)

    def test_self_repository_has_complete_minimal_governance(self):
        root = Path(__file__).resolve().parents[1]
        report = audit_topology(root)
        self.assertTrue(report["passed"], report)
        self.assertEqual(len(load_config(root).topology["modules"]), 12)
        self.assertEqual(report["stale_dependencies"], [])

    def test_regex_literals_do_not_hide_imports_or_confuse_division(self):
        cases = [
            ('const quote = /"/;', "../host/index.js"),
            ("const slash = /['\"/]/g;", "../host/index.ts"),
            ("const ratio = a / b;", "../host/index.ts"),
            (r"const escaped = /\/[/*]import['\"]/gi;", "../host/index.ts"),
            ('const broken = /unterminated', "../host/index.ts"),
            ('const broken = /"unterminated', "../host/index.ts"),
        ]
        cfg = GuardConfig(topology={"forbidden_imports": [{"imports": ["../host/"]}]})
        for declaration, spec in cases:
            with self.subTest(declaration=declaration):
                code = declaration + f'\nimport "{spec}";\n'
                self.assertEqual(extract_imports("packages/core/main.ts", code), [(2, 2, spec)])
                self.assertEqual(len(check_configured("packages/core/main.ts", code, None, None, cfg)), 1)
        self.assertEqual(extract_imports("main.ts", 'const value = `${/"/.test(x) ? import("../host/index.ts") : ""}`'), [(1, 1, "../host/index.ts")])
        for prefix in ("", "return ", "throw ", "case ", "typeof ", "await ", "(", "[", "!", "x =", "x &&"):
            with self.subTest(prefix=prefix):
                code = prefix + '/"/;\nimport "../host/index.ts";'
                self.assertEqual(extract_imports("main.ts", code), [(2, 2, "../host/index.ts")])
        self.assertEqual(extract_imports("main.ts", "const broken = /unterminated"), [])

    def test_regex_and_division_context_after_control_flow_and_expressions(self):
        cases = [
            'if (ready) /"/.test(value); import("../host/index.js");',
            'while (ready) /"/.test(value); import("../host/index.js");',
            'for (const x of list) /"/.test(x); import("../host/index.js");',
            'if (ready) { act(); } /"/.test(value); import("../host/index.js");',
            'const ratio = n++ / (await import("../host/index.js")).denominator / 2;',
            'const ratio = (a + b) / (await import("../host/index.js")).denominator / 2;',
            'const ratio = { valueOf: () => 4 } / (await import("../host/index.js")).denominator / 2;',
        ]
        cfg = GuardConfig(topology={"enforce_module_dependencies": True, "modules": [
            {"path": "packages/core", "dependencies": []},
            {"path": "packages/host", "dependencies": []},
        ]})
        for code in cases:
            with self.subTest(code=code):
                self.assertEqual(extract_imports("packages/core/main.ts", code), [(1, 1, "../host/index.js")])
                findings = check_configured("packages/core/main.ts", code, None, None, cfg)
                self.assertEqual(len(findings), 1)
                self.assertIn("packages/host", findings[0].message)
        for value in ("n--", "++n", "fn()", "0xffn", "0b10", "0o77", "1.2e-3", ".5", "this", "true", "null", "arr[0]"):
            with self.subTest(value=value):
                code = f'const ratio = {value} / (await import("../host/index.js")).denominator / 2;'
                self.assertEqual(extract_imports("main.ts", code), [(1, 1, "../host/index.js")])

    def test_explicit_script_rules_apply_in_both_changed_scope_and_audit(self):
        self.config({"forbidden_patterns": [{"scope_prefixes": ["scripts/"], "literal_or_regex": "private_state"}]})
        self.write("scripts/deploy.sh", "echo private_state\n")
        changed = run_topology_guard(self.root, ["scripts/deploy.sh"])
        self.assertEqual(len(changed), 1)
        report = audit_topology(self.root)
        self.assertEqual([(f["path"], f["line"]) for f in report["findings"]], [("scripts/deploy.sh", 1)])
        self.assertFalse(report["passed"])

    def test_component_files_prevent_unproven_stale_dependency_findings(self):
        self.config({"enforce_module_dependencies": True, "modules": [
            {"path": "packages/ui", "dependencies": ["packages/domain"]},
            {"path": "packages/domain", "dependencies": []},
        ]})
        self.write("packages/ui/helper.ts", "export const helper = 1;")
        self.write("packages/domain/index.ts", "export const domain = 1;")
        for extension in ("vue", "svelte", "astro", "graphql", "css"):
            with self.subTest(extension=extension):
                component = f"packages/ui/Widget.{extension}"
                self.write(component, '<script>import "../domain/index.ts";</script>')
                report = audit_topology(self.root)
                self.assertEqual(report["stale_dependencies"], [])
                self.assertTrue(report["passed"])
                (self.root / component).unlink()
        self.write("packages/ui/README.md", "UI documentation")
        self.write("packages/ui/package.json", '{}')
        report = audit_topology(self.root)
        self.assertEqual(report["stale_dependencies"], [{"module": "packages/ui", "unused_dependency": "packages/domain"}])
        self.assertFalse(report["passed"])

    def test_aliases_resolve_nested_owners_before_self_edge_filtering(self):
        for root, extension, spec in (
            ("src", "py", "pkg.host"),
            ("packages", "ts", "@org/pkg/host/entry"),
            ("packages", "ts", "pkg/host/entry.ts"),
        ):
            with self.subTest(spec=spec):
                paths = [f"{root}/pkg", f"{root}/pkg/host"]
                source = f"{root}/pkg/main.{extension}"
                self.assertEqual(resolve_target_module(source, spec, paths), paths[1])
                self.assertIsNone(resolve_target_module(source, spec, [*paths, "apps/pkg"]))
                cfg = GuardConfig(topology={"enforce_module_dependencies": True, "modules": [
                    {"path": path, "dependencies": []} for path in paths
                ]})
                code = f"import {spec}\n" if extension == "py" else f'import "{spec}";'
                findings = check_configured(source, code, None, None, cfg)
                self.assertEqual(len(findings), 1)
                self.assertIn(paths[1], findings[0].message)
        # A declared nested leaf can be resolved even without a declared parent.
        self.assertEqual(resolve_target_module("src/other/main.py", "pkg.host", ["src/pkg/host"]), "src/pkg/host")

    def test_template_interpolations_emit_imports_but_literal_text_is_inert(self):
        code = '''const a = `${await import('../host/index.ts')}`;
const b = `${`${import('../host/index.ts')}`}`;
const c = `${({value: {load: import('../host/index.ts')}}).value}`;
const d = '${import("ignored")}';
const e = `import 'ignored'`;
const f = `escaped \\${import('ignored')} and \\` import 'ignored'`;
const g = `text
${ /* comment import('ignored') */ await import(
'../host/index.ts')
}`;
'''
        self.assertEqual(extract_imports("packages/core/index.ts", code), [
            (1, 1, "../host/index.ts"), (2, 2, "../host/index.ts"),
            (3, 3, "../host/index.ts"), (8, 9, "../host/index.ts"),
        ])
        self.config({"enforce_module_dependencies": True, "modules": [
            {"path": "packages/core", "dependencies": []},
            {"path": "packages/host", "dependencies": []},
        ]})
        self.write("packages/core/index.ts", code)
        self.write("packages/host/index.ts", "export const host = 1;")
        self.assertEqual(discover_topology(self.root)["modules"][0]["dependencies"], ["packages/host"])
        self.assertEqual(len(audit_topology(self.root)["findings"]), 4)

    def test_inventory_empty_registration_and_nested_owned_files(self):
        self.write("packages/host/nested/index.ts", "export const value = 1;")
        for governance in ({"enforce_module_dependencies": True}, {"layers": {"core": []}}):
            with self.subTest(governance=governance):
                self.config({**governance, "modules": []})
                report = audit_topology(self.root)
                self.assertEqual(report["unregistered_modules"], ["packages/host"])
                self.assertFalse(report["passed"])
        self.config({"enforce_module_dependencies": True, "layers": {"core": []}, "modules": [
            {"path": "packages/host/nested", "layer": "core", "dependencies": []},
        ]})
        self.assertEqual(audit_topology(self.root)["unregistered_modules"], [])
        result = sync_topology(self.root)
        self.assertEqual(result["unregistered_modules"], [])
        self.assertEqual([m["path"] for m in result["modules"]], ["packages/host/nested"])
        self.assertEqual(result["modules"][0]["languages"], ["TypeScript"])
        self.write("packages/host/sibling.ts", "export const sibling = 1;")
        self.assertEqual(audit_topology(self.root)["unregistered_modules"], ["packages/host"])
        self.assertEqual(sync_topology(self.root)["unregistered_modules"], ["packages/host"])

    def test_sync_refreshes_declared_owners_without_absorbing_nested_files(self):
        self.config({"modules": [
            {"path": "packages/host", "role": "parent", "languages": ["Swift"], "dependencies": []},
            {"path": "packages/host/nested", "role": "leaf", "dependencies": []},
            {"path": "packages/domain", "dependencies": []},
        ]})
        self.write("packages/host/main.py", "value = 1\n")
        self.write("packages/host/nested/main.ts", "import '../../domain/index.ts';")
        self.write("packages/host/nested/README.md", "Leaf owner")
        self.write("packages/domain/index.ts", "export const value = 1;")
        modules = {m["path"]: m for m in sync_topology(self.root)["modules"]}
        self.assertEqual(modules["packages/host"]["languages"], ["Python"])
        self.assertEqual(modules["packages/host"]["dependencies"], [])
        self.assertEqual(modules["packages/host/nested"]["dependencies"], ["packages/domain"])
        self.assertEqual(modules["packages/host/nested"]["boundary_docs"], ["packages/host/nested/README.md"])

    def test_audit_checks_all_text_and_skips_binary_files(self):
        self.config({"forbidden_patterns": [{"scope_prefixes": ["frontend/"], "literal_or_regex": "private_state"}]})
        for extension in ("vue", "yaml", "graphql"):
            self.write(f"frontend/App.{extension}", "private_state\n")
        (self.root / "frontend/image.bin").write_bytes(b"private_state\x00binary")
        report = audit_topology(self.root)
        self.assertEqual([f["path"] for f in report["findings"]], ["frontend/App.graphql", "frontend/App.vue", "frontend/App.yaml"])
        self.assertFalse(report["passed"])

    def test_stale_dependencies_require_supported_source_languages_only(self):
        self.config({"enforce_module_dependencies": True, "modules": [
            {"path": "packages/ui", "dependencies": ["packages/domain"]},
            {"path": "packages/domain", "dependencies": []},
        ]})
        self.write("packages/ui/View.swift", "import Domain\n")
        self.write("packages/domain/Domain.swift", "struct Domain {}\n")
        report = audit_topology(self.root)
        self.assertEqual(report["stale_dependencies"], [])
        self.assertTrue(report["passed"])
        self.write("packages/ui/helper.py", "value = 1\n")
        self.assertEqual(audit_topology(self.root)["stale_dependencies"], [])
        (self.root / "packages/ui/View.swift").unlink()
        self.assertEqual(audit_topology(self.root)["stale_dependencies"], [
            {"module": "packages/ui", "unused_dependency": "packages/domain"},
        ])

    def test_multiline_import_spans_and_boundary_ratchets(self):
        modules = [
            {"path": "packages/core", "dependencies": []},
            {"path": "packages/target", "dependencies": []},
        ]
        cases = [
            ("packages/core/index.ts", 'import {\n from as f,\n} from "../target/index.ts";\n', (1, 3, "../target/index.ts"), "../"),
            ("packages/core/index.ts", 'export {\n from\n} from "../target/index.ts";\n', (1, 3, "../target/index.ts"), "../target/"),
            ("packages/core/index.py", 'from .. import (\n target,\n)\n', (1, 3, "target"), "target/"),
        ]
        for path, code, extracted, prefix in cases:
            with self.subTest(path=path, code=code):
                self.assertIn(extracted, extract_imports(path, code))
                for topology in (
                    {"modules": modules, "enforce_module_dependencies": True},
                    {"forbidden_imports": [{"scope_prefixes": ["packages/core/"], "imports": [prefix]}]},
                ):
                    cfg = GuardConfig(topology=topology)
                    for changed, expected in (({2}, 3), ({3}, 3), ({1}, 1), (None, 1)):
                        findings = check_configured(path, code, None, changed, cfg)
                        self.assertEqual([f.line for f in findings], [expected])
                    self.assertEqual(check_configured(path, code, code, {4}, cfg), [])
                    comment = " #" if path.endswith(".py") else " //"
                    for line in (0, 2):
                        lines = code.splitlines()
                        lines[line] += comment + " guard-exempt guard.service-boundaries: approved boundary fixture"
                        self.assertEqual(check_configured(path, "\n".join(lines), None, {2}, cfg), [])
        for prefix in ("pkg/", "pkg."):
            cfg = GuardConfig(topology={"forbidden_imports": [{"imports": [prefix]}]})
            for spec in ("pkg", "pkg/item", "pkg.item"):
                self.assertEqual(len(check_configured("src/a.ts", f'import "{spec}";', None, None, cfg)), 1)
            self.assertEqual(check_configured("src/a.ts", 'import "pkg_other";', None, None, cfg), [])

    def test_examples_and_benchmarks_are_not_production_modules(self):
        self.config({"enforce_module_dependencies": True, "modules": [
            {"path": "packages/core", "dependencies": []},
            {"path": "packages/host", "dependencies": []},
        ]})
        self.write("packages/core/index.ts", "export const x = 1;")
        self.write("packages/host/index.ts", "export const x = 1;")
        for directory in ("examples", "benchmarks"):
            self.write(f"{directory}/main.ts", "import '../packages/host/index.ts';")
            self.write(f"packages/core/{directory}/main.ts", "import '../../host/index.ts';")
        report = audit_topology(self.root)
        self.assertEqual(report["unregistered_modules"], [])
        self.assertEqual(report["findings"], [])
        self.assertTrue(report["passed"])
        self.assertEqual(discover_topology(self.root)["modules"][0]["dependencies"], [])

    def test_shared_js_extraction_and_discovery(self):
        code = '''// import "ignored";
/* export * from "ignored";
   require("ignored"); */
const text = "import 'ignored' // not a comment";
import type {
  Host,
} from '../orca-workspace/index.ts';
export * from "../orca-workspace/index.ts";
export type { Host } from '../orca-workspace/index.ts';
const load = import(
  '../orca-workspace/index.ts'
);
const host = require( '../orca-workspace/index.ts' );
import '../orca-workspace/index.ts';
'''
        for extension in ("ts", "tsx", "js", "jsx", "mjs", "cjs"):
            with self.subTest(extension=extension):
                path = f"packages/mailbox/main.{extension}"
                imports = extract_imports(path, code)
                self.assertEqual([line for line, _, _ in imports], [5, 8, 9, 10, 13, 14])
                self.assertEqual({spec for _, _, spec in imports}, {"../orca-workspace/index.ts"})
                self.write(path, code)
        self.write("packages/orca-workspace/index.ts", "export const host = 1;")
        modules = {m["path"]: m for m in discover_topology(self.root)["modules"]}
        self.assertEqual(modules["packages/mailbox"]["dependencies"], ["packages/orca-workspace"])
        self.assertEqual(extract_imports("x.ts", "const p = import('host/' + name); const r = require('host/' + name);"), [])
        self.write("frontend/main.js", "import '@org/mailbox';")
        self.write("packages/mailbox/README.md", "import 'frontend';")
        self.write("packages/mailbox/tests/main.js", "import 'frontend';")
        discovered = {m["path"]: m for m in discover_topology(self.root)["modules"]}
        self.assertIn("frontend", discovered)
        self.assertEqual(discovered["packages/mailbox"]["dependencies"], ["packages/orca-workspace"])
        cfg = GuardConfig(topology={"forbidden_imports": [{"scope_prefixes": ["packages/mailbox/"], "imports": ["../orca-workspace"]}]})
        self.assertEqual([f.line for f in check_configured(path, code, None, None, cfg)], [5, 8, 9, 10, 13, 14])

    def test_module_resolution_ownership_aliases_and_escape(self):
        paths = ["packages/core", "packages/host", "packages/host/nested", "apps/host"]
        self.assertEqual(owner_module("packages/host/nested/a.ts", paths), "packages/host/nested")
        cases = {
            "../host/nested/a.ts": "packages/host/nested",
            "./local.ts": None,
            "../../../packages/host/a.ts": None,
            "packages/host/a.ts": "packages/host",
            "packages.host.nested.entry": "packages/host/nested",
            "host/entry": None,
            "@org/host/entry": None,
            "external": None,
        }
        for spec, expected in cases.items():
            with self.subTest(spec=spec):
                self.assertEqual(resolve_target_module("packages/core/a.ts", spec, paths), expected)
        self.assertEqual(resolve_target_module("packages/core/a.ts", "@org/host/entry", paths[:3]), "packages/host")
        self.assertEqual(resolve_target_module("packages/core/a.ts", "host.sub", paths[:3]), "packages/host")

    def test_module_stem_preserves_posix_suffix_semantics(self):
        paths = (
            ".py", "a/.py", "a/..py", "a/...ts", "a/.hidden.py", "a/b.py",
            "a/b.ts", "a/b.d.ts", "a/b", "a/b.py/.", "a/b.py/", "a/b.py.txt",
        )
        for path in paths:
            with self.subTest(path=path):
                suffix = PurePosixPath(path).suffix
                expected = (
                    path.removesuffix(suffix)
                    if suffix in DETERMINISTIC_IMPORT_EXTENSIONS
                    else path
                )
                self.assertEqual(_module_stem(path), expected)

    def test_indexed_module_resolution_preserves_precedence(self):
        paths = (
            "src/client", "src/tool", "src/tool/__init__.py", "src/tool/nested",
            "src/tool/nested/config.d.ts", "src/.py", "packages/tool", "///tool",
            "src/choice.ts", "src/choice.py",
        )
        source = "src/client//./run.ts/."
        cases = (
            ("src/.py", ("src",), "src/.py"),
            ("tool", ("src",), "src/tool/__init__.py"),
            ("@scope/tool/nested/entry.ts", ("src",), "src/tool/nested"),
            ("../tool//./nested/config.d.ts", (), "src/tool/nested/config.d.ts"),
            ("./local.ts", ("src",), None),
            ("../../../tool", ("src",), None),
            ("tool", ("src", "packages"), None),
            ("src/tool", ("src", "packages"), "src/tool/__init__.py"),
            ("tool", ("packages",), "packages/tool"),
            ("tool", ("src",), "src/tool/__init__.py"),
            ("src.choice", ("src",), "src/choice.py"),
        )
        for spec, roots, expected in cases:
            with self.subTest(spec=spec, roots=roots):
                self.assertEqual(resolve_target_module(source, spec, paths, roots), expected)
        self.assertEqual(
            owner_module("src/tool/nested/config.d.ts", paths), "src/tool/nested/config.d.ts"
        )
        self.assertEqual(
            resolve_target_module("//src/client/run.ts", "../../tool/file", paths), "///tool"
        )

    def test_repeated_module_resolution_is_fast(self):
        source = "src/client/run.ts"
        paths = ["src/client", "src/tool/nested", *(f"src/module{i}" for i in range(38))]
        specs = [f"external{i}.entry" for i in range(19)] + ["../tool/nested/index.ts"]
        start = perf_counter()
        results = [resolve_target_module(source, spec, paths) for spec in specs * 1_000]
        elapsed = perf_counter() - start
        self.assertEqual(results, ([None] * 19 + ["src/tool/nested"]) * 1_000)
        self.assertLess(elapsed, 1.0, f"20,000 import resolutions took {elapsed:.3f}s")

    def test_python_from_and_relative_imports_share_resolution(self):
        code = "from ..storage import rows\nfrom internal import storage\n"
        imports = extract_imports("src/api/sub/main.py", code)
        self.assertIn((1, 1, "api.storage.rows"), imports)
        self.assertIn((2, 2, "internal.storage"), imports)
        self.assertIn((2, 2, "internal"), imports)
        custom_imports = extract_imports("custom/api/sub/main.py", code, ("custom",))
        self.assertIn((1, 1, "api.storage.rows"), custom_imports)
        self.assertNotIn((1, 1, "api.storage.rows"), extract_imports("app/api/sub/main.py", code))
        self.write("src/api/main.py", "from ..storage import rows\n")
        self.write("src/storage/__init__.py", "")
        modules = {m["path"]: m for m in discover_topology(self.root)["modules"]}
        self.assertEqual(modules["src/api"]["dependencies"], ["src/storage"])

    def test_layer_and_dependency_enforcement_touched_exemptions_and_tests(self):
        cfg = GuardConfig(topology={
            "layers": {"core": [], "plugin": ["core"]},
            "enforce_module_dependencies": True,
            "modules": [
                {"path": "packages/core", "layer": "core", "dependencies": []},
                {"path": "packages/one", "layer": "plugin", "dependencies": []},
                {"path": "packages/two", "layer": "plugin", "dependencies": []},
            ],
        })
        for source, target, message in (
            ("core", "one", "Layer 'core'"),
            ("one", "two", "Layer 'plugin'"),
            ("one", "core", "undeclared dependency"),
        ):
            code = f"import '../{target}/index.ts';\n"
            path = f"packages/{source}/index.ts"
            findings = check_configured(path, code, None, None, cfg)
            self.assertEqual(len(findings), 1)
            self.assertIn(message, findings[0].message)
            self.assertEqual(check_configured(path, code, code, set(), cfg), [])
            exempt = code.rstrip() + " // guard-exempt guard.service-boundaries: approved boundary fixture\n"
            self.assertEqual(check_configured(path, exempt, None, None, cfg), [])
        code = "import '../../two/index.ts';\n"
        self.assertEqual(check_configured("packages/one/tests/main.ts", code, None, None, cfg), [])
        cfg.topology["modules"][1]["dependencies"] = ["packages/core"]
        self.assertEqual(check_configured("packages/one/index.ts", "import '../core/index.ts'", None, None, cfg), [])

    def test_sync_preserves_curated_contract_and_defers_unlayered_modules(self):
        self.config({
            "enforce_module_dependencies": True,
            "layers": {"core": [], "plugin": ["core"]},
            "modules": [{"path": "packages/core", "layer": "core", "role": "shared state",
                         "dependencies": [], "boundary_docs": ["docs/architecture.md"]}],
        })
        self.write("packages/core/index.ts", "import '../host/index.ts';")
        self.write("packages/core/README.md", "boundary")
        self.write("packages/host/index.ts", "export const x = 1;")
        result = sync_topology(self.root)
        self.assertEqual(result["unregistered_modules"], ["packages/host"])
        module = load_config(self.root).topology["modules"][0]
        self.assertEqual(module, {"path": "packages/core", "layer": "core", "role": "shared state",
                                 "dependencies": [], "boundary_docs": ["docs/architecture.md", "packages/core/README.md"],
                                 "languages": ["TypeScript"]})
        self.assertEqual(sync_topology(self.root), result)
        self.config({"modules": [module]})
        result = sync_topology(self.root)
        self.assertEqual(result["modules"][0]["dependencies"], ["packages/host"])
        self.assertEqual(result["modules"][0]["role"], "shared state")
        self.assertIn("docs/architecture.md", result["modules"][0]["boundary_docs"])

    def test_audit_legacy_scope_missing_stale_and_unregistered(self):
        self.config({
            "enforce_module_dependencies": True,
            "modules": [
                {"path": "packages/core", "dependencies": ["packages/unused"], "boundary_docs": ["docs/missing.md"]},
                {"path": "packages/host", "dependencies": []},
                {"path": "packages/unused", "dependencies": []},
            ],
        })
        self.write("packages/core/index.ts", "import '../host/index.ts';\n")
        self.write("packages/core/tests/test.ts", "import '../../unused/index.ts';\n")
        for name in ("host", "unused", "new"):
            self.write(f"packages/{name}/index.ts", "export const x = 1;")
        self.git("add", ".")
        self.git("commit", "-m", "legacy violation")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.assertEqual(run_topology_guard(self.root, ["packages/core/index.ts"]), [])
        report = audit_topology(self.root)
        self.assertFalse(report["passed"])
        self.assertEqual(report["unregistered_modules"], ["packages/new"])
        self.assertEqual(report["missing_boundary_docs"], [{"module": "packages/core", "doc": "docs/missing.md"}])
        self.assertEqual(report["stale_dependencies"], [{"module": "packages/core", "unused_dependency": "packages/unused"}])
        self.assertEqual(len(report["findings"]), 1)
        self.assertIn("undeclared dependency", report["findings"][0]["message"])
        self.assertEqual(len(run_topology_guard(self.root, ["packages/core/index.ts"], base="HEAD", full_scan=True)), 1)
        self.config({"modules": [{"path": "packages/core", "dependencies": ["packages/unused"]}]})
        self.assertTrue(audit_topology(self.root)["passed"])

    def test_read_locks_and_mutation_helpers(self):
        for sql in (
            "FOR UPDATE",
            "FOR NO KEY UPDATE",
            "LOCK TABLE rows",
            "pg_advisory_xact_lock(1)",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(
                    len(self.check(f'def get_item():\n    return "{sql}"\n')), 1
                )
        self.assertEqual(
            len(self.check("def read_item():\n    return query.with_for_update()\n")), 1
        )
        self.assertEqual(
            self.check('def get_or_create_item():\n    return "FOR UPDATE"\n'), []
        )
        self.assertEqual(
            len(
                self.check(
                    '@router.get("/items")\ndef handler():\n    return "FOR UPDATE"\n'
                )
            ),
            1,
        )

    def test_duplicate_control_flow_write_barrier_and_batch(self):
        cases = [
            (
                "async def f(x):\n    await db.get_item(x)\n    await db.get_item(x)\n",
                1,
            ),
            (
                "async def f(x):\n    await db.get_item(x)\n    await db.update_item(x)\n    await db.get_item(x)\n",
                0,
            ),
            (
                "async def f(x):\n    if x:\n        await db.get_item(x)\n    else:\n        await db.get_item(x)\n",
                0,
            ),
            (
                "async def f(x):\n    if x:\n        await db.get_item(x)\n    await db.get_item(x)\n",
                1,
            ),
            (
                "async def f(x):\n    if x:\n        await db.get_item(x)\n        return\n    await db.get_item(x)\n",
                0,
            ),
            (
                "async def f(ids):\n    for key in ids:\n        await db.get_item(key)\n",
                1,
            ),
            (
                "async def f(ids):\n    return [await db.get_item(key) for key in ids]\n",
                1,
            ),
            (
                "async def f(ids):\n    for batch in ids:\n        await db.get_batch(batch)\n",
                0,
            ),
        ]
        for code, count in cases:
            with self.subTest(code=code):
                self.assertEqual(len(self.check(code)), count)

    def test_untouched_legacy_signature_and_new_read_function(self):
        baseline = 'def worker():\n    return "FOR UPDATE"\n'
        current = baseline.replace("worker", "get_worker")
        self.assertEqual(len(self.check(current, baseline)), 1)
        baseline = 'def get_worker():\n    old = 1\n    return "FOR UPDATE"\n'
        self.assertEqual(
            self.check(baseline.replace("old = 1", "old = 2"), baseline), []
        )
        self.assertEqual(self.check(baseline, baseline), [])

    def test_owner_and_retired_ratchet(self):
        self.config(
            {
                "exclusive_sql_owners": [
                    {"table": "active_jobs", "owners": ["src/owner.py"]}
                ],
                "retired_tables": {"scope_prefixes": ["src/"], "tables": ["old_jobs"]},
            }
        )
        code = 'def write():\n    return "SELECT * FROM old_jobs"\n'
        self.write("src/api.py", code)
        self.git("add", ".")
        self.git("commit", "-m", "baseline")
        self.write("src/api.py", code.replace("SELECT *", "SELECT id"))
        findings = run_topology_guard(self.root, ["src/api.py"], base="HEAD")
        self.assertEqual([f.rule_id for f in findings], ["guard.data-modeling"])
        self.write(
            "src/api.py", 'def write():\n    return "SELECT * FROM active_jobs"\n'
        )
        findings = run_topology_guard(self.root, ["src/api.py"], base="HEAD")
        self.assertEqual(
            [f.rule_id for f in findings], ["guard.single-source-of-truth"]
        )
        self.write(
            "src/owner.py", 'def write():\n    return "SELECT * FROM active_jobs"\n'
        )
        self.assertEqual(
            run_topology_guard(self.root, ["src/owner.py"], base="HEAD"), []
        )
        self.write("src/api.py", "def write():\n    return 1\n")
        self.assertEqual(run_topology_guard(self.root, ["src/api.py"], base="HEAD"), [])

    def test_forbidden_patterns_layering_and_exemption(self):
        self.config(
            {
                "forbidden_patterns": [
                    {
                        "scope_prefixes": ["src/"],
                        "literal_or_regex": "/private-state",
                        "literal": True,
                    }
                ],
                "forbidden_imports": [{"scope_prefixes": ["src/"], "imports": ["internal.storage"]}],
            }
        )
        self.write(
            "src/api.py", 'from internal.storage import rows\nurl = "/private-state"\n'
        )
        self.assertEqual(len(run_topology_guard(self.root, ["src/api.py"])), 2)
        self.assertEqual(
            self.check(
                'def get_item():\n    return "FOR UPDATE"  # guard-exempt guard.architecture-simplicity: documented migration reader\n'
            ),
            [],
        )

    def test_swift_base_signatures(self):
        code = "func loadRecord() async {\n    let a = await api.fetchHistory(id: key)\n    let version = a.generation\n    let b = await api.fetchHistory(id: key)\n}\n"
        self.assertEqual(len(_check_swift_file("app/View.swift", code, None, None)), 1)
        current = code.replace("a.generation", "a.version")
        self.assertEqual(
            _check_swift_file(
                "app/View.swift", current, code, _compute_changed_lines(code, current)
            ),
            [],
        )

    def test_discovery_excludes_installed_package_metadata(self):
        self.write("src/library/__init__.py", "")
        self.write("src/library.egg-info/PKG-INFO", "Metadata-Version: 2.4")
        self.write("src/library.dist-info/METADATA", "Metadata-Version: 2.4")
        self.assertEqual(
            [module["path"] for module in discover_topology(self.root)["modules"]],
            ["src/library"],
        )

    def test_discovery_does_not_invent_owner_rules(self):
        self.write("packages/api/main.py", "import storage.repository\n")
        self.write("packages/api/AGENTS.md", "# API contract\n")
        self.write("packages/storage/__init__.py", "")
        modules = discover_topology(self.root)["modules"]
        api = next(m for m in modules if m["path"] == "packages/api")
        self.assertEqual(api["dependencies"], ["packages/storage"])
        self.assertEqual(api["boundary_docs"], ["packages/api/AGENTS.md"])
        self.assertNotIn("exclusive_sql_owners", discover_topology(self.root))

    def test_configured_exemptions_and_retired_changed_line_precision(self):
        config = GuardConfig(
            exempt_tag="custom-exempt",
            topology={
                "exclusive_sql_owners": [
                    {"table": "owned", "owners": ["src/owner.py"]}
                ],
                "retired_tables": [{"tables": ["old"]}],
                "forbidden_patterns": [
                    {"literal_or_regex": "/reverse", "literal": True}
                ],
                "forbidden_imports": [{"scope_prefixes": ["src/"], "imports": ["internal", "private"]}],
            },
        )
        text = "\n".join(
            [
                "import internal # custom-exempt guard.service-boundaries: approved fixture boundary",
                "import private # custom-exempt guard.service-boundaries: approved fixture boundary",
                'query = "SELECT * FROM owned" # custom-exempt guard.single-source-of-truth: approved owner projection',
                'endpoint = "/reverse" # custom-exempt guard.single-source-of-truth: approved fixture boundary',
                'query = """',
                "-- SELECT * FROM old",
                "SELECT * FROM old -- custom-exempt guard.data-modeling: approved fixture boundary",
                "SELECT * FROM old",
                '"""',
            ]
        )
        findings = check_configured("src/api.py", text, None, None, config)
        self.assertEqual(
            [(f.line, f.rule_id) for f in findings], [(8, "guard.data-modeling")]
        )
        self.assertEqual(check_configured("src/api.py", text, text, {5, 9}, config), [])
        self.assertEqual(
            [f.line for f in check_configured("src/api.py", text, text, {8}, config)],
            [8],
        )

    def test_configured_findings_follow_source_order(self):
        config = GuardConfig(
            topology={
                "retired_tables": [{"tables": ["old"]}],
                "forbidden_patterns": [{"literal_or_regex": "/reverse"}],
            }
        )
        findings = check_configured(
            "src/api.py",
            'endpoint = "/reverse"\nquery = "SELECT * FROM old"',
            None,
            None,
            config,
        )
        self.assertEqual([f.line for f in findings], [1, 2])
