"""CLI for review execution, receipt verification and provider discovery."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from dataclasses import asdict

from .github import publish_github_pr_review

from ..config import load_config
from ..policy import PolicyError
from .contract import PROVIDERS, validate_review_output
from .engine import run_architecture_review
from .provider import probe_environment, resolve_provider_order
from .repository import collect_review_scope, resolve_or_carry_forward_receipt


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


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if not values or values[0].startswith("-"):
        values.insert(0, "run")
    parser = argparse.ArgumentParser(prog="guard review")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "check-push", "probe", "publish-github"):
        p = sub.add_parser(name)
        p.add_argument("--repo-root", default=".")
        p.add_argument("--main-branch", default=None)
        if name in ("run", "probe"):
            p.add_argument("--reprobe", action="store_true")
        if name == "publish-github":
            p.add_argument("--pr", required=True)
            p.add_argument("--input-json", default="-")
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
    args = parser.parse_args(values)
    root = Path(args.repo_root).resolve()
    try:
        config = load_config(root)
        main_branch = args.main_branch or config.main_branch
        if args.command == "publish-github":
            source = sys.stdin.read() if args.input_json == "-" else Path(args.input_json).read_text()
            data = json.loads(source)
            data = validate_review_output(data)
            receipt = publish_github_pr_review(args.pr, verdict=data["verdict"], summary=data["summary"], findings=data.get("blocking_findings", ()), commit_id=data.get("reviewed_head_sha"), dry_run=args.dry_run)
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
        if args.command == "check-push":
            if os.environ.get("THE_GUARD_REVIEW_ACTIVE") == "1":
                return 0
            scope = collect_review_scope(root, main_branch=main_branch)
            if scope.is_exempt:
                print(f"EXEMPT (digest={scope.patch_digest[:12]})")
                return 0
            receipt = resolve_or_carry_forward_receipt(root, scope, persist=False)
            if receipt is not None:
                print(
                    f"VERIFIED (provider={receipt.get('provider', 'unknown')}, digest={scope.patch_digest[:12]})"
                )
                return 0
            sys.stderr.write(
                f"## Architecture Review Gate: NO-GO (missing receipt for patch_digest={scope.patch_digest[:12]})\n\n"
                "Run `guard review`; fix all related findings or submit evidence "
                "with --note, then retry the push.\n"
            )
            return 2
        result = run_architecture_review(
            root,
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
