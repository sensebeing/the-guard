"""Policy and topology contracts must survive onboarding without project-specific defaults."""

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from the_guard.hooks import main_push_gate
from the_guard.init import WORKFLOW_TEMPLATE, initialize
from the_guard.config import load_config
from the_guard.policy import load_policy, PolicyError
from the_guard.topology import run_topology_guard, discover_topology
from the_guard.distill import distill


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def test_init_preserves_prose_and_validates_four_rules(self):
        self.write("AGENTS.md", "# Existing instructions\n")
        initialize(self.root)
        self.assertEqual(len(load_policy(self.root).rules), 4)
        self.assertIn("# Existing instructions", (self.root / "AGENTS.md").read_text())
        before = (self.root / "AGENTS.md").read_text()
        initialize(self.root)
        self.assertEqual(before, (self.root / "AGENTS.md").read_text())

    def test_full_and_invalid_detail(self):
        initialize(self.root, preset="full")
        self.assertEqual(len(load_policy(self.root).rules), 14)
        self.write("docs/standards/rules/service-boundaries.md", "broken")
        with self.assertRaises(PolicyError):
            load_policy(self.root)

    def test_configurable_topology(self):
        self.write(
            ".guard/config.json",
            json.dumps(
                {
                    "rule_id_prefix": "custom.",
                    "topology": {"python_prod_prefixes": ["lib/"]},
                }
            ),
        )
        self.write(
            "lib/api.py",
            'def get_item():\n    return "SELECT * FROM items FOR UPDATE"\n',
        )
        findings = run_topology_guard(
            self.root, ["lib/api.py"], config=load_config(self.root)
        )
        self.assertEqual(
            [f.rule_id for f in findings], ["custom.architecture-simplicity"]
        )

    def test_duplicate_reads_and_n_plus_one(self):
        self.write(
            "src/api.py",
            "async def handler(ids):\n    a = await db.get_item(1)\n    b = await db.get_item(1)\n    return [await db.get_item(key) for key in ids]\n",
        )
        findings = run_topology_guard(self.root, ["src/api.py"])
        self.assertEqual(len(findings), 2)

    def test_discovery_and_distill(self):
        self.write("packages/api/README.md", "# API owner\n")
        self.write("packages/api/main.py", "from storage import repository\n")
        self.write("packages/storage/__init__.py", "")
        topology = discover_topology(self.root)
        self.assertIn("packages/api", [m["path"] for m in topology["modules"]])
        self.assertIn("Python", distill(self.root)["languages"])


class GithubPrIntegrationModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=True
        ).stdout.strip()

    def init_repo(self):
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.write("README.md", "base\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")

    def test_github_workflow_onboarding_sets_github_pr_mode(self):
        initialize(self.root, with_github_workflow=True)
        workflow = self.root / ".github/workflows/the-guard-review.yml"
        self.assertEqual(workflow.read_text(), WORKFLOW_TEMPLATE)
        self.assertEqual(
            workflow.read_text(),
            (Path(__file__).resolve().parents[1] / "examples/the-guard-review.yml").read_text(),
        )
        self.assertEqual(
            load_config(self.root).review["integration_mode"], "github-pr"
        )

    def test_customized_workflow_is_preserved_only_for_self_dogfooding(self):
        initialize(self.root, with_github_workflow=True)
        workflow = self.root / ".github/workflows/the-guard-review.yml"
        self.write(workflow.relative_to(self.root), "name: custom\n")
        with self.assertRaises(ValueError):
            initialize(self.root, with_github_workflow=True)
        self.write(
            workflow.relative_to(self.root),
            "name: custom\nuses: ./\n",
        )
        initialize(self.root, with_github_workflow=True)
        self.assertEqual(workflow.read_text(), "name: custom\nuses: ./\n")

    def test_github_pr_mode_blocks_direct_main_push_but_allows_topic_branches(self):
        self.init_repo()
        initialize(self.root, integration_mode="github-pr")
        self.assertTrue((self.root / ".git/hooks/pre-push").exists())
        head = self.git("rev-parse", "HEAD")
        direct = f"refs/heads/main {head} refs/heads/main {head}\n"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(main_push_gate(self.root, direct), 2)
        self.assertIn("review.integration_mode=github-pr", stderr.getvalue())
        self.assertIn("gh pr merge", stderr.getvalue())
        topic = f"refs/heads/feat/topic {head} refs/heads/feat/topic {head}\n"
        self.assertEqual(main_push_gate(self.root, topic), 0)

    def test_integration_mode_validation_rejects_unknown_values(self):
        self.write(
            ".guard/config.json",
            json.dumps({"review": {"integration_mode": "server-side"}}),
        )
        with self.assertRaises(ValueError):
            load_config(self.root)
        with self.assertRaises(ValueError):
            initialize(self.root, integration_mode="server-side")


if __name__ == "__main__":
    unittest.main()
