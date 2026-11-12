"""GitHub pull request review formatting and publication."""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contract import BlockingFinding

THE_GUARD_REVIEW_MARKER = "<!-- the-guard-review -->"
_GITHUB_API_URL = "https://api.github.com"
_GITHUB_APP_CONFIG_RELATIVE_PATH = Path(".config/the-guard/github-app.json")
_TARGET_RE = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^/#\s]+)#(?P<number>\d+)\s*$")
_URL_RE = re.compile(r"^https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s#]+)/pull/(?P<number>\d+)(?:/[^\s]*)?\s*$")


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
    keys = (name, *aliases)
    if isinstance(finding, dict):
        for key in keys:
            if key in finding:
                return finding[key]
        return ""
    for key in keys:
        value = getattr(finding, key, "")
        if value != "":
            return value
    return ""


def format_finding_comment(finding: BlockingFinding | dict[str, Any]) -> str:
    rule_id = _field(finding, "rule_id")
    file = _field(finding, "file", "path")
    line = _field(finding, "line")
    issue = _field(finding, "issue", "title", "why_it_violates", "message")
    required_fix = _field(finding, "required_fix", "required_refactoring")
    comment = f"{THE_GUARD_REVIEW_MARKER}\n**[{rule_id}]** `{file}:{line}`\n\n**Issue:** {issue}"
    if required_fix:
        comment += f"\n\n**Required fix:** {required_fix}"
    return comment


def build_github_review_payload(verdict: str, summary: str, findings: Sequence[BlockingFinding | dict[str, Any]], *, commit_id: str | None = None) -> dict[str, Any]:
    body = f"{THE_GUARD_REVIEW_MARKER}\n## The Guard Architecture Review: {verdict}\n\n{summary}"
    comments: list[dict[str, Any]] = []
    body_findings: list[str] = []
    for finding in findings:
        file = str(_field(finding, "file", "path") or "")
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


@dataclass(frozen=True)
class _GithubAppCredentials:
    app_id: str
    private_key: str = ""
    private_key_path: Path | None = None


def _load_github_app_credentials(config_path: Path | None) -> _GithubAppCredentials | None:
    app_id = os.environ.get("THE_GUARD_GITHUB_APP_ID", "").strip()
    private_key = os.environ.get("THE_GUARD_GITHUB_PRIVATE_KEY", "")
    key_path = os.environ.get("THE_GUARD_GITHUB_PRIVATE_KEY_PATH", "").strip()
    if app_id and (private_key.strip() or key_path):
        return _GithubAppCredentials(app_id, private_key, Path(key_path) if key_path else None)
    path = config_path or Path.home() / _GITHUB_APP_CONFIG_RELATIVE_PATH
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    file_app_id = str(data.get("app_id") or "").strip()
    file_private_key = str(data.get("private_key") or "")
    file_key_path = str(data.get("private_key_path") or "").strip()
    if not file_app_id or not (file_private_key.strip() or file_key_path):
        return None
    return _GithubAppCredentials(file_app_id, file_private_key, Path(file_key_path) if file_key_path else None)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _openssl_rs256_signature(key_file: Path, signing_input: bytes) -> bytes:
    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key_file)],
        input=signing_input,
        capture_output=True,
        check=True,
    )
    return result.stdout


def _sign_github_app_jwt(credentials: _GithubAppCredentials) -> str:
    now = int(time.time())
    header = _base64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode("utf-8"))
    claims = _base64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": credentials.app_id}, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header}.{claims}".encode("ascii")
    if credentials.private_key.strip():
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "github-app.pem"
            key_file.write_text(credentials.private_key, encoding="utf-8")
            key_file.chmod(0o600)
            signature = _openssl_rs256_signature(key_file, signing_input)
    elif credentials.private_key_path is not None:
        signature = _openssl_rs256_signature(credentials.private_key_path, signing_input)
    else:
        raise ValueError("GitHub App credentials have no private key")
    return f"{signing_input.decode('ascii')}.{_base64url(signature)}"


def _github_api_request(url: str, *, method: str, jwt: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "the-guard",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("GitHub API returned a non-object response")
    return data


def _github_app_installation_token(jwt: str, owner: str, repo: str) -> str:
    installation = _github_api_request(f"{_GITHUB_API_URL}/repos/{owner}/{repo}/installation", method="GET", jwt=jwt)
    access_token = _github_api_request(f"{_GITHUB_API_URL}/app/installations/{installation['id']}/access_tokens", method="POST", jwt=jwt)
    return str(access_token["token"])


def resolve_github_app_token(owner: str, repo: str, *, config_path: Path | None = None) -> str | None:
    """Return a GitHub App installation token for owner/repo, or None when the App is not configured.

    Credentials come from the THE_GUARD_GITHUB_APP_ID and THE_GUARD_GITHUB_PRIVATE_KEY /
    THE_GUARD_GITHUB_PRIVATE_KEY_PATH environment variables, or from config_path (default:
    ~/.config/the-guard/github-app.json). Unconfigured environments and repositories without
    an App installation resolve to None so publication falls back to GH_TOKEN / gh CLI auth.
    """
    try:
        credentials = _load_github_app_credentials(config_path)
        if credentials is None:
            return None
        return _github_app_installation_token(_sign_github_app_jwt(credentials), owner, repo)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None


def publish_github_pr_review(target: str, *, verdict: str, summary: str, findings: Sequence[BlockingFinding | dict[str, Any]], commit_id: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    owner, repo, pr_number = parse_github_pr_target(target)
    payload = build_github_review_payload(verdict, summary, findings, commit_id=commit_id)
    base = {"owner": owner, "repo": repo, "pr_number": pr_number, "inline_count": len(payload["comments"])}
    if dry_run:
        return {"status": "dry_run", **base, "delivery_mode": "dry_run", "payload": payload}
    app_token = resolve_github_app_token(owner, repo)
    process_env = {**os.environ, "GH_TOKEN": app_token} if app_token else None
    auth_mode = "github_app" if app_token else "gh_cli"
    command = ["gh", "api", f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews", "--method", "POST", "--input", "-"]
    result = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True, env=process_env)
    if result.returncode == 0:
        response = json.loads(result.stdout or "{}")
        return {"status": "published", **base, "delivery_mode": "inline", "auth_mode": auth_mode, "review_id": response.get("id"), "html_url": response.get("html_url")}
    stderr = result.stderr or ""
    if payload["comments"] and ("422" in stderr or "Unprocessable Entity" in stderr):
        fallback = dict(payload)
        fallback["comments"] = []
        inline_findings = [comment["body"] for comment in payload["comments"]]
        if inline_findings:
            fallback["body"] = payload["body"] + "\n\n" + "\n\n---\n\n".join(inline_findings)
        fallback_result = subprocess.run(command, input=json.dumps(fallback), text=True, capture_output=True, env=process_env)
        if fallback_result.returncode != 0:
            raise RuntimeError(fallback_result.stderr or stderr)
        response = json.loads(fallback_result.stdout or "{}")
        return {"status": "published", **base, "delivery_mode": "fallback_body", "auth_mode": auth_mode, "review_id": response.get("id"), "html_url": response.get("html_url")}
    raise RuntimeError(stderr)
