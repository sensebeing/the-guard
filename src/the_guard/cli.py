"""Public command surface."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from .auto_update import (
    guard_status,
    maybe_trigger_background_auto_update,
    self_update,
)
from .config import load_config
from .distill import distill
from .git_helpers import changed_files, resolve_changed_base
from .hooks import edit_guard, main_push_gate
from .init import initialize
from .policy import PolicyError, load_policy
from .review.cli import main as review_main
from .review.github import publish_github_pr_review
from .topology import discover_topology, run_topology_guard


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="guard",
        description="Upfront engineering policy and independent pre-merge review",
        epilog="Independent review: guard review [run|check-push|probe]. Aliases: guard check-push, guard probe.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "init", "distill", "topology", "hook", "status", "self-update"):
        p = sub.add_parser(name)
        if name != "self-update":
            p.add_argument("--repo-root", default=".")
        if name in {"check", "distill", "topology", "status", "self-update"}:
            p.add_argument("--json", action="store_true")
        if name in {"check", "topology"}:
            p.add_argument("--base", default=None)
            p.add_argument("--file", action="append", default=[])
        if name == "check":
            p.add_argument("--policy-only", action="store_true")
            p.add_argument("--github-pr", default=None)
            p.add_argument("--github-dry-run", action="store_true")
        if name == "init":
            p.add_argument("--preset", choices=["core", "full"], default="core")
            p.add_argument("--rules-dir", default="docs/standards/rules")
            p.add_argument("--manifest-file", default="AGENTS.md")
            p.add_argument("--with-hooks", action="store_true")
        if name == "topology":
            p.add_argument("action", choices=["sync", "check"])
        if name == "hook":
            p.add_argument("action", choices=["edit-guard", "main-push-gate"])
        if name in {"status", "self-update"}:
            p.add_argument("--checkout-dir", default=None)
            p.add_argument("--home-dir", default=None)
            p.add_argument("--state-dir", default=None)
            p.add_argument("--gemini-dir", default=None)
        if name == "self-update":
            p.add_argument("--quiet", action="store_true")
    return parser


def _extract_repo_root(values: list[str]) -> Path:
    for idx, token in enumerate(values):
        if token == "--repo-root" and idx + 1 < len(values):
            return Path(values[idx + 1]).resolve()
        if token.startswith("--repo-root="):
            return Path(token.partition("=")[2]).resolve()
    return Path(".").resolve()


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    command = values[0] if values else ""
    root = _extract_repo_root(values)
    should_trigger_auto_update = False
    try:
        if command in {"review", "check-push", "probe"}:
            sub_values = values[1:] if command == "review" else values
            code = review_main(sub_values)
            sub_cmd = (
                sub_values[0]
                if sub_values and not sub_values[0].startswith("-")
                else "run"
            )
            should_trigger_auto_update = (
                sub_cmd in {"run", "check-push"} and code in {0, 2}
            )
            return code
        parser = build_parser()
        args = parser.parse_args(values)
        command = args.command
        if args.command == "self-update":
            report = self_update(
                checkout_dir=args.checkout_dir,
                home_dir=args.home_dir,
                state_dir=args.state_dir,
                gemini_dir=args.gemini_dir,
            )
            if not args.quiet:
                if args.json:
                    print(json.dumps(report, indent=2))
                else:
                    print(
                        f"guard self-update: {report['status']} "
                        f"(source={report['source_status']}, head={report.get('head_sha') or 'none'})"
                    )
            return 0
        if args.command == "status":
            report = guard_status(
                home_dir=args.home_dir,
                state_dir=args.state_dir,
                gemini_dir=args.gemini_dir,
                checkout_dir=args.checkout_dir,
            )
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                auto_state = (report.get("auto_update") or {}).get("status", "never")
                print(
                    f"guard {report['version']}: checkout={report.get('checkout') or 'none'}, "
                    f"auto_update={auto_state}"
                )
            return 0
        root = Path(args.repo_root).resolve()
        if args.command == "init":
            print(
                json.dumps(
                    initialize(
                        root,
                        preset=args.preset,
                        rules_dir=args.rules_dir,
                        manifest_file=args.manifest_file,
                        with_hooks=args.with_hooks,
                    ),
                    indent=2,
                )
            )
            return 0
        if args.command == "distill":
            print(json.dumps(distill(root), indent=2))
            return 0
        if args.command == "hook":
            return (edit_guard if args.action == "edit-guard" else main_push_gate)(
                root, sys.stdin.read()
            )
        if args.command == "topology" and args.action == "sync":
            path = root / ".guard/config.json"
            load_config(root)
            data = json.loads(path.read_text()) if path.exists() else {}
            data.setdefault("topology", {}).update(discover_topology(root))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2) + "\n")
            print(json.dumps(data["topology"], indent=2))
            return 0
        cfg = load_config(root)
        if args.command == "check":
            load_policy(root)
            if args.policy_only and args.base is None:
                should_trigger_auto_update = True
                gh_receipt = None
                if args.github_pr:
                    head_sha = None
                    try:
                        head_sha = subprocess.run(
                            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
                            capture_output=True, text=True,
                        ).stdout.strip()
                    except (OSError, subprocess.SubprocessError):
                        head_sha = None
                    base_name = args.base or cfg.main_branch
                    gh_receipt = publish_github_pr_review(
                        args.github_pr,
                        verdict="GO",
                        summary=f"Deterministic policy and topology check passed with 0 findings (base {base_name}).",
                        findings=[], commit_id=head_sha, dry_run=args.github_dry_run,
                    )
                if args.json:
                    output = {"status": "GO"}
                    if gh_receipt is not None:
                        output["github_review"] = gh_receipt
                    print(json.dumps(output))
                else:
                    print("GO: policy valid")
                return 0
        base = resolve_changed_base(root, args.base, main_branch=cfg.main_branch)
        if args.command == "check" and args.policy_only:
            should_trigger_auto_update = True
            gh_receipt = None
            if args.github_pr:
                head_sha = None
                try:
                    head_sha = subprocess.run(
                        ["git", "rev-parse", "HEAD"], cwd=root, check=True,
                        capture_output=True, text=True,
                    ).stdout.strip()
                except (OSError, subprocess.SubprocessError):
                    head_sha = None
                gh_receipt = publish_github_pr_review(
                    args.github_pr,
                    verdict="GO",
                    summary=f"Deterministic policy and topology check passed with 0 findings (base {base}).",
                    findings=[], commit_id=head_sha, dry_run=args.github_dry_run,
                )
            if args.json:
                output = {"status": "GO"}
                if gh_receipt is not None:
                    output["github_review"] = gh_receipt
                print(json.dumps(output))
            else:
                print("GO: policy valid")
            return 0
        findings = run_topology_guard(
            root, args.file or changed_files(root, base), base=base, config=cfg
        )
        should_trigger_auto_update = args.command == "check"
        gh_receipt = None
        if args.command == "check" and args.github_pr:
            head_sha = None
            try:
                head_sha = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=root, check=True,
                    capture_output=True, text=True,
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                head_sha = None
            verdict = "NO-GO" if findings else "GO"
            summary = (
                f"Deterministic policy and topology check found {len(findings)} blocking violation(s) (base {base})."
                if findings
                else f"Deterministic policy and topology check passed with 0 findings (base {base})."
            )
            gh_receipt = publish_github_pr_review(
                args.github_pr, verdict=verdict, summary=summary, findings=findings,
                commit_id=head_sha, dry_run=args.github_dry_run,
            )
        if args.json:
            output = {
                "status": "NO-GO" if findings else "GO",
                "findings": [asdict(f) for f in findings],
            }
            if gh_receipt is not None:
                output["github_review"] = gh_receipt
            print(json.dumps(output, indent=2))
        else:
            for f in findings:
                print(f"{f.path}:{f.line}: {f.rule_id}: {f.message}")
            print("## The Guard: NO-GO" if findings else "## The Guard: GO")
        return 2 if findings else 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        if command == "self-update" and "--quiet" in values:
            return 3
        print(f"guard: {exc}", file=sys.stderr)
        return 2 if isinstance(exc, (PolicyError, ValueError)) else 3
    finally:
        if should_trigger_auto_update:
            maybe_trigger_background_auto_update(active_repo_root=root)


if __name__ == "__main__":
    raise SystemExit(main())
