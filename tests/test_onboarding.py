"""Policy and topology contracts must survive onboarding without project-specific defaults."""
import json
import tempfile
import unittest
from pathlib import Path
from the_guard.init import initialize
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
        self.write('AGENTS.md', '# Existing instructions\n')
        initialize(self.root)
        self.assertEqual(len(load_policy(self.root).rules), 4)
        self.assertIn('# Existing instructions', (self.root/'AGENTS.md').read_text())
        before = (self.root/'AGENTS.md').read_text()
        initialize(self.root)
        self.assertEqual(before, (self.root/'AGENTS.md').read_text())

    def test_full_and_invalid_detail(self):
        initialize(self.root, preset='full')
        self.assertEqual(len(load_policy(self.root).rules), 14)
        self.write('docs/engineering-quality/rules/service-boundaries.md', 'broken')
        with self.assertRaises(PolicyError):
            load_policy(self.root)

    def test_configurable_topology(self):
        self.write('.guard/config.json', json.dumps({'rule_id_prefix':'custom.', 'topology': {'python_prod_prefixes':['lib/']}}))
        self.write('lib/api.py', 'def get_item():\n    return "SELECT * FROM items FOR UPDATE"\n')
        findings = run_topology_guard(self.root, ['lib/api.py'], config=load_config(self.root))
        self.assertEqual([f.rule_id for f in findings], ['custom.architecture-simplicity'])

    def test_duplicate_reads_and_n_plus_one(self):
        self.write('src/api.py', 'async def handler(ids):\n    a = await db.get_item(1)\n    b = await db.get_item(1)\n    return [await db.get_item(key) for key in ids]\n')
        findings = run_topology_guard(self.root, ['src/api.py'])
        self.assertEqual(len(findings), 2)

    def test_discovery_and_distill(self):
        self.write('packages/api/README.md', '# API owner\n')
        self.write('packages/api/main.py', 'from storage import repository\n')
        self.write('packages/storage/__init__.py', '')
        topology = discover_topology(self.root)
        self.assertIn('packages/api', [m['path'] for m in topology['modules']])
        self.assertIn('Python', distill(self.root)['languages'])

if __name__ == '__main__':
    unittest.main()
