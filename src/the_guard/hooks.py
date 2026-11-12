"""Git pre-push integration anchored to the actual ref updates supplied by Git."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .config import load_config
from .review.repository import collect_review_scope, resolve_or_carry_forward_receipt
from .topology import run_topology_guard


HOOK_CONTENT = (
    "#!/bin/sh\n# Managed by the-guard\n"
    'exec guard hook main-push-gate --repo-root "$(git rev-parse --show-toplevel)"\n'
)


def hook_install_plan(root: Path | str) -> Path:
    """Resolve the managed pre-push hook without writing; refuse to replace a customized one."""
    root = Path(root).resolve()
    proc = subprocess.run(
        ["git", "rev-parse", "--git-path", "hooks"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    directory = Path(proc.stdout.strip())
    if not directory.is_absolute():
        directory = root / directory
    hook = directory / "pre-push"
    if hook.exists() and hook.read_text() != HOOK_CONTENT:
        raise ValueError(
            f"Refusing to overwrite existing hook: {hook}; chain guard hook main-push-gate explicitly"
        )
    return hook


def install_hooks(root: Path | str) -> Path:
    hook = hook_install_plan(root)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(HOOK_CONTENT)
    hook.chmod(0o755)
    return hook


def main_push_gate(root: Path | str, input_text: str) -> int:
    root = Path(root).resolve()
    config = load_config(root)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    for line in input_text.splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise ValueError("Invalid git pre-push input")
        _, local_sha, remote_ref, remote_sha = fields
        if remote_ref != "refs/heads/" + config.main_branch:
            continue
        if config.review.get("integration_mode") == "github-pr":
            print(
                "NO-GO: repository uses GitHub PR mode (review.integration_mode=github-pr); "
                "direct push to refs/heads/"
                + config.main_branch
                + " is disabled. Push a topic branch, open a PR with gh pr create, "
                "run guard review --github-pr <owner/repo#number>, and merge via gh pr merge.",
                file=sys.stderr,
            )
            return 2
        if set(local_sha) == {"0"}:
            continue
        if local_sha != head:
            raise ValueError(
                "Review the actual pushed commit as HEAD before pushing the protected branch"
            )
        if set(remote_sha) == {"0"}:
            raise ValueError(
                "Protected branch creation needs an explicit reviewed baseline; initialize the remote before enabling this hook"
            )
        scope = collect_review_scope(root, main_branch=remote_sha)
        if (
            not scope.is_exempt
            and resolve_or_carry_forward_receipt(root, scope, persist=False) is None
        ):
            print(
                "NO-GO: missing review receipt for pushed range. Run guard review, address findings or submit --note evidence, then retry.",
                file=sys.stderr,
            )
            return 2
    return 0


def edit_guard(root: Path | str, input_text: str) -> int:
    payload = json.loads(input_text or "{}")
    tool_input = payload.get("tool_input", payload)
    path = tool_input.get("file_path") or tool_input.get("path")
    if not path:
        return 0
    findings = run_topology_guard(root, [path])
    for finding in findings:
        print(
            f"{finding.path}:{finding.line}: {finding.rule_id}: {finding.message}",
            file=sys.stderr,
        )
    return 2 if findings else 0
