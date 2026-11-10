"""CLI for review execution, receipt verification and provider discovery."""
import argparse
import json
from pathlib import Path
import sys
from .contract import PROVIDERS
from .environment import review_environment
from .engine import run_architecture_review
from .provider import probe_environment, resolve_provider_order
from .repository import collect_review_scope, resolve_or_carry_forward_receipt
from ..config import load_config
from ..policy import PolicyError


def _parse_priority_order(value):
    providers = list(dict.fromkeys(item.strip() for item in value.split(',')))
    if any(item not in PROVIDERS for item in providers):
        raise argparse.ArgumentTypeError('Unsupported provider in priority order')
    return providers


def _parse_provider_model(value):
    provider,sep,settings = value.partition('=')
    model,colon,effort = settings.partition(':')
    if provider not in PROVIDERS or not sep or not model or (colon and not effort):
        raise argparse.ArgumentTypeError('Expected provider=model[:effort]')
    return provider, {'model':model, **({'effort':effort} if effort else {})}


def main(argv=None):
    values = list(sys.argv[1:] if argv is None else argv)
    if not values or values[0].startswith('-'):
        values.insert(0,'run')
    parser = argparse.ArgumentParser(prog='guard review')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('run','check-push','probe'):
        p = sub.add_parser(name)
        p.add_argument('--repo-root',default='.')
        p.add_argument('--main-branch',default=None)
        if name in ('run','probe'):
            p.add_argument('--reprobe',action='store_true')
        if name=='run':
            p.add_argument('--provider',choices=PROVIDERS)
            p.add_argument('--force',action='store_true')
            p.add_argument('--note','--rebuttal',default='')
        if name=='probe':
            p.add_argument('--priority-order',type=_parse_priority_order)
            p.add_argument('--provider-model',type=_parse_provider_model,action='append')
    args = parser.parse_args(values)
    root = Path(args.repo_root).resolve()
    try:
        config = load_config(root)
        main_branch = args.main_branch or config.main_branch
        if args.command=='probe':
            if args.reprobe or args.priority_order or args.provider_model:
                result = probe_environment(root, priority_order=args.priority_order,provider_models=dict(args.provider_model or []))
            else:
                _,result = resolve_provider_order(root)
            print(json.dumps(result,indent=2))
            return 0 if result.get('preferred_provider') else 3
        if args.command=='check-push':
            if review_environment().get('ALTER_ARCH_REVIEW_ACTIVE') == '1':
                return 0
            scope = collect_review_scope(root,main_branch=main_branch)
            receipt = None if scope.is_exempt else resolve_or_carry_forward_receipt(root,scope,persist=False)
            if scope.is_exempt or receipt:
                print('EXEMPT' if scope.is_exempt else 'VERIFIED')
                return 0
            print('NO-GO: no matching receipt. Run guard review; fix all related findings or submit evidence with --note, then retry.',file=sys.stderr)
            return 2
        result = run_architecture_review(root,main_branch=main_branch,explicit_provider=args.provider,reprobe=args.reprobe,force=args.force,reviewer_note=args.note)
        print(f'Architecture Review: {result.verdict}\nprovider: {result.provider}\nmode: {result.mode}\npatch_digest: {result.patch_digest}\nsummary: {result.summary}\nlog_path: {result.log_path}')
        for finding in result.blocking_findings:
            print(f'{finding.file}:{finding.line} [{finding.rule_id} | {finding.finding_origin}] {finding.issue}\nRequired fix: {finding.required_fix}')
        if result.verdict!='GO':
            print('Address the complete pattern across the diff; do not leave transitional shells. Commit fixes or provide factual --note rebuttal evidence and rerun review.',file=sys.stderr)
        return 0 if result.verdict=='GO' else 2
    except PolicyError as exc:
        print(f'NO-GO: policy error: {exc}',file=sys.stderr)
        return 2
    except (OSError,ValueError,RuntimeError) as exc:
        print(f'CHECKER_UNAVAILABLE: {exc}',file=sys.stderr)
        return 3
