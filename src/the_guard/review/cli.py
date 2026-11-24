"""CLI for review execution, receipt verification and provider discovery."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from dataclasses import asdict

from .github import publish_github_pr_review

from ..auto_update import maybe_delegate_to_checkout, maybe_trigger_background_auto_update
from ..config import load_config
from ..policy import PolicyError, load_policy
from ..topology import run_topology_guard, check_contract_drift
from .contract import PROVIDERS, BlockingFinding, validate_review_output
from .session import ReviewResult, compute_review_contract_digest
from .engine import run_architecture_review
from .provider import probe_environment, resolve_provider_order
from .repository import (
    collect_review_scope,
    diagnose_missing_receipt,
    resolve_or_carry_forward_receipt,
)


def _parse_priority_order(value: str) -> list[str]:
    providers = list(dict.fromkeys(item.strip() for item in value.split(",")))
    if any(item not in PROVIDERS for item in providers):
        raise argparse.ArgumentTypeError("Unsupported provider in priority order")
    return providers


def _parse_provider_model(value: str) -> tuple[str, dict[str, str]]:
    provider, sep, settings = value.partition("=")
    model, colon, effort = settings.partition(":")
    if provider not in PROVIDERS or not sep or not model or (colon and not effort):
        raise argparse.ArgumentTypeError("Expected provider=model[:effort]")
    return provider, {"model": model, **({"effort": effort} if effort else {})}


def should_update_after_review(command: str, exit_code: int | None) -> bool:
    return (
        exit_code in {0, 2}
        and (not command or command.startswith("-") or command in {"run", "check-push"})
    )


def main(argv: list[str] | None = None, *, _from_root_cli: bool = False) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="guard review")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "check-push", "probe", "publish-github"):
        p = sub.add_parser(name)
        p.add_argument("--repo-root", default=".")
        p.add_argument("--main-branch", default=None)
        if name == "check-push":
            p.add_argument("--head", default="HEAD", help="Commit or ref to verify (default: HEAD)")
        if name in ("run", "probe"):
            p.add_argument("--reprobe", action="store_true")
        if name == "publish-github":
            p.add_argument("--pr", required=True)
            p.add_argument("--input-json", default=None)
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--json", action="store_true")
        if name == "run":
            p.add_argument("--provider", choices=PROVIDERS)
            p.add_argument("--force", action="store_true")
            p.add_argument("--note", "--rebuttal", default="")
            p.add_argument("--json", action="store_true")
            p.add_argument("--github-pr", default=None, help="Publish attributable review to GitHub PR (owner/repo#number or URL)")
            p.add_argument("--github-dry-run", action="store_true")
        if name == "probe":
            p.add_argument("--priority-order", type=_parse_priority_order)
            p.add_argument(
                "--provider-model", type=_parse_provider_model, action="append"
            )
    args = parser.parse_args(values if values and not values[0].startswith("-") else ["run", *values])
    root = Path(args.repo_root).resolve()
    if not _from_root_cli:
        delegated = maybe_delegate_to_checkout(["review", *values], active_repo_root=root)
        if delegated is not None:
            return delegated
    exit_code = None
    try:
        exit_code = _run_review(args, root)
        return exit_code
    finally:
        if not _from_root_cli and should_update_after_review(args.command, exit_code):
            maybe_trigger_background_auto_update(active_repo_root=root)


def _run_review(args: argparse.Namespace, root: Path) -> int:
    try:
        head_ref = args.head if args.command == "check-push" else "HEAD"
        config = load_config(root, revision=head_ref if args.command == "check-push" else None)
        main_branch = args.main_branch or config.main_branch
        if args.command == "publish-github":
            if args.input_json == "-":
                data = json.loads(sys.stdin.read())
            elif args.input_json is not None:
                data = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
            else:
                scope = collect_review_scope(root, main_branch=main_branch)
                receipt = resolve_or_carry_forward_receipt(root, scope)
                if receipt is None:
                    raise ValueError(
                        "No review verdict provided via --input-json and no cached review receipt found for current HEAD; run `guard review run --github-pr <pr>` or pass `--input-json <path|->`."
                    )
                data = {
                    "verdict": receipt["verdict"],
                    "summary": receipt["summary"],
                    "blocking_findings": [],
                    "reviewed_head_sha": scope.head_sha,
                }
            output = validate_review_output(data)
            receipt = publish_github_pr_review(
                args.pr, verdict=output.verdict, summary=output.summary,
                findings=output.blocking_findings, commit_id=output.reviewed_head_sha,
                dry_run=args.dry_run,
            )
            if args.json:
                print(json.dumps(receipt, indent=2))
            else:
                print(f"GitHub review: {receipt['status']} ({receipt['delivery_mode']})")
            return 0
        if args.command == "probe":
            if args.reprobe or args.priority_order or args.provider_model:
                result = probe_environment(
                    root,
                    priority_order=args.priority_order,
                    provider_models=dict(args.provider_model or []),
                )
            else:
                _, result = resolve_provider_order(root)
            print(json.dumps(result, indent=2))
            return 0 if result.get("preferred_provider") else 3
        if (
            args.command == "check-push"
            and os.environ.get("THE_GUARD_REVIEW_ACTIVE") == "1"
        ):
            return 0
        scope = collect_review_scope(root, main_branch=main_branch, head_ref=head_ref)
        snapshot_config = load_config(root, revision=scope.head_sha)
        snapshot_policy = load_policy(
            root, revision=scope.head_sha, config=snapshot_config
        )
        static_findings = [
            *run_topology_guard(
                root,
                list(scope.changed_files),
                base=scope.merge_base,
                config=snapshot_config,
                revision=scope.head_sha,
            ),
            *check_contract_drift(
                root,
                list(scope.changed_files),
                base=scope.merge_base,
                config=snapshot_config,
                revision=scope.head_sha,
            ),
        ]
        if args.command == "check-push":
            if static_findings:
                for finding in static_findings:
                    print(
                        f"{finding.path}:{finding.line}: {finding.rule_id}: {finding.message}",
                        file=sys.stderr,
                    )
                print(
                    "## Architecture Review Gate: NO-GO (deterministic check failed)",
                    file=sys.stderr,
                )
                return 2
            if scope.is_exempt:
                print(f"EXEMPT (digest={scope.patch_digest[:12]})")
                return 0
            current_contract_digest = compute_review_contract_digest(
                snapshot_policy, snapshot_config, repo_root=root, revision=scope.head_sha, scope=scope,
            )
            receipt = resolve_or_carry_forward_receipt(
                root,
                scope,
                persist=False,
                config=snapshot_config,
                policy=snapshot_policy,
                contract_digest=current_contract_digest,
            )
            if receipt is not None:
                print(
                    f"VERIFIED (provider={receipt.get('provider', 'unknown')}, digest={scope.patch_digest[:12]})"
                )
                return 0
            print(
                diagnose_missing_receipt(root, scope, contract_digest=current_contract_digest),
                file=sys.stderr,
            )
            return 2
        if static_findings:
            result = ReviewResult(
                verdict="NO-GO",
                summary="Deterministic check failed.",
                blocking_findings=tuple(
                    BlockingFinding(
                        rule_id=f.rule_id,
                        file=f.path,
                        line=f.line,
                        issue=f.message,
                        required_fix="Resolve deterministic policy/topology/contract-drift violation before independent review.",
                        finding_origin="initial_full_scope",
                    )
                    for f in static_findings
                ),
                provider="static",
                mode="static",
                patch_digest=scope.patch_digest,
                head_sha=scope.head_sha,
                merge_base=scope.merge_base,
            )
        else:
            result = run_architecture_review(
                root,
                scope=scope,
                config=snapshot_config,
                policy=snapshot_policy,
                main_branch=main_branch,
                explicit_provider=args.provider,
                reprobe=args.reprobe,
                force=args.force,
                reviewer_note=args.note,
            )
        gh_receipt = None
        if args.github_pr:
            gh_receipt = publish_github_pr_review(args.github_pr, verdict=result.verdict, summary=result.summary, findings=result.blocking_findings, commit_id=getattr(result, "reviewed_head_sha", None) or getattr(result, "head_sha", None), dry_run=args.github_dry_run)
        if args.json:
            output = asdict(result)
            output["reviewed_head_sha"] = output.pop("head_sha")
            print(json.dumps({**output, **({"github_review": gh_receipt} if gh_receipt else {})}, indent=2))
            return 0 if result.verdict == "GO" else 2
        lines = [
            f"## Architecture Review: {result.verdict}",
            "",
            f"- provider: {result.provider}",
            f"- mode: {result.mode}",
            f"- patch_digest: {result.patch_digest[:12]}",
        ]
        if result.session_id:
            lines.append(f"- session_id: {result.session_id}")
        if result.log_path:
            lines.append(f"- log_path: {result.log_path}")
        lines.append(f"- summary: {result.summary}")
        if result.verdict != "GO":
            lines.extend([f"- blocking_findings: {len(result.blocking_findings)}", ""])
            for index, finding in enumerate(result.blocking_findings, 1):
                lines.append(
                    f"{index}. [{finding.rule_id} | {finding.finding_origin}] {finding.file}:{finding.line}\n   - Issue: {finding.issue}\n   - Required fix: {finding.required_fix}"
                )
            lines.extend(
                [
                    "",
                    "## Action Required (Do Not Pause Agent Loop)",
                    "Address the complete pattern across the diff; do not leave transitional shells. Commit fixes or provide factual --note rebuttal evidence and rerun `guard review`.",
                ]
            )
        print(
            "\n".join(lines), file=sys.stdout if result.verdict == "GO" else sys.stderr
        )
        return 0 if result.verdict == "GO" else 2
    except PolicyError as exc:
        sys.stderr.write(f"## Architecture Review: NO-GO\n\n- policy error: {exc}\n")
        return 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"CHECKER_UNAVAILABLE: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
