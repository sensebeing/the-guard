"""Focused contracts for GitHub PR review publication."""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from the_guard.review.cli import main as review_main
from the_guard.review.github import (
    build_github_review_payload,
    parse_github_pr_target,
    publish_github_pr_review,
)


class GithubReviewTests(unittest.TestCase):
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
        with mock.patch("the_guard.review.github.subprocess.run", return_value=completed) as run:
            result = publish_github_pr_review(
                "owner/repo#42", verdict="NO-GO", summary="needs work",
                findings=[{"rule_id": "guard.rule", "file": "a.py", "line": 3, "issue": "bad", "required_fix": "fix"}],
            )
        self.assertEqual(result["delivery_mode"], "inline")
        self.assertEqual(result["review_id"], 9)
        self.assertEqual(run.call_count, 1)

    def test_publish_422_fallback_does_not_duplicate_body_findings(self) -> None:
        first = subprocess.CompletedProcess([], 1, stdout="", stderr="422 Unprocessable Entity")
        second = subprocess.CompletedProcess([], 0, stdout='{"id": 10}', stderr="")
        with mock.patch("the_guard.review.github.subprocess.run", side_effect=[first, second]) as run:
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
        with mock.patch("the_guard.review.github.subprocess.run", return_value=completed):
            with self.assertRaises(RuntimeError):
                publish_github_pr_review("owner/repo#42", verdict="GO", summary="ok", findings=[])

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
