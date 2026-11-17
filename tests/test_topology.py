"""Read paths remain pure and efficient; owner/retirement rules ratchet changed code only."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from the_guard.config import GuardConfig, load_config
from the_guard.topology import (
    audit_topology,
    discover_topology,
    run_topology_guard,
    sync_topology,
)
from the_guard.topology.configured import check_configured
from the_guard.topology.git import _compute_changed_lines
from the_guard.topology.imports import (
    extract_imports,
    owner_module,
    resolve_target_module,
)
from the_guard.topology.python import _check_python_file
from the_guard.topology.swift import _check_swift_file


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
                    {"forbidden_imports": [{"imports": [prefix]}]},
                    {"module_boundaries": [{"from": "packages/core/", "to": prefix}]},
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
            cfg = GuardConfig(topology={"forbidden_imports": [{"to": prefix}]})
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
        cfg = GuardConfig(topology={"forbidden_imports": [{"from": "packages/mailbox/", "to": "../orca-workspace"}]})
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

    def test_python_from_and_relative_imports_share_resolution(self):
        code = "from ..storage import rows\nfrom internal import storage\n"
        imports = extract_imports("src/api/sub/main.py", code)
        self.assertIn((1, 1, "api.storage.rows"), imports)
        self.assertIn((2, 2, "internal.storage"), imports)
        self.assertIn((2, 2, "internal"), imports)
        self.assertIn((1, 1, "api.storage.rows"), extract_imports("src/api/sub/main.py", code, ("custom",)))
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
                "forbidden_imports": [{"from": "src/", "to": "internal.storage"}],
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
                "forbidden_imports": [{"imports": ["internal"]}],
                "module_boundaries": [{"from": "src/", "to": "private"}],
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
