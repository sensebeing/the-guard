"""Shared repository fixtures for the architecture-review resident tests.

The temporary origin/topic repository and the streaming helpers are owned here
so both review test files exercise the same Git topology.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _write_exec(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


class ArchitectureReviewTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_active = os.environ.pop("ALTER_ARCH_REVIEW_ACTIVE", None)
        self._saved_session_mode = os.environ.pop("GUARD_WORKTREE_SESSION_MODE", None)

    def tearDown(self) -> None:
        if self._saved_active is not None:
            os.environ["ALTER_ARCH_REVIEW_ACTIVE"] = self._saved_active
        else:
            os.environ.pop("ALTER_ARCH_REVIEW_ACTIVE", None)
        if self._saved_session_mode is not None:
            os.environ["GUARD_WORKTREE_SESSION_MODE"] = self._saved_session_mode
        else:
            os.environ.pop("GUARD_WORKTREE_SESSION_MODE", None)

    def _init_repo(self, tmp_dir: Path) -> tuple[Path, Path]:
        origin = tmp_dir / "origin.git"
        subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
        repo = tmp_dir / "repo"
        subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
        _git(repo, "checkout", "-b", "main")
        _git(repo, "config", "user.email", "test@example.invalid")
        _git(repo, "config", "user.name", "Test User")
        (repo / "README.md").write_text("# base\n", encoding="utf-8")
        (repo / "CLAUDE.md").write_text(
            "# Root\n\n"
            "<!-- product-engineering-quality:start -->\n"
            "```yaml\n"
            "schema: product-engineering-quality/v1\n"
            "rules:\n"
            "  - id: peq.single-source-of-truth\n"
            "    severity: block\n"
            "    scope: [backend]\n"
            "    summary: One canonical owner.\n"
            "    details: docs/engineering-quality/rules/single-source-of-truth.md\n"
            "    checker: static-plus-review\n"
            "```\n"
            "<!-- product-engineering-quality:end -->\n",
            encoding="utf-8",
        )
        detail = repo / "docs/engineering-quality/rules/single-source-of-truth.md"
        detail.parent.mkdir(parents=True, exist_ok=True)
        detail.write_text(
            "---\n"
            "id: peq.single-source-of-truth\n"
            "title: Single Source Of Truth\n"
            "manifest: CLAUDE.md#product-engineering-quality-policy\n"
            "---\n\n"
            "# Single Source Of Truth\n\n"
            "Every business rule has a single executable owner.\n",
            encoding="utf-8",
        )
        _git(repo, "add", "README.md", "CLAUDE.md", "docs/engineering-quality/rules/single-source-of-truth.md")
        _git(repo, "commit", "-m", "base")
        _git(repo, "push", "-u", "origin", "main")
        _git(repo, "checkout", "-b", "topic")
        return repo, origin
