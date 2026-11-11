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


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] in {"review", "check-push", "probe"}:
        return review_main(values[1:] if values[0] == "review" else values)
    parser = build_parser()
    args = parser.parse_args(values)
    if args.command not in {"self-update", "status"}:
        maybe_trigger_background_auto_update()
    try:
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
                print('{"status": "GO"}' if args.json else "GO: policy valid")
                return 0
        base = resolve_changed_base(root, args.base, main_branch=cfg.main_branch)
        if args.command == "check" and args.policy_only:
            print('{"status": "GO"}' if args.json else "GO: policy valid")
            return 0
        findings = run_topology_guard(
            root, args.file or changed_files(root, base), base=base, config=cfg
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "status": "NO-GO" if findings else "GO",
                        "findings": [asdict(f) for f in findings],
                    },
                    indent=2,
                )
            )
        else:
            for f in findings:
                print(f"{f.path}:{f.line}: {f.rule_id}: {f.message}")
            print("## The Guard: NO-GO" if findings else "## The Guard: GO")
        return 2 if findings else 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        if args.command == "self-update" and getattr(args, "quiet", False):
            return 3
        print(f"guard: {exc}", file=sys.stderr)
        return 2 if isinstance(exc, (PolicyError, ValueError)) else 3


if __name__ == "__main__":
    raise SystemExit(main())
