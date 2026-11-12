"""GitHub pull request review formatting and publication."""
from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from typing import Any

from .session import BlockingFinding

THE_GUARD_REVIEW_MARKER = "<!-- the-guard-review -->"
_TARGET_RE = re.compile(r"^(?P<owner>[^/\\s]+)/(?P<repo>[^/#\\s]+)#(?P<number>\\d+)\\s*$")
_URL_RE = re.compile(r"^https://github\\.com/(?P<owner>[^/\\s]+)/(?P<repo>[^/\\s#]+)/pull/(?P<number>\\d+)(?:/[^\\s]*)?\\s*$")


def parse_github_pr_target(target: str) -> tuple[str, str, int]:
    value = str(target).strip()
    match = _TARGET_RE.match(value) or _URL_RE.match(value)
    if not match:
        raise ValueError("Invalid GitHub PR target; expected owner/repo#number or https://github.com/owner/repo/pull/number")
    number = int(match.group("number"))
    if number <= 0:
        raise ValueError("GitHub PR number must be greater than zero")
    return match.group("owner"), match.group("repo"), number


def _field(finding: BlockingFinding | dict[str, Any], name: str, *aliases: str) -> Any:
    if isinstance(finding, dict):
        for key in (name, *aliases):
            if key in finding:
                return finding[key]
        return ""
    return getattr(finding, name, "")


def format_finding_comment(finding: BlockingFinding | dict[str, Any]) -> str:
    rule_id = _field(finding, "rule_id")
    file = _field(finding, "file")
    line = _field(finding, "line")
    issue = _field(finding, "issue", "title", "why_it_violates")
    required_fix = _field(finding, "required_fix", "required_refactoring")
    return f"{THE_GUARD_REVIEW_MARKER}\n**[{rule_id}]** `{file}:{line}`\n\n**Issue:** {issue}\n\n**Required fix:** {required_fix}"


def build_github_review_payload(verdict: str, summary: str, findings: Sequence[BlockingFinding | dict[str, Any]], *, commit_id: str | None = None) -> dict[str, Any]:
    body = f"{THE_GUARD_REVIEW_MARKER}\n## The Guard Architecture Review: {verdict}\n\n{summary}"
    comments: list[dict[str, Any]] = []
    body_findings: list[str] = []
    for finding in findings:
        file = str(_field(finding, "file") or "")
        try:
            line = int(_field(finding, "line") or 0)
        except (TypeError, ValueError):
            line = 0
        formatted = format_finding_comment(finding)
        if file and line > 0:
            comments.append({"path": file, "line": line, "side": "RIGHT", "body": formatted})
        else:
            body_findings.append(formatted)
    if body_findings:
        body += "\n\n" + "\n\n---\n\n".join(body_findings)
    payload: dict[str, Any] = {"event": "COMMENT", "body": body, "comments": comments}
    if commit_id and re.fullmatch(r"[0-9a-fA-F]{40}", commit_id):
        payload["commit_id"] = commit_id
    return payload


def publish_github_pr_review(target: str, *, verdict: str, summary: str, findings: Sequence[BlockingFinding | dict[str, Any]], commit_id: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    owner, repo, pr_number = parse_github_pr_target(target)
    payload = build_github_review_payload(verdict, summary, findings, commit_id=commit_id)
    base = {"owner": owner, "repo": repo, "pr_number": pr_number, "inline_count": len(payload["comments"])}
    if dry_run:
        return {"status": "dry_run", **base, "delivery_mode": "dry_run", "payload": payload}
    command = ["gh", "api", f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews", "--method", "POST", "--input", "-"]
    result = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True)
    if result.returncode == 0:
        response = json.loads(result.stdout or "{}")
        return {"status": "published", **base, "delivery_mode": "inline", "review_id": response.get("id"), "html_url": response.get("html_url")}
    stderr = result.stderr or ""
    if payload["comments"] and ("422" in stderr or "Unprocessable Entity" in stderr):
        fallback = dict(payload)
        fallback["comments"] = []
        all_findings = [format_finding_comment(f) for f in findings]
        if all_findings:
            fallback["body"] = payload["body"] + "\n\n" + "\n\n---\n\n".join(all_findings)
        fallback_result = subprocess.run(command, input=json.dumps(fallback), text=True, capture_output=True)
        if fallback_result.returncode != 0:
            raise RuntimeError(fallback_result.stderr or stderr)
        response = json.loads(fallback_result.stdout or "{}")
        return {"status": "published", **base, "delivery_mode": "fallback_body", "review_id": response.get("id"), "html_url": response.get("html_url")}
    raise RuntimeError(stderr)
