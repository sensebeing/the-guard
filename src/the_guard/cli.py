"""Public command surface."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
from .config import load_config
from .policy import load_policy, PolicyError
from .topology import run_topology_guard, discover_topology
from .distill import distill
from .init import initialize
from .hooks import edit_guard, main_push_gate
from .review.cli import main as review_main
from .review.repository import resolve_base_ref


def changed_files(root, base):
    if subprocess.run(['git', 'rev-parse', '--verify', '--quiet', base], cwd=root, capture_output=True).returncode:
        try:
            base = resolve_base_ref(root, 'main')
        except RuntimeError:
            base = 'HEAD'
    proc = subprocess.run(['git','diff','--name-only','-z',base,'--'],cwd=root,capture_output=True,check=True)
    untracked = subprocess.run(['git','ls-files','--others','--exclude-standard','-z'],cwd=root,capture_output=True,check=True)
    return sorted(set(p.decode('utf-8') for p in (proc.stdout+untracked.stdout).split(b'\0') if p))


def main(argv=None):
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] in {'review','check-push','probe'}:
        return review_main(values[1:] if values[0]=='review' else values)
    parser = argparse.ArgumentParser(prog='guard',description='Upfront engineering policy and independent pre-merge review', epilog='Independent review: guard review [run|check-push|probe]. Aliases: guard check-push, guard probe.')
    sub = parser.add_subparsers(dest='command',required=True)
    for name in ('check','init','distill','topology','hook'):
        p = sub.add_parser(name)
        p.add_argument('--repo-root',default='.')
        if name in {'check','distill','topology'}:
            p.add_argument('--json',action='store_true')
        if name in {'check','topology'}:
            p.add_argument('--base',default='origin/main')
            p.add_argument('--file',action='append',default=[])
        if name=='check':
            p.add_argument('--policy-only',action='store_true')
        if name=='init':
            p.add_argument('--preset',choices=['core','full'],default='core')
            p.add_argument('--rules-dir',default='docs/engineering-quality/rules')
            p.add_argument('--manifest-file',default='AGENTS.md')
            p.add_argument('--with-hooks',action='store_true')
        if name=='topology':
            p.add_argument('action',choices=['sync','check'])
        if name=='hook':
            p.add_argument('action',choices=['edit-guard','main-push-gate'])
    args = parser.parse_args(values)
    root = Path(args.repo_root).resolve()
    try:
        if args.command=='init':
            print(json.dumps(initialize(root,preset=args.preset,rules_dir=args.rules_dir,manifest_file=args.manifest_file,with_hooks=args.with_hooks),indent=2))
            return 0
        if args.command=='distill':
            print(json.dumps(distill(root),indent=2))
            return 0
        if args.command=='hook':
            return (edit_guard if args.action=='edit-guard' else main_push_gate)(root,sys.stdin.read())
        if args.command=='topology' and args.action=='sync':
            path = root/'.guard/config.json'
            load_config(root)
            data = json.loads(path.read_text()) if path.exists() else {}
            data.setdefault('topology',{}).update(discover_topology(root))
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(data,indent=2)+'\n')
            print(json.dumps(data['topology'],indent=2))
            return 0
        if args.command=='check':
            load_policy(root)
            if args.policy_only:
                print('{"status": "GO"}' if args.json else 'GO: policy valid')
                return 0
        findings = run_topology_guard(root,args.file or changed_files(root,args.base),base=args.base)
        if args.json:
            print(json.dumps({'status':'NO-GO' if findings else 'GO','findings':[asdict(f) for f in findings]},indent=2))
        else:
            for f in findings:
                print(f'{f.path}:{f.line}: {f.rule_id}: {f.message}')
            print('## The Guard: NO-GO' if findings else '## The Guard: GO')
        return 2 if findings else 0
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print(f'guard: {exc}',file=sys.stderr)
        return 2 if isinstance(exc,PolicyError) else 3
