"""Read paths remain pure and efficient; owner/retirement rules ratchet changed code only."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from the_guard.config import GuardConfig, load_config
from the_guard.topology.configured import check_configured
from the_guard.topology import run_topology_guard, discover_topology
from the_guard.topology.python import _check_python_file
from the_guard.topology.swift import _check_swift_file
from the_guard.topology.git import _compute_changed_lines

class TopologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git('init','-b','main')
        self.git('config','user.email','test@example.invalid')
        self.git('config','user.name','Test')

    def git(self,*args):
        return subprocess.run(['git',*args],cwd=self.root,check=True,capture_output=True,text=True).stdout.strip()

    def write(self,path,text):
        target=self.root/path
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(text)

    def config(self,topology):
        self.write('.guard/config.json',json.dumps({'topology':topology}))

    def check(self,text,base=None):
        return _check_python_file('src/api.py',text,base,_compute_changed_lines(base,text) if base is not None else None)

    def test_read_locks_and_mutation_helpers(self):
        for sql in ('FOR UPDATE','FOR NO KEY UPDATE','LOCK TABLE rows','pg_advisory_xact_lock(1)'):
            with self.subTest(sql=sql):
                self.assertEqual(len(self.check(f'def get_item():\n    return "{sql}"\n')),1)
        self.assertEqual(len(self.check('def read_item():\n    return query.with_for_update()\n')),1)
        self.assertEqual(self.check('def get_or_create_item():\n    return "FOR UPDATE"\n'),[])
        self.assertEqual(len(self.check('@router.get("/items")\ndef handler():\n    return "FOR UPDATE"\n')),1)

    def test_duplicate_control_flow_write_barrier_and_batch(self):
        cases = [
            ('async def f(x):\n    await db.get_item(x)\n    await db.get_item(x)\n',1),
            ('async def f(x):\n    await db.get_item(x)\n    await db.update_item(x)\n    await db.get_item(x)\n',0),
            ('async def f(x):\n    if x:\n        await db.get_item(x)\n    else:\n        await db.get_item(x)\n',0),
            ('async def f(x):\n    if x:\n        await db.get_item(x)\n    await db.get_item(x)\n',1),
            ('async def f(x):\n    if x:\n        await db.get_item(x)\n        return\n    await db.get_item(x)\n',0),
            ('async def f(ids):\n    for key in ids:\n        await db.get_item(key)\n',1),
            ('async def f(ids):\n    return [await db.get_item(key) for key in ids]\n',1),
            ('async def f(ids):\n    for batch in ids:\n        await db.get_batch(batch)\n',0),
        ]
        for code,count in cases:
            with self.subTest(code=code):
                self.assertEqual(len(self.check(code)),count)

    def test_untouched_legacy_signature_and_new_read_function(self):
        baseline='def worker():\n    return "FOR UPDATE"\n'
        current=baseline.replace('worker','get_worker')
        self.assertEqual(len(self.check(current,baseline)),1)
        baseline='def get_worker():\n    old = 1\n    return "FOR UPDATE"\n'
        self.assertEqual(self.check(baseline.replace('old = 1','old = 2'),baseline),[])
        self.assertEqual(self.check(baseline,baseline),[])

    def test_owner_and_retired_ratchet(self):
        self.config({'exclusive_sql_owners':[{'table':'active_jobs','owners':['src/owner.py']}], 'retired_tables':{'scope_prefixes':['src/'],'tables':['old_jobs']}})
        code='def write():\n    return "SELECT * FROM old_jobs"\n'
        self.write('src/api.py',code)
        self.git('add','.')
        self.git('commit','-m','baseline')
        self.write('src/api.py',code.replace('SELECT *','SELECT id'))
        findings=run_topology_guard(self.root,['src/api.py'],base='HEAD')
        self.assertEqual([f.rule_id for f in findings],['guard.data-modeling'])
        self.write('src/api.py','def write():\n    return "SELECT * FROM active_jobs"\n')
        findings=run_topology_guard(self.root,['src/api.py'],base='HEAD')
        self.assertEqual([f.rule_id for f in findings],['guard.single-source-of-truth'])
        self.write('src/owner.py','def write():\n    return "SELECT * FROM active_jobs"\n')
        self.assertEqual(run_topology_guard(self.root,['src/owner.py'],base='HEAD'),[])
        self.write('src/api.py','def write():\n    return 1\n')
        self.assertEqual(run_topology_guard(self.root,['src/api.py'],base='HEAD'),[])

    def test_forbidden_patterns_layering_and_exemption(self):
        self.config({'forbidden_patterns':[{'scope_prefixes':['src/'],'literal_or_regex':'/private-state','literal':True}], 'forbidden_imports':[{'from':'src/','to':'internal.storage'}]})
        self.write('src/api.py','from internal.storage import rows\nurl = "/private-state"\n')
        self.assertEqual(len(run_topology_guard(self.root,['src/api.py'])),2)
        self.assertEqual(self.check('def get_item():\n    return "FOR UPDATE"  # guard-exempt guard.architecture-simplicity: documented migration reader\n'),[])

    def test_swift_base_signatures_and_configured_history(self):
        code='func prepareConversation() async {\n    let a = await api.fetchHistory(id: key)\n    let resetGeneration = a.generation\n    let b = await api.fetchHistory(id: key)\n}\n'
        self.assertEqual(len(_check_swift_file('app/View.swift',code,None,None)),1)
        self.assertEqual(len(_check_swift_file('app/View.swift',code,None,None,True)),3)
        current=code.replace('a.generation','a.version')
        self.assertEqual(_check_swift_file('app/View.swift',current,code,_compute_changed_lines(code,current)),[])

    def test_discovery_does_not_invent_owner_rules(self):
        self.write('packages/api/main.py','import storage.repository\n')
        self.write('packages/api/AGENTS.md','# API contract\n')
        self.write('packages/storage/__init__.py','')
        modules=discover_topology(self.root)['modules']
        api=next(m for m in modules if m['path']=='packages/api')
        self.assertEqual(api['dependencies'],['packages/storage'])
        self.assertEqual(api['boundary_docs'],['packages/api/AGENTS.md'])
        self.assertNotIn('exclusive_sql_owners',discover_topology(self.root))


    def test_configured_exemptions_and_retired_changed_line_precision(self):
        config = GuardConfig(exempt_tag='custom-exempt', topology={
            'exclusive_sql_owners': [{'table': 'owned', 'owners': ['src/owner.py']}],
            'retired_tables': [{'tables': ['old']}],
            'forbidden_patterns': [{'literal_or_regex': '/reverse', 'literal': True}],
            'forbidden_imports': [{'imports': ['internal']}],
            'module_boundaries': [{'from': 'src/', 'to': 'private'}],
        })
        text = '\n'.join([
            'import internal # custom-exempt guard.service-boundaries: approved fixture boundary',
            'import private # custom-exempt guard.service-boundaries: approved fixture boundary',
            'query = "SELECT * FROM owned" # custom-exempt guard.single-source-of-truth: approved owner projection',
            'endpoint = "/reverse" # custom-exempt guard.single-source-of-truth: approved fixture boundary',
            'query = """',
            '-- SELECT * FROM old',
            'SELECT * FROM old -- custom-exempt guard.data-modeling: approved fixture boundary',
            'SELECT * FROM old',
            '"""',
        ])
        findings = check_configured('src/api.py', text, None, None, config)
        self.assertEqual([(f.line, f.rule_id) for f in findings], [(8, 'guard.data-modeling')])
        self.assertEqual(check_configured('src/api.py', text, text, {5, 9}, config), [])
        self.assertEqual([f.line for f in check_configured('src/api.py', text, text, {8}, config)], [8])

    def test_configured_findings_follow_source_order(self):
        config = GuardConfig(topology={
            'retired_tables': [{'tables': ['old']}],
            'forbidden_patterns': [{'literal_or_regex': '/reverse'}],
        })
        findings = check_configured('src/api.py', 'endpoint = "/reverse"\nquery = "SELECT * FROM old"', None, None, config)
        self.assertEqual([f.line for f in findings], [1, 2])
