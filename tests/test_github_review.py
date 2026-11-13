"""Focused contracts for GitHub PR review publication."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from types import SimpleNamespace
from unittest import mock

from the_guard.review.cli import main as review_main
from the_guard.cli import main as guard_main
from the_guard.finding import Finding
from the_guard.review.github import (
    build_github_review_payload,
    parse_github_pr_target,
    publish_github_pr_review,
    resolve_github_app_token,
)

EMPTY_APP_ENV = {
    "THE_GUARD_GITHUB_APP_ID": "",
    "THE_GUARD_GITHUB_PRIVATE_KEY": "",
    "THE_GUARD_GITHUB_PRIVATE_KEY_PATH": "",
}


class _FakeUrlResponse:
    def __init__(self, payload: dict[str, Any] | bytes) -> None:
        self._body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeUrlResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _jwt_claims(jwt: str) -> dict[str, Any]:
    claims = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))


class GithubReviewTests(unittest.TestCase):
    def test_cli_publish_uses_cached_receipt_when_stdin_is_idle(self) -> None:
        cached = {
            "verdict": "GO",
            "summary": "cached review",
            "head_sha": "a" * 40,
        }
        published = {"status": "dry_run", "delivery_mode": "dry_run"}
        output = io.StringIO()
        with mock.patch("the_guard.review.cli.load_config", return_value=SimpleNamespace(main_branch="main")), \
                mock.patch("the_guard.review.cli._read_nonblocking_stdin_text", return_value=None), \
                mock.patch("the_guard.review.cli.collect_review_scope", return_value=object()), \
                mock.patch("the_guard.review.cli.resolve_or_carry_forward_receipt", return_value=cached), \
                mock.patch("the_guard.review.cli.publish_github_pr_review", return_value=published) as publish, \
                contextlib.redirect_stdout(output):
            code = review_main(["publish-github", "--pr", "sensebeing/the-guard#1", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), published)
        self.assertEqual(publish.call_args.kwargs["commit_id"], "a" * 40)

    def test_cli_publish_without_input_or_receipt_fails_immediately(self) -> None:
        errors = io.StringIO()
        with mock.patch("the_guard.review.cli.load_config", return_value=SimpleNamespace(main_branch="main")), \
                mock.patch("the_guard.review.cli._read_nonblocking_stdin_text", return_value=None), \
                mock.patch("the_guard.review.cli.collect_review_scope", return_value=object()), \
                mock.patch("the_guard.review.cli.resolve_or_carry_forward_receipt", return_value=None), \
                contextlib.redirect_stderr(errors):
            code = review_main(["publish-github", "--pr", "sensebeing/the-guard#1"])
        self.assertEqual(code, 3)
        self.assertIn("No review verdict provided via --input-json/stdin", errors.getvalue())

    def test_cli_publish_accepts_piped_json(self) -> None:
        payload = {
            "verdict": "GO",
            "summary": "piped review",
            "blocking_findings": [],
            "reviewed_head_sha": "b" * 40,
        }
        published = {"status": "dry_run", "delivery_mode": "dry_run"}
        output = io.StringIO()
        with mock.patch("the_guard.review.cli.load_config", return_value=SimpleNamespace(main_branch="main")), \
                mock.patch("the_guard.review.cli.sys.stdin", io.StringIO(json.dumps(payload))), \
                mock.patch("the_guard.review.cli.publish_github_pr_review", return_value=published) as publish, \
                contextlib.redirect_stdout(output):
            code = review_main(["publish-github", "--pr", "sensebeing/the-guard#1", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), published)
        self.assertEqual(publish.call_args.kwargs["commit_id"], "b" * 40)

    def test_cli_publish_rejects_whitespace_only_piped_stdin(self) -> None:
        errors = io.StringIO()
        with mock.patch("the_guard.review.cli.load_config", return_value=SimpleNamespace(main_branch="main")), \
                mock.patch("the_guard.review.cli._read_nonblocking_stdin_text", return_value="   \n"), \
                mock.patch("the_guard.review.cli.resolve_or_carry_forward_receipt") as resolve, \
                contextlib.redirect_stderr(errors):
            code = review_main(["publish-github", "--pr", "sensebeing/the-guard#1"])
        self.assertEqual(code, 3)
        self.assertIn("Expecting value", errors.getvalue())
        resolve.assert_not_called()

    def test_static_finding_maps_to_inline_comment_without_required_fix(self) -> None:
        finding = Finding("guard.service-boundaries", "src/a.py", 12, "bad import")
        payload = build_github_review_payload("NO-GO", "summary", [finding])
        self.assertEqual(payload["comments"][0]["path"], "src/a.py")
        self.assertEqual(payload["comments"][0]["line"], 12)
        self.assertIn("bad import", payload["comments"][0]["body"])
        self.assertNotIn("**Required fix:**", payload["comments"][0]["body"])

    def test_guard_check_policy_only_publishes_github_dry_run(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = guard_main([
                "check", "--policy-only", "--github-pr", "owner/repo#42",
                "--github-dry-run", "--json",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["github_review"]["delivery_mode"], "dry_run")

    def test_parse_targets_and_rejects_invalid_values(self) -> None:
        self.assertEqual(parse_github_pr_target("owner/repo#42"), ("owner", "repo", 42))
        self.assertEqual(
            parse_github_pr_target("https://github.com/owner/repo/pull/42"),
            ("owner", "repo", 42),
        )
        for target in ("owner/repo", "owner/repo#0", "https://gitlab.com/owner/repo/pull/42"):
            with self.assertRaises(ValueError):
                parse_github_pr_target(target)

    def test_build_payload_separates_inline_and_body_findings(self) -> None:
        payload = build_github_review_payload(
            "NO-GO",
            "summary",
            [
                {"rule_id": "guard.rule", "file": "src/a.py", "line": 7, "issue": "bad", "required_fix": "fix"},
                {"rule_id": "guard.rule", "file": "src/b.py", "line": 0, "issue": "whole file", "required_fix": "fix"},
                {"rule_id": "guard.rule", "file": "", "line": 2, "issue": "no path", "required_fix": "fix"},
            ],
            commit_id="a" * 40,
        )
        self.assertIn("<!-- the-guard-review -->", payload["body"])
        self.assertEqual(len(payload["comments"]), 1)
        self.assertEqual(payload["comments"][0]["line"], 7)
        self.assertEqual(payload["comments"][0]["path"], "src/a.py")
        self.assertEqual(payload["body"].count("whole file"), 1)
        self.assertEqual(payload["body"].count("no path"), 1)
        self.assertEqual(payload["commit_id"], "a" * 40)

    def test_publish_dry_run(self) -> None:
        result = publish_github_pr_review("owner/repo#42", verdict="GO", summary="ok", findings=[], dry_run=True)
        self.assertEqual(result["delivery_mode"], "dry_run")
        self.assertEqual(result["status"], "dry_run")

    def test_publish_inline_success(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout='{"id": 9, "html_url": "https://example/review"}', stderr="")
        with mock.patch("the_guard.review.github.resolve_github_app_token", return_value=None), \
                mock.patch("the_guard.review.github.subprocess.run", return_value=completed) as run:
            result = publish_github_pr_review(
                "owner/repo#42", verdict="NO-GO", summary="needs work",
                findings=[{"rule_id": "guard.rule", "file": "a.py", "line": 3, "issue": "bad", "required_fix": "fix"}],
            )
        self.assertEqual(result["delivery_mode"], "inline")
        self.assertEqual(result["review_id"], 9)
        self.assertEqual(result["auth_mode"], "gh_cli")
        self.assertEqual(run.call_count, 1)

    def test_publish_422_fallback_does_not_duplicate_body_findings(self) -> None:
        first = subprocess.CompletedProcess([], 1, stdout="", stderr="422 Unprocessable Entity")
        second = subprocess.CompletedProcess([], 0, stdout='{"id": 10}', stderr="")
        with mock.patch("the_guard.review.github.resolve_github_app_token", return_value=None), \
                mock.patch("the_guard.review.github.subprocess.run", side_effect=[first, second]) as run:
            result = publish_github_pr_review(
                "owner/repo#42", verdict="NO-GO", summary="needs work",
                findings=[
                    {"rule_id": "guard.rule", "file": "a.py", "line": 3, "issue": "inline issue", "required_fix": "fix"},
                    {"rule_id": "guard.rule", "file": "", "line": 0, "issue": "body issue", "required_fix": "fix"},
                ],
            )
        self.assertEqual(result["delivery_mode"], "fallback_body")
        fallback = json.loads(run.call_args_list[1].kwargs["input"])
        self.assertEqual(fallback["comments"], [])
        self.assertEqual(fallback["body"].count("body issue"), 1)
        self.assertEqual(fallback["body"].count("inline issue"), 1)

    def test_publish_non_422_failure_raises(self) -> None:
        completed = subprocess.CompletedProcess([], 1, stdout="", stderr="500 server error")
        with mock.patch("the_guard.review.github.resolve_github_app_token", return_value=None), \
                mock.patch("the_guard.review.github.subprocess.run", return_value=completed):
            with self.assertRaises(RuntimeError):
                publish_github_pr_review("owner/repo#42", verdict="GO", summary="ok", findings=[])

    def test_publish_uses_github_app_token_when_resolved(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout='{"id": 11, "html_url": "https://example/review"}', stderr="")
        with mock.patch("the_guard.review.github.resolve_github_app_token", return_value="ghs_app_token") as resolve, \
                mock.patch("the_guard.review.github.subprocess.run", return_value=completed) as run:
            result = publish_github_pr_review("owner/repo#42", verdict="GO", summary="ok", findings=[])
        resolve.assert_called_once_with("owner", "repo")
        self.assertEqual(result["auth_mode"], "github_app")
        self.assertEqual(run.call_args.kwargs["env"], {**os.environ, "GH_TOKEN": "ghs_app_token"})

    def test_publish_422_fallback_reuses_github_app_token(self) -> None:
        first = subprocess.CompletedProcess([], 1, stdout="", stderr="422 Unprocessable Entity")
        second = subprocess.CompletedProcess([], 0, stdout='{"id": 12}', stderr="")
        with mock.patch("the_guard.review.github.resolve_github_app_token", return_value="ghs_app_token"), \
                mock.patch("the_guard.review.github.subprocess.run", side_effect=[first, second]) as run:
            result = publish_github_pr_review(
                "owner/repo#42", verdict="NO-GO", summary="needs work",
                findings=[{"rule_id": "guard.rule", "file": "a.py", "line": 3, "issue": "bad", "required_fix": "fix"}],
            )
        self.assertEqual(result["delivery_mode"], "fallback_body")
        self.assertEqual(result["auth_mode"], "github_app")
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["env"]["GH_TOKEN"], "ghs_app_token")

    def test_resolve_github_app_token_from_config_file(self) -> None:
        signing = subprocess.CompletedProcess([], 0, stdout=b"rsa-signature", stderr=b"")
        responses = [_FakeUrlResponse({"id": 99}), _FakeUrlResponse({"token": "ghs_installation"})]
        with tempfile.TemporaryDirectory() as root:
            config_path = Path(root) / "github-app.json"
            key_path = Path(root) / "app.pem"
            config_path.write_text(
                json.dumps({"app_id": 12345, "private_key_path": str(key_path)}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, EMPTY_APP_ENV, clear=True), \
                    mock.patch("the_guard.review.github.subprocess.run", return_value=signing) as run, \
                    mock.patch("the_guard.review.github.urllib.request.urlopen", side_effect=responses) as urlopen:
                token = resolve_github_app_token("owner", "repo", config_path=config_path)

        self.assertEqual(token, "ghs_installation")
        self.assertEqual(run.call_args.args[0], ["openssl", "dgst", "-sha256", "-sign", str(key_path)])
        installation_request = urlopen.call_args_list[0].args[0]
        self.assertEqual(installation_request.full_url, "https://api.github.com/repos/owner/repo/installation")
        self.assertEqual(installation_request.get_method(), "GET")
        jwt = installation_request.get_header("Authorization").removeprefix("Bearer ")
        self.assertEqual(_jwt_claims(jwt)["iss"], "12345")
        self.assertEqual(_jwt_claims(jwt)["exp"] - _jwt_claims(jwt)["iat"], 600)
        self.assertEqual(jwt.split(".")[2], "cnNhLXNpZ25hdHVyZQ")
        token_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(token_request.full_url, "https://api.github.com/app/installations/99/access_tokens")
        self.assertEqual(token_request.get_method(), "POST")
        self.assertEqual(token_request.get_header("Accept"), "application/vnd.github+json")
        self.assertEqual(token_request.get_header("User-agent"), "the-guard")

    def test_resolve_github_app_token_signs_inline_environment_key(self) -> None:
        signing = subprocess.CompletedProcess([], 0, stdout=b"rsa-signature", stderr=b"")
        inline_key = "-----BEGIN PRIVATE KEY-----\nfake-key-body\n-----END PRIVATE KEY-----\n"
        observed: dict[str, Any] = {}

        def fake_run(command: list[str], *, input: bytes, capture_output: bool, check: bool) -> subprocess.CompletedProcess[bytes]:
            observed["command"] = command
            observed["key_text"] = Path(command[4]).read_text(encoding="utf-8")
            observed["mode"] = stat.S_IMODE(os.stat(command[4]).st_mode)
            observed["input"] = input
            return signing

        responses = [_FakeUrlResponse({"id": 5}), _FakeUrlResponse({"token": "ghs_env"})]
        env = {**EMPTY_APP_ENV, "THE_GUARD_GITHUB_APP_ID": "777", "THE_GUARD_GITHUB_PRIVATE_KEY": inline_key}
        with tempfile.TemporaryDirectory() as root, \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("the_guard.review.github.subprocess.run", side_effect=fake_run), \
                mock.patch("the_guard.review.github.urllib.request.urlopen", side_effect=responses):
            token = resolve_github_app_token("owner", "repo", config_path=Path(root) / "missing.json")

        self.assertEqual(token, "ghs_env")
        self.assertEqual(observed["command"][:4], ["openssl", "dgst", "-sha256", "-sign"])
        self.assertEqual(observed["key_text"], inline_key)
        self.assertEqual(observed["mode"], 0o600)
        _, claims_b64 = observed["input"].split(b".")
        claims = json.loads(base64.urlsafe_b64decode(claims_b64 + b"=" * (-len(claims_b64) % 4)))
        self.assertEqual(claims["iss"], "777")

    def test_resolve_github_app_token_returns_none_without_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, EMPTY_APP_ENV, clear=True):
            token = resolve_github_app_token("owner", "repo", config_path=Path(root) / "missing.json")
        self.assertIsNone(token)

    def test_resolve_github_app_token_returns_none_when_app_not_installed(self) -> None:
        signing = subprocess.CompletedProcess([], 0, stdout=b"rsa-signature", stderr=b"")
        not_found = urllib.error.HTTPError("https://api.github.com/repos/owner/repo/installation", 404, "Not Found", None, None)
        with tempfile.TemporaryDirectory() as root:
            config_path = Path(root) / "github-app.json"
            config_path.write_text(json.dumps({"app_id": 42, "private_key": "inline"}), encoding="utf-8")
            with mock.patch.dict(os.environ, EMPTY_APP_ENV, clear=True), \
                    mock.patch("the_guard.review.github.subprocess.run", return_value=signing), \
                    mock.patch("the_guard.review.github.urllib.request.urlopen", side_effect=not_found):
                token = resolve_github_app_token("owner", "repo", config_path=config_path)
        self.assertIsNone(token)

    def test_resolve_github_app_token_propagates_configured_app_failures(self) -> None:
        signing = subprocess.CompletedProcess([], 0, stdout=b"rsa-signature", stderr=b"")
        cases = {
            "authentication": ({"side_effect": urllib.error.HTTPError("https://api.github.com", 401, "Unauthorized", None, None)}, urllib.error.HTTPError),
            "transport": ({"side_effect": urllib.error.URLError("offline")}, urllib.error.URLError),
            "malformed_response": ({"return_value": _FakeUrlResponse(b"not-json")}, ValueError),
        }
        with tempfile.TemporaryDirectory() as root:
            config_path = Path(root) / "github-app.json"
            config_path.write_text(json.dumps({"app_id": 42, "private_key": "inline"}), encoding="utf-8")
            for name, (urlopen_kwargs, expected) in cases.items():
                with self.subTest(name), mock.patch.dict(os.environ, EMPTY_APP_ENV, clear=True), \
                        mock.patch("the_guard.review.github.subprocess.run", return_value=signing), \
                        mock.patch("the_guard.review.github.urllib.request.urlopen", **urlopen_kwargs):
                    with self.assertRaises(expected):
                        resolve_github_app_token("owner", "repo", config_path=config_path)

    def test_resolve_github_app_token_propagates_configured_app_signing_failure(self) -> None:
        failure = subprocess.CalledProcessError(1, ["openssl", "dgst", "-sha256", "-sign"])
        with tempfile.TemporaryDirectory() as root:
            config_path = Path(root) / "github-app.json"
            config_path.write_text(json.dumps({"app_id": 42, "private_key": "inline"}), encoding="utf-8")
            with mock.patch.dict(os.environ, EMPTY_APP_ENV, clear=True), \
                    mock.patch("the_guard.review.github.subprocess.run", side_effect=failure):
                with self.assertRaises(subprocess.CalledProcessError):
                    resolve_github_app_token("owner", "repo", config_path=config_path)

    def test_resolve_github_app_token_rejects_incomplete_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config_path = Path(root) / "github-app.json"
            config_path.write_text(json.dumps({"app_id": 42}), encoding="utf-8")
            with mock.patch.dict(os.environ, EMPTY_APP_ENV, clear=True):
                with self.assertRaises(ValueError):
                    resolve_github_app_token("owner", "repo", config_path=config_path)
            partial_env = {**EMPTY_APP_ENV, "THE_GUARD_GITHUB_APP_ID": "42"}
            with mock.patch.dict(os.environ, partial_env, clear=True):
                with self.assertRaises(ValueError):
                    resolve_github_app_token("owner", "repo", config_path=Path(root) / "missing.json")

    def test_publish_fails_closed_when_configured_app_token_fails(self) -> None:
        with mock.patch("the_guard.review.github.resolve_github_app_token", side_effect=urllib.error.URLError("offline")), \
                mock.patch("the_guard.review.github.subprocess.run") as run:
            with self.assertRaises(urllib.error.URLError):
                publish_github_pr_review("owner/repo#42", verdict="GO", summary="ok", findings=[])
        run.assert_not_called()

    def test_cli_publish_dry_run_and_malformed_json(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "review.json"
            path.write_text(
                json.dumps({"verdict": "GO", "summary": "ok", "reviewed_head_sha": "a" * 40, "blocking_findings": []}),
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = review_main(["publish-github", "--pr", "sensebeing/the-guard#1", "--input-json", str(path), "--dry-run", "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["delivery_mode"], "dry_run")
            path.write_text(json.dumps({"summary": "missing verdict"}), encoding="utf-8")
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                code = review_main(["publish-github", "--pr", "sensebeing/the-guard#1", "--input-json", str(path), "--dry-run", "--json"])
            self.assertEqual(code, 3)
            self.assertIn("Input JSON must contain verdict and summary", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
