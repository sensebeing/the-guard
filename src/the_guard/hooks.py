"""Git pre-push integration anchored to the actual ref updates supplied by Git."""
import json
from pathlib import Path
import subprocess
import sys
from .config import load_config
from .review.repository import collect_review_scope, resolve_or_carry_forward_receipt
from .topology import run_topology_guard


def install_hooks(root):
    root = Path(root).resolve()
    proc = subprocess.run(['git','rev-parse','--git-path','hooks'], cwd=root, capture_output=True, text=True, check=True)
    directory = Path(proc.stdout.strip())
    if not directory.is_absolute():
        directory = root/directory
    hook = directory/'pre-push'
    content = '#!/bin/sh\n# Managed by the-guard\nexec guard hook main-push-gate --repo-root "$(git rev-parse --show-toplevel)"\n'
    if hook.exists() and hook.read_text()!=content:
        raise ValueError(f'Refusing to overwrite existing hook: {hook}; chain guard hook main-push-gate explicitly')
    directory.mkdir(parents=True, exist_ok=True)
    hook.write_text(content)
    hook.chmod(0o755)
    return hook


def main_push_gate(root, input_text):
    root = Path(root).resolve()
    config = load_config(root)
    head = subprocess.run(['git','rev-parse','HEAD'],cwd=root,capture_output=True,text=True,check=True).stdout.strip()
    for line in input_text.splitlines():
        fields = line.split()
        if len(fields)!=4:
            raise ValueError('Invalid git pre-push input')
        _,local_sha,remote_ref,remote_sha = fields
        if remote_ref!='refs/heads/'+config.main_branch or set(local_sha)=={'0'}:
            continue
        if local_sha!=head:
            raise ValueError('Review the actual pushed commit as HEAD before pushing the protected branch')
        if set(remote_sha)=={'0'}:
            raise ValueError('Protected branch creation needs an explicit reviewed baseline; initialize the remote before enabling this hook')
        scope = collect_review_scope(root, main_branch=remote_sha)
        if not scope.is_exempt and resolve_or_carry_forward_receipt(root,scope,persist=False) is None:
            print('NO-GO: missing review receipt for pushed range. Run guard review, address findings or submit --note evidence, then retry.',file=sys.stderr)
            return 2
    return 0


def edit_guard(root, input_text):
    payload = json.loads(input_text or '{}')
    tool_input = payload.get('tool_input', payload)
    path = tool_input.get('file_path') or tool_input.get('path')
    if not path:
        return 0
    findings = run_topology_guard(root,[path])
    for finding in findings:
        print(f'{finding.path}:{finding.line}: {finding.rule_id}: {finding.message}',file=sys.stderr)
    return 2 if findings else 0
